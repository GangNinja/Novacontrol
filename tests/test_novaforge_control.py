"""NF-19 game-control interface tests.

Three layers, each failing for its own reason:

* **Model** — parsing, per-kind parameter rules, policy bounds, target identity
  mismatches. A widened bound or a kind that accepts the wrong fields fails here.
* **Service** — the real ``ActionRunner``/``InputGuard`` against a recording
  injector: dispatch vs. completion, target refusal BEFORE anything is sent,
  timeouts, cancellation, busy refusal, release-on-failure and bounded history.
* **Integration** — the real manager and the real HTTP routes: the Guardian gate,
  the shared action budget, session close cancelling an in-flight hold, and the
  honest ``effect: None`` on every result.

No test sends real input: every injector here records edges.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.desktop.input import WindowGeometry
from novacontrol.novaforge import (
    ActionRunner,
    MissionValidationError,
    NovaForgeManager,
    SessionState,
)
from novacontrol.novaforge.control import (
    CONTROL_KINDS,
    ControlAction,
    ControlKind,
    ControlPolicy,
    ControlStatus,
    ControlTarget,
    GameControlService,
)
from test_novaforge import FakeDesktop, _mission
from test_novaforge_gameplay import RecordingInput, geometry
from test_novaforge_gameplay_api import _GameplayApiFixture

SESSION = "session-1"


def target(**overrides: Any) -> ControlTarget:
    values: dict[str, Any] = {
        "session_id": SESSION,
        "title": "NC FORGE HARNESS",
        "handle": 0x1234,
        "pid": 4242,
    }
    values.update(overrides)
    return ControlTarget(**values)


def action(kind: str | ControlKind, **fields: Any) -> ControlAction:
    payload = {"kind": str(getattr(kind, "value", kind)), **fields}
    return ControlAction.from_payload(payload, session_id=SESSION, target=target())


def make_service(
    *,
    policy: ControlPolicy | None = None,
    injector: Any | None = None,
    window: Any | None = geometry(),
    gate: Any | None = None,
    publish: Any | None = None,
) -> tuple[GameControlService, ActionRunner, RecordingInput]:
    recorder = injector if injector is not None else RecordingInput()
    runner = ActionRunner(input=recorder)

    async def read_target(_session_id: str) -> Any:
        return window

    service = GameControlService(
        policy=policy or ControlPolicy(),
        runner=runner,
        read_target=read_target,
        gate=gate
        or (lambda _sid: {"allow": True, "state": "allowed", "reason": "test gate"}),
        publish=publish,
    )
    return service, runner, recorder


class _SlowInput(RecordingInput):
    """A press that takes longer than the action's deadline."""

    async def press(self, kind: str, name: str) -> None:
        await asyncio.sleep(0.3)
        await super().press(kind, name)


class ControlModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ControlPolicy()

    def test_every_declared_kind_parses_and_validates(self) -> None:
        samples = {
            "key_press": {"key": "w"},
            "key_release": {"key": "w"},
            "key_hold": {"key": "w", "duration_ms": 100},
            "mouse_move": {"x": 500, "y": 300},
            "mouse_press": {"x": 500, "y": 300},
            "mouse_release": {},
            "mouse_hold": {"x": 500, "y": 300, "duration_ms": 100},
            "wait": {"duration_ms": 50},
            "focus": {},
        }
        self.assertEqual(set(samples), set(CONTROL_KINDS))
        for kind, fields in samples.items():
            with self.subTest(kind=kind):
                self.assertEqual(action(kind, **fields).validate(self.policy), [])

    def test_a_missing_or_unknown_kind_is_malformed_not_a_default(self) -> None:
        for payload in ({}, {"kind": ""}, {"kind": "teleport"}, {"kind": "key"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ControlAction.from_payload(payload, session_id=SESSION, target=target())

    def test_key_actions_need_a_known_allowed_key_and_no_coordinates(self) -> None:
        self.assertTrue(action("key_press").validate(self.policy))
        self.assertTrue(
            any("not a key this build knows" in p for p in action("key_press", key="f13-magic").validate(self.policy))
        )
        alt = action("key_press", key="alt")
        self.assertTrue(any("allowed set" in p for p in alt.validate(self.policy)), alt.validate(self.policy))
        self.assertTrue(
            any("does not take coordinates" in p for p in action("key_press", key="w", x=2, y=3).validate(self.policy))
        )

    def test_hold_durations_and_wait_are_bounded_by_the_policy(self) -> None:
        too_long = action("key_hold", key="w", duration_ms=10_000)
        self.assertTrue(any("between 1 and 1500 ms" in p for p in too_long.validate(self.policy)))
        self.assertTrue(action("key_hold", key="w", duration_ms=0).validate(self.policy))
        long_wait = action("wait", duration_ms=60_000)
        self.assertTrue(any("between 1 and 2000 ms" in p for p in long_wait.validate(self.policy)))
        self.assertTrue(action("wait", duration_ms=50, key="w").validate(self.policy))

    def test_a_deadline_must_exceed_the_duration_it_wraps(self) -> None:
        contradictory = action("key_hold", key="w", duration_ms=500, deadline_ms=200)
        self.assertTrue(
            any("must exceed the action's duration" in p for p in contradictory.validate(self.policy)),
            contradictory.validate(self.policy),
        )
        self.assertEqual(action("wait", duration_ms=50, deadline_ms=5000).validate(self.policy), [])

    def test_mouse_actions_need_a_point_or_one_of_the_four_kinds(self) -> None:
        self.assertTrue(action("mouse_move", x=10).validate(self.policy))
        self.assertTrue(action("mouse_press", y=10).validate(self.policy))
        self.assertTrue(action("mouse_press", x=-1, y=10).validate(self.policy))
        right = action("mouse_press", button="right")
        self.assertTrue(any("allowed set" in p for p in right.validate(self.policy)))
        self.assertTrue(action("mouse_press", button="pedal").validate(self.policy))
        self.assertEqual(action("mouse_hold", x=200, y=200, duration_ms=100).validate(self.policy), [])

    def test_a_policy_that_cannot_hold_its_own_bounds_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            GameControlService(
                policy=ControlPolicy(action_deadline_ms=100),
                runner=None,
                read_target=lambda _sid: None,
                gate=lambda _sid: {"allow": False},
            )
        with self.assertRaises(ValueError):
            GameControlService(
                policy=ControlPolicy(allowed_keys=frozenset()),
                runner=None,
                read_target=lambda _sid: None,
                gate=lambda _sid: {"allow": False},
            )

    def test_a_session_and_its_target_must_name_each_other(self) -> None:
        mismatched = ControlAction(
            kind=ControlKind.KEY_PRESS, session_id="other", target=target(), key="w"
        )
        self.assertTrue(any("does not match" in p for p in mismatched.validate(self.policy)))

    def test_target_identity_is_handle_and_process_not_title(self) -> None:
        pinned = target()
        self.assertEqual(pinned.mismatches(geometry()), [])
        self.assertEqual(pinned.mismatches(geometry(title="A NEW TITLE")), [])
        self.assertTrue(pinned.mismatches(geometry(handle=0x9999)))
        self.assertTrue(pinned.mismatches(geometry(pid=7)))

    def test_the_payload_cannot_redirect_the_target(self) -> None:
        parsed = ControlAction.from_payload(
            {"kind": "key_press", "key": "w", "target": {"handle": 0xDEAD, "pid": 1}},
            session_id=SESSION,
            target=target(),
        )
        self.assertEqual(parsed.target.handle, 0x1234)
        self.assertEqual(parsed.target.pid, 4242)

    def test_a_result_never_carries_an_effect_claim(self) -> None:
        service, _runner, _input = make_service()
        result = asyncio.run(service.execute(action("wait", duration_ms=10))).to_dict()
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["effect"])
        self.assertIn("does not mean the", result["effect_note"])
        self.assertTrue(result["dispatched_at"] == "")


class ControlExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_key_press_dispatches_and_stays_held_until_released(self) -> None:
        service, _runner, injector = make_service()
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.status, ControlStatus.COMPLETED)
        self.assertTrue(result.ok)
        self.assertTrue(result.dispatched_at)
        self.assertEqual(injector.presses, ["key:w"])
        self.assertEqual(injector.releases, [])
        self.assertEqual(service.held(), [{"kind": "key", "name": "w"}])
        self.assertEqual(result.cleanup["held_after"], 1)

        released = await service.execute(action("key_release", key="w"))
        self.assertEqual(released.status, ControlStatus.COMPLETED)
        self.assertEqual(injector.releases, ["key:w"])
        self.assertEqual(service.held(), [])

    async def test_a_bounded_hold_presses_then_releases(self) -> None:
        service, _runner, injector = make_service()
        started = time.monotonic()
        result = await service.execute(action("key_hold", key="w", duration_ms=120))
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, ControlStatus.COMPLETED, result.reason)
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertEqual(injector.presses, ["key:w"])
        self.assertEqual(injector.releases, ["key:w"])
        self.assertEqual(service.held(), [])

    async def test_a_wait_sends_nothing_and_is_not_a_dispatch(self) -> None:
        service, _runner, injector = make_service()
        result = await service.execute(action("wait", duration_ms=30))
        self.assertEqual(result.status, ControlStatus.COMPLETED)
        self.assertEqual(result.dispatched_at, "")
        self.assertEqual(injector.presses, [])
        self.assertEqual(injector.moves, [])
        self.assertIn("no input was sent", result.reason)

    async def test_a_mouse_move_and_hold_use_the_shared_injector(self) -> None:
        service, _runner, injector = make_service()
        moved = await service.execute(action("mouse_move", x=500, y=300))
        self.assertEqual(moved.status, ControlStatus.COMPLETED)
        self.assertEqual(injector.moves, [(500, 300)])
        held = await service.execute(action("mouse_hold", x=500, y=300, duration_ms=60))
        self.assertEqual(held.status, ControlStatus.COMPLETED)
        self.assertEqual(injector.presses, ["button:left"])
        self.assertEqual(injector.releases, ["button:left"])
        self.assertEqual(service.held(), [])

    async def test_a_foreign_gate_refusal_sends_nothing(self) -> None:
        service, _runner, injector = make_service(
            gate=lambda _sid: {"allow": False, "state": "closed", "reason": "session ended"}
        )
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.code, "closed")
        self.assertEqual(result.validation["ok"], True)
        self.assertEqual(injector.presses, [])
        self.assertEqual(result.dispatched_at, "")

    async def test_a_lost_target_is_refused_before_dispatch(self) -> None:
        service, _runner, injector = make_service(window=None)
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertEqual(result.code, "target_lost")
        self.assertEqual(injector.presses, [])

    async def test_a_changed_window_is_refused_not_rerouted(self) -> None:
        service, _runner, injector = make_service(window=geometry(handle=0x9999))
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.code, "target_changed")
        self.assertIn("0x1234", result.reason)
        self.assertEqual(injector.presses, [])

        service, _runner, injector = make_service(window=geometry(pid=7))
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.code, "target_changed")
        self.assertEqual(injector.presses, [])

    async def test_a_background_or_minimized_target_is_refused(self) -> None:
        service, _runner, injector = make_service(window=geometry(foreground=False))
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.code, "not_foreground")
        self.assertEqual(injector.presses, [])

        hidden = geometry()
        hidden = WindowGeometry(
            handle=hidden.handle,
            title=hidden.title,
            bounds=hidden.bounds,
            client=hidden.client,
            pid=hidden.pid,
            minimized=True,
        )
        service, _runner, injector = make_service(window=hidden)
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.code, "target_minimized")
        self.assertEqual(injector.presses, [])

    async def test_a_focus_action_is_allowed_exactly_when_other_input_is_not(self) -> None:
        # The refusal that protects every other kind must not protect the one
        # kind whose job is to remove its cause: a background target can be
        # focused, and a focused target can then be acted on.
        service, _runner, injector = make_service(window=geometry(foreground=False))
        focused = await service.execute(action("focus"))
        self.assertEqual(focused.status, ControlStatus.COMPLETED)
        self.assertEqual(injector.focuses, [0x1234])
        self.assertIn("verified it is the foreground window", focused.reason)
        self.assertEqual(injector.presses, [])

        minimized = geometry()
        minimized = WindowGeometry(
            handle=minimized.handle,
            title=minimized.title,
            bounds=minimized.bounds,
            client=minimized.client,
            pid=minimized.pid,
            minimized=True,
        )
        service, _runner, injector = make_service(window=minimized)
        raised = await service.execute(action("focus"))
        self.assertEqual(raised.status, ControlStatus.COMPLETED)
        self.assertEqual(injector.focuses, [0x1234])

    async def test_a_focus_action_never_sends_a_key_or_a_click(self) -> None:
        service, _runner, injector = make_service(window=geometry(foreground=False))
        result = await service.execute(action("focus", key="w", x=5, y=5))
        self.assertEqual(result.status, ControlStatus.REJECTED)
        self.assertTrue(result.reason)
        self.assertEqual(injector.focuses, [])
        self.assertEqual(injector.presses, [])
        self.assertEqual(injector.moves, [])

    async def test_a_point_outside_the_client_area_is_refused(self) -> None:
        service, _runner, injector = make_service()
        result = await service.execute(action("mouse_move", x=10, y=10))
        self.assertEqual(result.code, "outside_target")
        self.assertEqual(injector.moves, [])

    async def test_a_second_request_while_one_is_running_is_busy_not_queued(self) -> None:
        service, _runner, injector = make_service()
        first = asyncio.create_task(
            service.execute(action("key_hold", key="w", duration_ms=300))
        )
        for _ in range(50):
            if service.active:
                break
            await asyncio.sleep(0.005)
        second = await service.execute(action("key_press", key="s"))
        self.assertEqual(second.status, ControlStatus.REJECTED)
        self.assertEqual(second.code, "busy")
        self.assertEqual(injector.presses, ["key:w"])
        completed = await first
        self.assertEqual(completed.status, ControlStatus.COMPLETED)
        self.assertEqual(service.status()["counts"]["rejected"], 1)

    async def test_a_slow_injector_times_out_and_releases(self) -> None:
        service, _runner, injector = make_service(injector=_SlowInput())
        result = await service.execute(action("key_press", key="w", deadline_ms=80))
        self.assertEqual(result.status, ControlStatus.TIMED_OUT, result.reason)
        self.assertEqual(result.code, "timed_out")
        self.assertFalse(result.ok)
        self.assertEqual(service.held(), [])
        self.assertEqual(result.cleanup["held_after"], 0)

    async def test_cancel_interrupts_a_hold_and_frees_the_key(self) -> None:
        service, _runner, injector = make_service()
        task = asyncio.create_task(
            service.execute(action("key_hold", key="w", duration_ms=1000))
        )
        for _ in range(100):
            if service.held():
                break
            await asyncio.sleep(0.005)
        self.assertEqual(service.held(), [{"kind": "key", "name": "w"}])
        cancelled = await service.cancel("test")
        self.assertTrue(cancelled["cancelled"], cancelled)
        result = await task
        self.assertEqual(result.status, ControlStatus.CANCELLED)
        self.assertIn("cancel", result.reason.lower())
        self.assertEqual(injector.releases, ["key:w"])
        self.assertEqual(service.held(), [])

    async def test_a_cancel_for_another_session_does_not_stop_this_action(self) -> None:
        service, _runner, injector = make_service()
        task = asyncio.create_task(
            service.execute(action("key_hold", key="w", duration_ms=200))
        )
        for _ in range(100):
            if service.held():
                break
            await asyncio.sleep(0.005)
        refused = await service.cancel("stale", session_id="session-other")
        self.assertFalse(refused["cancelled"], refused)
        self.assertIn("different session", refused["reason"])
        result = await task
        self.assertEqual(result.status, ControlStatus.COMPLETED)
        self.assertEqual(injector.releases, ["key:w"])

    async def test_a_cancel_during_a_slow_press_cancels_and_releases_it(self) -> None:
        # The press edge has no checkpoint of its own; a stop requested while it
        # is in flight must still end the action as CANCELLED and release the key
        # instead of reporting completion with the key still down.
        service, _runner, injector = make_service(injector=_SlowInput())
        task = asyncio.create_task(service.execute(action("key_press", key="w")))
        for _ in range(100):
            if service.active:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        requested = await service.cancel("slow-press", session_id=SESSION)
        self.assertTrue(requested["cancelled"], requested)
        result = await task
        self.assertEqual(result.status, ControlStatus.CANCELLED, result.reason)
        self.assertEqual(injector.presses, ["key:w"])
        self.assertEqual(injector.releases, ["key:w"])
        self.assertEqual(service.held(), [])

    async def test_a_cancelled_caller_task_still_releases_what_it_held(self) -> None:
        service, _runner, injector = make_service()
        task = asyncio.create_task(
            service.execute(action("key_hold", key="w", duration_ms=1000))
        )
        for _ in range(100):
            if service.held():
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(injector.releases, ["key:w"])
        self.assertEqual(service.held(), [])
        # The stop was one-shot about that action: the next request still runs.
        again = await service.execute(action("key_press", key="s"))
        self.assertEqual(again.status, ControlStatus.COMPLETED)

    async def test_a_failed_dispatch_is_reported_and_leaves_nothing_held(self) -> None:
        injector = RecordingInput()
        injector.fail_press = True
        service, _runner, _input = make_service(injector=injector)
        result = await service.execute(action("key_hold", key="w", duration_ms=40))
        self.assertEqual(result.status, ControlStatus.FAILED)
        self.assertEqual(result.code, "dispatch_failed")
        self.assertFalse(result.ok)
        self.assertEqual(result.dispatched_at, "")
        self.assertIn("injector refused the press", result.error)
        self.assertEqual(service.held(), [])
        self.assertEqual(result.cleanup["held_after"], 0)

    async def test_a_failed_release_is_cleanup_failed_not_completed(self) -> None:
        injector = RecordingInput()
        injector.fail_release = True
        service, _runner, _input = make_service(injector=injector)
        result = await service.execute(action("key_hold", key="w", duration_ms=40))
        self.assertEqual(result.status, ControlStatus.CLEANUP_FAILED, result.reason)
        self.assertEqual(result.code, "cleanup_failed")
        self.assertFalse(result.ok)
        self.assertFalse(result.cleanup["ok"])
        self.assertEqual(result.cleanup["held_after"], 1)
        self.assertTrue(result.cleanup["failed"])
        self.assertEqual(len(service.held()), 1)

    async def test_a_duplicate_press_or_phantom_release_is_refused(self) -> None:
        service, _runner, injector = make_service()
        self.assertEqual((await service.execute(action("key_press", key="w"))).status, ControlStatus.COMPLETED)
        duplicate = await service.execute(action("key_press", key="w"))
        self.assertEqual(duplicate.code, "already_held")
        self.assertEqual(injector.presses, ["key:w"])
        phantom = await service.execute(action("key_release", key="s"))
        self.assertEqual(phantom.code, "not_held")
        self.assertEqual(injector.releases, [])

    async def test_release_is_idempotent_and_refused_while_running(self) -> None:
        service, _runner, injector = make_service()
        await service.execute(action("key_press", key="w"))
        first = await service.release("test")
        self.assertTrue(first["ok"])
        self.assertEqual(first["released"], 1)
        second = await service.release("test")
        self.assertTrue(second["ok"])
        self.assertEqual(second["released"], 0)
        self.assertEqual(injector.releases, ["key:w"])

        task = asyncio.create_task(service.execute(action("key_hold", key="s", duration_ms=200)))
        for _ in range(50):
            if service.active:
                break
            await asyncio.sleep(0.005)
        refused = await service.release("test")
        self.assertEqual(refused["reason"], "busy")
        self.assertFalse(refused["ok"])
        await task

    async def test_shutdown_refuses_new_actions_and_frees_held_input(self) -> None:
        service, _runner, injector = make_service()
        await service.execute(action("key_press", key="w"))
        cleanup = await service.shutdown()
        self.assertTrue(cleanup["ok"])
        self.assertEqual(injector.releases, ["key:w"])
        after = await service.execute(action("key_press", key="s"))
        self.assertEqual(after.code, "service_stopped")
        self.assertEqual(injector.presses, ["key:w"])

    async def test_history_is_bounded_and_status_counts_every_ending(self) -> None:
        service, _runner, _injector = make_service(policy=ControlPolicy(history_limit=3))
        for _ in range(5):
            await service.execute(action("wait", duration_ms=5))
        status = service.status()
        self.assertEqual(len(service.history(10)), 3)
        self.assertEqual(status["counts"]["completed"], 5)
        self.assertEqual(status["counts"]["received"], 5)
        self.assertEqual(status["last_result"]["status"], "completed")
        self.assertFalse(status["available"] is None)

    async def test_dispatch_and_result_are_announced_on_the_bus(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        service, _runner, _injector = make_service(
            publish=lambda event_type, payload: events.append((event_type, payload))
        )
        await service.execute(action("key_hold", key="w", duration_ms=20))
        kinds = [event_type for event_type, _payload in events]
        self.assertIn("novaforge.control.dispatched", kinds)
        self.assertIn("novaforge.control.result", kinds)
        result_payload = next(payload for name, payload in events if name == "novaforge.control.result")
        self.assertEqual(result_payload["status"], "completed")
        self.assertTrue(result_payload["dispatched"])
        self.assertNotIn("effect", result_payload)

    async def test_an_unwired_runner_refuses_truthfully(self) -> None:
        service = GameControlService(
            policy=ControlPolicy(),
            runner=None,
            read_target=lambda _sid: None,
            gate=lambda _sid: {"allow": True},
        )
        result = await service.execute(action("key_press", key="w"))
        self.assertEqual(result.code, "unsupported")
        self.assertFalse(service.available)
        self.assertEqual(service.status()["held"], [])


class ManagerControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.desktop = FakeDesktop()
        self.input = RecordingInput()
        self.probe_window: Any = geometry(title=self.desktop.window.title)

        async def probe(_handle: int) -> Any:
            return self.probe_window

        self.manager = NovaForgeManager(
            data_dir=Path(self.temp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            action_input=self.input,
            objective_probe=probe,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=1.0,
            stop_timeout=2.0,
        )
        self.record = None

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    async def start(self, **mission: Any) -> Any:
        payload = {
            "name": "Control Probe",
            "objective": "Exercise the control interface",
            "target_title": self.desktop.window.title,
            "frame_interval_seconds": 0.05,
            **mission,
        }
        described = await self.manager.start(payload)
        self.record = self.manager.get(described["session_id"])
        for _ in range(60):
            if self.record is not None and self.record.target_validated:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.record is not None and self.record.target_validated)
        return self.record

    async def test_a_control_action_runs_through_the_session_and_its_budget(self) -> None:
        record = await self.start()
        result = await self.manager.control_execute(
            record.session_id, {"kind": "key_press", "key": "w"}
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["ok"])
        self.assertFalse(result["effect_verified"])
        self.assertIsNone(result["result"]["effect"])
        self.assertEqual(self.input.presses, ["key:w"])
        self.assertEqual(record.actions_attempted, 1)
        self.assertEqual(record.actions_executed, 1)
        self.assertEqual(record.action_records[-1]["status"], "completed")

        status = self.manager.control_status(record.session_id)
        self.assertEqual(status["session"]["state"], "RUNNING")
        self.assertEqual(len(status["held"]), 1)
        self.assertTrue(status["session"]["gate"]["allow"])

        released = await self.manager.release_control_inputs(record.session_id)
        self.assertTrue(released["cleanup"]["ok"], released)
        self.assertEqual(self.input.releases, ["key:w"])
        self.assertEqual(released["held"], [])

    async def test_the_guardian_gate_and_the_budget_refuse_direct_control(self) -> None:
        record = await self.start(max_actions=1)
        first = await self.manager.control_execute(
            record.session_id, {"kind": "key_hold", "key": "w", "duration_ms": 30}
        )
        self.assertEqual(first["status"], "completed", first)
        second = await self.manager.control_execute(
            record.session_id, {"kind": "key_press", "key": "s"}
        )
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["code"], "budget_exhausted")
        self.assertEqual(self.input.presses, ["key:w"])

    async def test_a_closed_session_refuses_control_and_sends_nothing(self) -> None:
        record = await self.start()
        await self.manager.stop(record.session_id, reason="test_done")
        self.assertEqual(record.state, SessionState.STOPPED)
        result = await self.manager.control_execute(
            record.session_id, {"kind": "key_press", "key": "w"}
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn(result["code"], {"closed", "not_running"})
        self.assertEqual(self.input.presses, [])

    async def test_a_lost_target_is_refused_and_pauses_the_session(self) -> None:
        record = await self.start()
        self.probe_window = None
        result = await self.manager.control_execute(
            record.session_id, {"kind": "key_press", "key": "w"}
        )
        self.assertEqual(result["code"], "target_lost")
        self.assertEqual(self.input.presses, [])
        self.assertEqual(record.state, SessionState.PAUSED)
        self.assertEqual(record.pause_reason, "target_lost")

    async def test_stopping_a_session_cancels_an_in_flight_hold(self) -> None:
        record = await self.start()
        task = asyncio.create_task(
            self.manager.control_execute(
                record.session_id, {"kind": "key_hold", "key": "w", "duration_ms": 900}
            )
        )
        for _ in range(100):
            if self.manager.control.held():
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(self.manager.control.held()), 1)
        # ``cancel`` (like emergency stop) deliberately does not queue behind the
        # action's lock; a graceful ``stop`` waits for the action to finish first.
        await self.manager.cancel(record.session_id)
        result = await task
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(self.input.releases, ["key:w"])
        self.assertEqual(self.input.held(), 0)
        self.assertEqual(self.manager.control.held(), [])
        self.assertTrue(record.input_cleanup.get("ok"), record.input_cleanup)

    async def test_a_stale_session_cannot_cancel_or_release_the_live_one(self) -> None:
        first = await self.start()
        await self.manager.stop(first.session_id, reason="first_done")
        second = await self.start()
        pressed = await self.manager.control_execute(
            second.session_id, {"kind": "key_press", "key": "w"}
        )
        self.assertEqual(pressed["status"], "completed", pressed)
        self.assertEqual(len(self.manager.control.held()), 1)

        # The stale session's read must not describe the live session's action.
        stale = self.manager.control_status(first.session_id)
        self.assertIsNone(stale["active"])
        self.assertIsNone(stale["last_result"])
        self.assertEqual(stale["history"], [])

        cancelled = await self.manager.cancel_control_action(first.session_id)
        self.assertFalse(cancelled["cancelled"], cancelled)
        self.assertEqual(len(self.manager.control.held()), 1)

        released = await self.manager.release_control_inputs(first.session_id)
        self.assertFalse(released["cleanup"]["ok"])
        self.assertEqual(released["cleanup"]["reason"], "wrong_session")
        self.assertEqual(len(self.manager.control.held()), 1)

        released = await self.manager.release_control_inputs(second.session_id)
        self.assertTrue(released["cleanup"]["ok"], released)
        self.assertEqual(self.manager.control.held(), [])

    async def test_an_unparseable_body_is_a_validation_error(self) -> None:
        record = await self.start()
        with self.assertRaises(MissionValidationError):
            await self.manager.control_execute(record.session_id, {"kind": "teleport"})
        with self.assertRaises(MissionValidationError):
            await self.manager.control_execute(record.session_id, {"kind": "key_hold", "key": "w", "duration_ms": "abc"})
        self.assertEqual(self.input.presses, [])

    async def test_the_capability_row_never_claims_a_verified_effect(self) -> None:
        capabilities = self.manager.capabilities()
        control = capabilities["game_control"]
        self.assertTrue(control["available"])
        self.assertTrue(control["target_verified_before_dispatch"])
        self.assertFalse(control["effect_verified"])
        self.assertEqual(control["kinds"], sorted(CONTROL_KINDS))
        self.assertTrue(control["policy"]["action_deadline_ms"])


class NovaForgeControlApiTests(_GameplayApiFixture):
    """The NF-19 routes through the real app, manager and Guardian."""

    def _control(self, session_id: str) -> str:
        return "/novaforge/sessions/" + session_id + "/control"

    def test_unknown_session_is_a_404_on_every_control_route(self) -> None:
        for method, path, payload in (
            ("get", "/novaforge/sessions/nope/control", None),
            ("post", "/novaforge/sessions/nope/control", {"kind": "key_press", "key": "w"}),
            ("post", "/novaforge/sessions/nope/control/cancel", None),
            ("post", "/novaforge/sessions/nope/control/release", None),
        ):
            with self.subTest(path=path):
                response = getattr(self._client, method)(
                    path, **({"json": payload} if payload is not None else {})
                )
                self.assertEqual(response.status_code, 404, response.text)

    def test_control_status_reports_bounds_gate_and_held_input(self) -> None:
        session = self._validated_session()
        body = self._client.get(self._control(session["session_id"])).json()
        self.assertEqual(body["schema_version"], "nf19.1")
        self.assertTrue(body["available"])
        self.assertEqual(body["policy"]["action_deadline_ms"], 4000)
        self.assertEqual(body["session"]["state"], "RUNNING")
        self.assertTrue(body["session"]["gate"]["allow"])
        self.assertIn("key_hold", body["kinds"])
        self.assertEqual(body["held"], [])
        self.assertEqual(body["history"], [])

    def test_a_control_action_completes_without_an_effect_claim(self) -> None:
        session = self._validated_session(max_actions=8)
        path = self._control(session["session_id"])
        response = self._client.post(path, json={"kind": "key_press", "key": "w"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed", body)
        self.assertFalse(body["effect_verified"])
        self.assertIsNone(body["result"]["effect"])
        self.assertTrue(body["result"]["dispatched_at"])
        self.assertEqual(body["remaining_actions"], 7)

        held = self._client.get(path).json()
        self.assertEqual(held["held"], [{"kind": "key", "name": "w"}])
        self.assertEqual(len(held["history"]), 1)

        released = self._client.post(path + "/release").json()
        self.assertTrue(released["cleanup"]["ok"], released)
        self.assertEqual(released["held"], [])
        self.assertEqual(self.input.presses, ["key:w"])
        self.assertEqual(self.input.releases, ["key:w"])

    def test_a_wait_completes_without_dispatching_anything(self) -> None:
        session = self._validated_session()
        response = self._client.post(
            self._control(session["session_id"]), json={"kind": "wait", "duration_ms": 20}
        )
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["result"]["dispatched_at"], "")
        self.assertEqual(self.input.presses, [])

    def test_a_policy_refusal_is_a_200_with_its_code_and_sends_nothing(self) -> None:
        session = self._validated_session()
        response = self._client.post(
            self._control(session["session_id"]),
            json={"kind": "key_hold", "key": "w", "duration_ms": 10_000},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["code"], "policy")
        self.assertIn("between 1 and 1500 ms", body["reason"])
        self.assertEqual(self.input.presses, [])

        malformed = self._client.post(
            self._control(session["session_id"]), json={"kind": "teleport"}
        )
        self.assertEqual(malformed.status_code, 422)
        self.assertIn("Unknown control kind", malformed.json()["detail"])

    def test_cancel_with_nothing_in_flight_is_an_honest_no_op(self) -> None:
        session = self._validated_session()
        body = self._client.post(self._control(session["session_id"]) + "/cancel").json()
        self.assertFalse(body["cancelled"])
        self.assertIn("nothing was cancelled", body["reason"])

    def test_capabilities_expose_the_control_row_without_an_effect_claim(self) -> None:
        body = self._client.get("/novaforge/capabilities").json()["capabilities"]
        control = body["game_control"]
        self.assertTrue(control["available"])
        self.assertFalse(control["effect_verified"])
        self.assertTrue(control["target_verified_before_dispatch"])
        self.assertEqual(control["policy"]["allowed_buttons"], ["left"])
