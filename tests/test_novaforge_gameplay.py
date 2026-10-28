"""NovaForge gameplay remediation tests: typed actions, enforcement, verification.

Three layers, each failing for its own reason:

* **Pure** — action parsing/validation and the approval digest are asserted as
  code, so a widened policy or a digest that ignores target identity fails here.
* **Runner** — the real ``ActionRunner``/``InputGuard`` against a recording
  injector: press/release parity, release on exception and cancellation, release
  when a stop interrupts a hold, and a refusal before injecting outside the
  client area.
* **Manager** — the real manager with fake desktop seams: a proposal sends
  nothing, a mismatched digest sends nothing, a lost/changed target is refused
  AND pauses the session, the budget is enforced at execution, and only a
  verified objective reaches COMPLETED.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image

from novacontrol.desktop.input import WindowGeometry
from novacontrol.novaforge import (
    ActionKind,
    ActionPolicy,
    ActionRunner,
    ActionStatus,
    GameplayAction,
    MissionValidationError,
    NovaForgeManager,
    ObjectiveDefinition,
    ObjectiveKind,
    ObjectiveOutcome,
    ObjectiveRegistry,
    OcrTextVerifier,
    ScreenInput,
    SessionState,
    WindowTitleVerifier,
    action_digest,
)
from novacontrol.novaforge.objective import ObjectiveVerification

WINDOW_TITLE = "NC FORGE HARNESS - idle"
HANDLE = 0x1234
PID = 4242


def geometry(*, title: str = WINDOW_TITLE, handle: int = HANDLE, pid: int = PID,
             foreground: bool = True) -> WindowGeometry:
    """The same shape the capture worker's ``describe`` produces."""
    return WindowGeometry(
        handle=handle,
        title=title,
        bounds=(100, 50, 1100, 650),
        client=(0, 0, 1000, 600),
        pid=pid,
        scale=1.0,
        foreground=foreground,
    )


@dataclass
class RecordingInput:
    """Records every edge a runner sends, and can be told to fail."""

    kind: str = ""
    fail_press: bool = False
    fail_release: bool = False
    presses: list[str] | None = None
    releases: list[str] | None = None
    moves: list[tuple[int, int]] | None = None

    def __post_init__(self) -> None:
        self.presses = []
        self.releases = []
        self.moves = []

    async def press(self, kind: str, name: str) -> None:
        if self.fail_press:
            raise RuntimeError("injector refused the press")
        self.presses.append(f"{kind}:{name}")

    async def release(self, kind: str, name: str) -> None:
        if self.fail_release:
            raise RuntimeError("injector refused the release")
        self.releases.append(f"{kind}:{name}")

    async def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def held(self) -> int:
        return len(self.presses) - len(self.releases)


# ── Pure: parsing, policy and the approval digest ──────────────────────────


class GameplayActionValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ActionPolicy()

    def test_unknown_kind_and_missing_coordinates_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            GameplayAction.from_payload({"kind": "teleport"})
        with self.assertRaises(ValueError):
            GameplayAction.from_payload({})
        click = GameplayAction.from_payload({"kind": "click", "x": 10})
        self.assertEqual(click.validate(self.policy), ["A click needs both x and y coordinates."])

    def test_system_keys_and_unlisted_buttons_are_outside_the_policy(self) -> None:
        alt_f4 = GameplayAction.from_payload({"kind": "key", "key": "alt"})
        self.assertTrue(
            any("not in the gameplay policy" in problem for problem in alt_f4.validate(self.policy))
        )
        right_click = GameplayAction.from_payload({"kind": "click", "x": 10, "y": 10, "button": "right"})
        self.assertTrue(
            any("button is not in the gameplay policy" in problem for problem in right_click.validate(self.policy))
        )
        unknown = GameplayAction.from_payload({"kind": "key", "key": "f13-magic"})
        self.assertTrue(unknown.validate(self.policy))

    def test_hold_duration_is_bounded_by_policy(self) -> None:
        long_hold = GameplayAction.from_payload({"kind": "hold", "key": "w", "duration_ms": 5000})
        problems = long_hold.validate(self.policy)
        self.assertTrue(any("between 1 and 2000 ms" in problem for problem in problems), problems)
        allowed = GameplayAction.from_payload({"kind": "hold", "key": "w", "duration_ms": 400})
        self.assertEqual(allowed.validate(self.policy), [])

    def test_digest_covers_session_action_and_target_identity(self) -> None:
        action = GameplayAction.from_payload({"kind": "key", "key": "w"})
        base = action_digest("session-a", action, geometry())
        self.assertEqual(base, action_digest("session-a", action, geometry()))
        self.assertNotEqual(base, action_digest("session-b", action, geometry()))
        self.assertNotEqual(base, action_digest("session-a", action, geometry(handle=0x9999)))
        self.assertNotEqual(base, action_digest("session-a", action, geometry(pid=7)))
        other = GameplayAction.from_payload({"kind": "key", "key": "s"})
        self.assertNotEqual(base, action_digest("session-a", other, geometry()))

    def test_policy_defaults_are_tight(self) -> None:
        policy = ActionPolicy()
        self.assertEqual(policy.allowed_buttons, frozenset({"left"}))
        self.assertEqual(policy.max_hold_ms, 2000)
        self.assertNotIn("alt", policy.allowed_keys)
        self.assertNotIn("f4", policy.allowed_keys)

    def test_screen_input_speaks_the_worker_protocol(self) -> None:
        sent: list[str] = []

        class Result:
            ok = True
            error = ""

        async def send(command: str) -> Result:
            sent.append(command)
            return Result()

        input = ScreenInput(send=send)
        asyncio.run(input.press("key", "w"))
        asyncio.run(input.release("key", "w"))
        asyncio.run(input.press("button", "left"))
        asyncio.run(input.release("button", "left"))
        asyncio.run(input.move(12, 34))
        self.assertEqual(sent, ["key 87 1", "key 87 0", "button 2", "button 4", "move 12 34"])


# ── Runner: the invariant that survives every exit path ────────────────────


class ActionRunnerTests(unittest.TestCase):
    def run_action(self, action: GameplayAction, input: RecordingInput,
                   policy: ActionPolicy | None = None) -> Any:
        runner = ActionRunner(input=input)
        return asyncio.run(runner.run(action, geometry(), policy or ActionPolicy()))

    def test_key_tap_presses_once_and_releases_once(self) -> None:
        input = RecordingInput()
        outcome = self.run_action(GameplayAction.from_payload({"kind": "key", "key": "w"}), input)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(input.presses, ["key:w"])
        self.assertEqual(input.releases, ["key:w"])
        self.assertEqual(outcome.held_after, 0)

    def test_click_moves_then_clicks_in_the_client_area(self) -> None:
        input = RecordingInput()
        outcome = self.run_action(
            GameplayAction.from_payload({"kind": "click", "x": 500, "y": 300}), input
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(input.moves, [(500, 300)])
        self.assertEqual(input.presses, ["button:left"])
        self.assertEqual(input.releases, ["button:left"])

    def test_a_click_outside_the_client_area_sends_nothing(self) -> None:
        input = RecordingInput()
        outcome = self.run_action(
            GameplayAction.from_payload({"kind": "click", "x": 4000, "y": 300}), input
        )
        self.assertFalse(outcome.ok)
        self.assertIs(outcome.status, ActionStatus.FAILED)
        self.assertEqual(input.moves, [])
        self.assertEqual(input.presses, [])

    def test_a_failed_press_is_not_recorded_as_held(self) -> None:
        input = RecordingInput(fail_press=True)
        outcome = self.run_action(GameplayAction.from_payload({"kind": "hold", "key": "w", "duration_ms": 50}), input)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.presses, 0)
        self.assertEqual(outcome.held_after, 0)

    def test_a_failed_release_is_reported_not_swallowed(self) -> None:
        input = RecordingInput(fail_release=True)
        outcome = self.run_action(GameplayAction.from_payload({"kind": "key", "key": "w"}), input)
        self.assertFalse(outcome.ok)
        self.assertNotEqual(outcome.held_after, 0)
        self.assertIn("release", outcome.reason.lower())

    def test_a_stop_request_interrupts_a_hold_and_releases_it(self) -> None:
        input = RecordingInput()
        runner = ActionRunner(input=input)
        holder = GameplayAction.from_payload({"kind": "hold", "key": "w", "duration_ms": 1500})

        async def scenario() -> Any:
            task = asyncio.create_task(runner.run(holder, geometry(), ActionPolicy()))
            await asyncio.sleep(0.05)
            runner.abort.stop()
            return await task

        outcome = asyncio.run(scenario())
        self.assertIs(outcome.status, ActionStatus.ABORTED)
        self.assertIn("key:w", input.presses)
        self.assertEqual(input.presses, input.releases, "a held key survived the stop")
        self.assertEqual(outcome.held_after, 0)

    def test_a_stop_is_one_shot_and_a_reset_rearms_the_runner(self) -> None:
        """A stop request is about ONE action. Left set, it aborts every later
        action of the session (and of the manager's next session, since one
        runner serves them all) with no way to clear it.
        """
        input = RecordingInput()
        runner = ActionRunner(input=input)
        tap = GameplayAction.from_payload({"kind": "key", "key": "w"})
        holder = GameplayAction.from_payload({"kind": "hold", "key": "w", "duration_ms": 1500})

        async def scenario() -> tuple[Any, Any, Any]:
            task = asyncio.create_task(runner.run(holder, geometry(), ActionPolicy()))
            await asyncio.sleep(0.05)
            runner.abort.stop()
            first = await task
            still_stopped = await runner.run(tap, geometry(), ActionPolicy())
            runner.reset_abort()
            rearmed = await runner.run(tap, geometry(), ActionPolicy())
            return first, still_stopped, rearmed

        first, still_stopped, rearmed = asyncio.run(scenario())
        self.assertIs(first.status, ActionStatus.ABORTED)
        self.assertIs(still_stopped.status, ActionStatus.ABORTED)
        self.assertEqual(still_stopped.presses, 0)
        self.assertIs(rearmed.status, ActionStatus.EXECUTED, rearmed.error)
        self.assertEqual(input.presses, ["key:w", "key:w"])
        self.assertEqual(input.presses, input.releases)

    def test_cancellation_releases_before_propagating(self) -> None:
        input = RecordingInput()
        runner = ActionRunner(input=input)
        holder = GameplayAction.from_payload({"kind": "hold", "key": "w", "duration_ms": 1500})

        async def scenario() -> None:
            task = asyncio.create_task(runner.run(holder, geometry(), ActionPolicy()))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertEqual(input.presses, ["key:w"])
        self.assertEqual(input.releases, ["key:w"], "cancellation skipped the release")


# ── Objective verification: independent, and honest when it cannot decide ──


class WindowTitleVerifierTests(unittest.TestCase):
    def verifier(self, window: Any) -> WindowTitleVerifier:
        async def probe(_handle: int) -> Any:
            if isinstance(window, Exception):
                raise window
            return window

        return WindowTitleVerifier(probe=probe)

    def check(self, definition: ObjectiveDefinition, window: Any) -> ObjectiveVerification:
        return asyncio.run(
            self.verifier(window).verify(definition, handle=HANDLE, pid=PID, session_id="s1")
        )

    def test_success_and_failure_are_decided_from_the_window(self) -> None:
        success = self.check(
            ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "ready"), geometry(title=f"{WINDOW_TITLE} - READY")
        )
        self.assertIs(success.outcome, ObjectiveOutcome.VERIFIED_SUCCESS)
        self.assertIs(success.achieved, True)
        self.assertEqual(success.evidence[0].window_title, f"{WINDOW_TITLE} - READY")
        failure = self.check(ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "ready"), geometry())
        self.assertIs(failure.outcome, ObjectiveOutcome.VERIFIED_FAILURE)
        self.assertIs(failure.achieved, False)

    def test_equals_and_absent_modes(self) -> None:
        exact = self.check(
            ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "Ready", mode="equals"),
            geometry(title="ready"),
        )
        self.assertIs(exact.achieved, True)
        absent = self.check(
            ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "error", mode="absent"), geometry()
        )
        self.assertIs(absent.achieved, True)

    def test_a_missing_window_is_unknown_not_success(self) -> None:
        unknown = self.check(ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "ready"), None)
        self.assertIs(unknown.outcome, ObjectiveOutcome.UNKNOWN)
        self.assertIsNone(unknown.achieved)

    def test_a_recycled_handle_is_unknown(self) -> None:
        result = self.check(
            ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "ready"), geometry(handle=0x9999, title="ready")
        )
        self.assertIs(result.outcome, ObjectiveOutcome.UNKNOWN)
        self.assertIsNone(result.achieved)

    def test_a_changed_process_is_unknown(self) -> None:
        result = self.check(
            ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "ready"), geometry(pid=7, title="ready")
        )
        self.assertIs(result.outcome, ObjectiveOutcome.UNKNOWN)

    def test_a_raising_probe_is_a_verification_error(self) -> None:
        result = self.check(ObjectiveDefinition(ObjectiveKind.WINDOW_TITLE, "ready"), RuntimeError("probe died"))
        self.assertIs(result.outcome, ObjectiveOutcome.VERIFICATION_ERROR)
        self.assertIsNone(result.achieved)

    def test_registry_reports_unsupported_kinds_honestly(self) -> None:
        registry = ObjectiveRegistry(verifiers=(self.verifier(geometry()),))
        unsupported = ObjectiveDefinition(ObjectiveKind.UNSUPPORTED)
        self.assertIsNone(registry.verifier_for(unsupported))
        verdict = registry.unavailable(unsupported)
        self.assertIs(verdict.outcome, ObjectiveOutcome.UNSUPPORTED)
        self.assertIsNone(verdict.achieved)
        kinds = {row["kind"]: row["available"] for row in registry.availability()}
        self.assertTrue(kinds["window_title"])
        self.assertFalse(kinds["unsupported"])


# ── OCR verification: read the screen, and never invent a confidence ────────


def ocr_frame(**overrides: Any) -> dict[str, Any]:
    """The reading an OCR frame probe returns, with honest defaults.

    Windows' OCR exposes words but no per-word confidence, so the default is
    ``confidence: None`` with ``confidence_reported: false`` — the shape the
    real probe produces.
    """
    frame: dict[str, Any] = {
        "available": True,
        "fresh": True,
        "captured_at": "2026-10-21T10:00:00+00:00",
        "age_seconds": 0.4,
        "path": "C:/frames/000001.png",
        "text": "RECEIVED 1 KEY(S)",
        "words": [
            {"text": "RECEIVED", "line": 0, "x": 10, "y": 10, "width": 80, "height": 20},
            {"text": "1", "line": 0, "x": 95, "y": 10, "width": 10, "height": 20},
            {"text": "KEY(S)", "line": 0, "x": 110, "y": 10, "width": 55, "height": 20},
        ],
        "confidence": None,
        "confidence_reported": False,
        "engine": "windows.media.ocr",
        "reason": "",
    }
    frame.update(overrides)
    return frame


class OcrTextVerifierTests(unittest.TestCase):
    def verifier(self, reading: Any) -> OcrTextVerifier:
        async def probe(_handle: int) -> Any:
            if isinstance(reading, Exception):
                raise reading
            return reading

        return OcrTextVerifier(probe=probe)

    def check(
        self, definition: ObjectiveDefinition, reading: Any
    ) -> ObjectiveVerification:
        return asyncio.run(self.verifier(reading).verify(definition, handle=HANDLE, pid=PID))

    def objective(self, **payload: Any) -> ObjectiveDefinition:
        return ObjectiveDefinition.from_payload({"kind": "ocr_text", **payload})

    def test_recognised_text_decides_success_and_failure(self) -> None:
        success = self.check(self.objective(parameter="received 1 key"), ocr_frame())
        self.assertIs(success.outcome, ObjectiveOutcome.VERIFIED_SUCCESS)
        self.assertIs(success.achieved, True)
        self.assertIsNone(success.confidence, "the engine reported none; do not invent one")
        self.assertEqual(success.evidence[0].method, "ocr_text")
        failure = self.check(self.objective(parameter="LEVEL COMPLETE"), ocr_frame())
        self.assertIs(failure.outcome, ObjectiveOutcome.VERIFIED_FAILURE)
        self.assertIs(failure.achieved, False)

    def test_equals_and_absent_modes(self) -> None:
        exact = self.check(
            self.objective(parameter="Received 1 Key(S)", mode="equals"), ocr_frame()
        )
        self.assertIs(exact.achieved, True)
        absent = self.check(
            self.objective(parameter="GAME OVER", mode="absent"), ocr_frame()
        )
        self.assertIs(absent.achieved, True)
        still_there = self.check(
            self.objective(parameter="KEY(S)", mode="absent"), ocr_frame()
        )
        self.assertIs(still_there.achieved, False)

    def test_a_stale_frame_is_unknown(self) -> None:
        result = self.check(self.objective(parameter="received"), ocr_frame(age_seconds=30.0))
        self.assertIs(result.outcome, ObjectiveOutcome.UNKNOWN)
        self.assertIsNone(result.achieved)
        unfresh = self.check(self.objective(parameter="received"), ocr_frame(fresh=False))
        self.assertIs(unfresh.outcome, ObjectiveOutcome.UNKNOWN)

    def test_an_unavailable_engine_is_unsupported(self) -> None:
        result = self.check(
            self.objective(parameter="received"),
            ocr_frame(available=False, reason="no OCR language pack"),
        )
        self.assertIs(result.outcome, ObjectiveOutcome.UNSUPPORTED)
        self.assertIn("no OCR language pack", result.reason)

    def test_a_low_confidence_reading_never_decides(self) -> None:
        result = self.check(
            self.objective(parameter="received"),
            ocr_frame(confidence=0.2, confidence_reported=True),
        )
        self.assertIs(result.outcome, ObjectiveOutcome.UNKNOWN)
        self.assertIsNone(result.achieved)

    def test_absence_is_unknown_when_nothing_was_read_and_nothing_was_measured(self) -> None:
        result = self.check(
            self.objective(parameter="GAME OVER", mode="absent"),
            ocr_frame(text="", words=[]),
        )
        self.assertIs(result.outcome, ObjectiveOutcome.UNKNOWN)
        self.assertIsNone(result.achieved)

    def test_a_region_bounds_the_evidence(self) -> None:
        frame = ocr_frame(
            words=[
                {"text": "READY", "line": 0, "x": 400, "y": 400, "width": 40, "height": 10},
                {"text": "ELSEWHERE", "line": 1, "x": 5, "y": 5, "width": 60, "height": 10},
            ]
        )
        inside = self.check(
            self.objective(parameter="READY", region="350,350,200,100"), frame
        )
        self.assertIs(inside.achieved, True)
        outside = self.check(
            self.objective(parameter="READY", region="0,0,100,50"), frame
        )
        self.assertIs(outside.achieved, False)

    def test_a_raising_or_silent_probe_is_unknown(self) -> None:
        raised = self.check(self.objective(parameter="received"), RuntimeError("OCR died"))
        self.assertIs(raised.outcome, ObjectiveOutcome.UNKNOWN)
        silent = self.check(self.objective(parameter="received"), None)
        self.assertIs(silent.outcome, ObjectiveOutcome.UNKNOWN)

    def test_registry_reports_ocr_as_available_only_with_a_probe(self) -> None:
        registry = ObjectiveRegistry(verifiers=(self.verifier(ocr_frame()),))
        kinds = {row["kind"]: row for row in registry.availability()}
        self.assertTrue(kinds["ocr_text"]["available"])
        self.assertIn("OcrTextVerifier", kinds["ocr_text"]["verifier"])
        self.assertEqual(registry.available_kinds(), ["ocr_text"])


# ── Manager: enforcement at execution, not at display ──────────────────────

FakeWindow = WindowGeometry


class FakeDesktop:
    """Deterministic capture/geometry double; writes a real PNG per frame."""

    def __init__(self) -> None:
        self.window: WindowGeometry | None = geometry()
        self.captures = 0

    async def capture(self, path: str) -> dict[str, Any]:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 800), (12, 18, 30)).save(path, format="PNG")
        self.captures += 1
        return {"ok": True, "path": path, "fresh": True, "width": 1000, "height": 600,
                "elapsed_ms": 1.0, "size_bytes": 72, "source": "window_crop", "mode": "window"}

    async def window_lines(self) -> list[str] | None:
        # The picker/validate format: "process|Title", one entry per window.
        return [f"python.exe|{WINDOW_TITLE}"]

    async def geometry_for(self, _title: str) -> WindowGeometry | None:
        return self.window


class ManagerGameplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.desktop = FakeDesktop()
        self.input = RecordingInput()
        self.probe_window: Any = geometry()

        async def probe(_handle: int) -> Any:
            return self.probe_window

        self.manager = NovaForgeManager(
            data_dir=Path(self.temp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry_for,
            window_lines=self.desktop.window_lines,
            release_inputs=self._release,
            action_input=self.input,
            objective_probe=probe,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=1.0,
            stop_timeout=2.0,
        )
        self.record = None

    async def _release(self) -> tuple[int, int]:
        return (len(self.input.presses), len(self.input.releases))

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    async def start(self, **mission: Any) -> Any:
        payload = {"name": "Probe", "objective": "Observe the harness", "target_title": WINDOW_TITLE,
                   "frame_interval_seconds": 0.05, **mission}
        described = await self.manager.start(payload)
        self.record = self.manager.get(described["session_id"])
        # Let the loop validate the target identity for THIS run before acting,
        # exactly as a real session must.
        for _ in range(40):
            if self.record is not None and self.record.target_validated:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.record is not None and self.record.target_validated)
        return self.record

    async def plan(self, action: dict[str, Any]) -> dict[str, Any]:
        return self.manager.plan_action(self.record.session_id, action)

    async def test_a_retitled_window_is_the_same_target_not_a_lost_one(self) -> None:
        """The objective this build verifies is met BY a title change.

        The observation loop used to resolve its target through the TITLE it
        pinned, so the harness retitling itself to ``... READY`` — exactly what
        meeting a ``window_title`` objective looks like — read as "the target
        window closed": the session paused one tick after the action that met
        the objective and could never reach COMPLETED. Identity is handle + pid;
        the label has to be allowed to change.
        """
        await self.start()
        # A title lookup that RESOLVES BY TITLE, as the production seam does: it
        # cannot find the window once the label changes, which is exactly the
        # condition the handle probe has to survive.
        desktop = self.desktop

        async def by_title(title: str) -> Any:
            window = desktop.window
            if window is None or title.lower() not in window.title.lower():
                return None
            return window

        self.manager.geometry = by_title
        retitled = "NC FORGE HARNESS - READY"
        # The app retitles itself: the same handle and pid, a new label.
        self.desktop.window = geometry(title=retitled)
        self.probe_window = geometry(title=retitled)
        await asyncio.sleep(0.3)

        self.assertFalse(self.record.pause_reason, self.record.pause_reason)
        self.assertEqual(self.record.state, SessionState.RUNNING)
        self.assertEqual(self.record.target.title, retitled)
        self.assertTrue(self.record.target_validated)

    async def test_a_verified_objective_completes_a_paused_session(self) -> None:
        """A decided verdict is about the window, not about the loop's state.

        A session the loop had paused (an operator pause, or a window it briefly
        could not measure) has still met an objective the verifier can read, and
        PAUSED -> STOPPING -> COMPLETED is a legal close. Leaving it open would
        record ``achieved: true`` on a session that never completed.
        """
        await self.start()
        self.manager.set_objective(
            self.record.session_id, {"kind": "window_title", "parameter": "READY"}
        )
        await self.manager.pause(self.record.session_id)
        self.assertEqual(self.record.state, SessionState.PAUSED)
        self.probe_window = geometry(title="NC FORGE HARNESS - READY")

        verdict = await self.manager.verify_objective(self.record.session_id)

        self.assertEqual(verdict["outcome"], ObjectiveOutcome.VERIFIED_SUCCESS.value)
        self.assertEqual(self.record.state, SessionState.COMPLETED)
        self.assertEqual(self.record.end_reason, "objective_verified")

    async def test_a_completed_session_is_not_rewritten_by_the_time_limit(self) -> None:
        """Closing on a verified objective ends the run; the limit is for open runs.

        The loop outlived the finalize that ``verify_objective`` performs, and
        because finalize clears the monotonic origin the time limit then fired on
        the FINISHED record: ``end_reason`` became ``time_limit`` and an error
        said the objective was NOT verified, on a session whose objective had
        just been verified. Both halves are pinned here.
        """
        await self.start(time_limit_seconds=1)
        self.manager.set_objective(
            self.record.session_id, {"kind": "window_title", "parameter": "READY"}
        )
        self.probe_window = geometry(title="NC FORGE HARNESS - READY")
        verdict = await self.manager.verify_objective(self.record.session_id)
        self.assertEqual(verdict["outcome"], ObjectiveOutcome.VERIFIED_SUCCESS.value)
        self.assertEqual(self.record.state, SessionState.COMPLETED)

        await asyncio.sleep(1.6)  # well past the configured limit

        self.assertEqual(self.record.state, SessionState.COMPLETED)
        self.assertEqual(self.record.end_reason, "objective_verified")
        self.assertFalse(
            [
                error
                for error in self.record.errors
                if "NOT verified" in str(error.get("message", ""))
            ],
            self.record.errors,
        )

    async def test_a_proposal_sends_nothing_and_a_matching_digest_executes(self) -> None:
        record = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(self.input.presses, [], "planning injected input")
        proposal = plan["proposal"]
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]}
        )
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(self.input.presses, ["key:w"])
        self.assertEqual(self.input.releases, ["key:w"])
        self.assertEqual(record.actions_executed, 1)

    async def test_a_mismatched_digest_is_denied_and_sends_nothing(self) -> None:
        record = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        proposal = plan["proposal"]
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": proposal["proposal_id"], "digest": "0" * 64}
        )
        self.assertEqual(result["status"], "denied")
        self.assertIn("approval", result["outcome"]["reason"].lower())
        self.assertEqual(self.input.presses, [])
        self.assertEqual(record.actions_executed, 0)

    async def test_the_budget_is_enforced_at_execution(self) -> None:
        record = await self.start(max_actions=1)
        first = await self.plan({"kind": "key", "key": "w"})
        await self.manager.execute_action(
            record.session_id, {"proposal_id": first["proposal"]["proposal_id"],
                                "digest": first["proposal"]["digest"]}
        )
        self.assertEqual(record.actions_executed, 1)
        denied = await self.plan({"kind": "key", "key": "w"})
        self.assertEqual(denied["status"], "denied")
        self.assertIn("budget", denied["outcome"]["reason"].lower())
        self.assertEqual(self.input.presses, ["key:w"])

    async def test_a_guardian_cancel_interrupts_an_in_flight_hold(self) -> None:
        """Emergency controls are useless if they queue behind the action.

        ``execute_action`` holds the manager lock for the whole injection, so a
        cancel that only sets its flag after taking that lock would let a held
        key run to the end of its duration before anything stopped it.
        """
        record = await self.start(max_actions=5)
        plan = await self.plan({"kind": "hold", "key": "w", "duration_ms": 1500})
        proposal = plan["proposal"]
        task = asyncio.create_task(
            self.manager.execute_action(
                record.session_id, {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]}
            )
        )
        await asyncio.sleep(0.1)
        await self.manager.cancel(record.session_id)
        stopped = await task
        outcome = stopped["outcome"]
        self.assertIn(outcome["status"], {"aborted", "failed"})
        self.assertEqual(outcome["held_after"], 0)
        self.assertLess(outcome["elapsed_ms"], 1400, "the cancel waited the hold out")
        self.assertEqual(self.input.presses, self.input.releases, "a key survived the cancel")
        self.assertIs(record.state, SessionState.STOPPED)

    async def test_input_cleanup_counts_only_this_sessions_presses(self) -> None:
        """The runner outlives the session, so its totals must be cleared.

        A session that tapped once reported "2 / 2" in its cleanup after an
        earlier session had tapped once too: a lifetime total wearing a
        per-session label, in the one place a person looks to confirm nothing
        is held.
        """
        first = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        await self.manager.execute_action(
            first.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                               "digest": plan["proposal"]["digest"]}
        )
        plan = await self.plan({"kind": "key", "key": "w"})
        await self.manager.execute_action(
            first.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                               "digest": plan["proposal"]["digest"]}
        )
        await self.manager.stop(first.session_id)
        self.assertEqual(first.input_cleanup["presses"], 2, first.input_cleanup)

        second = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        await self.manager.execute_action(
            second.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        await self.manager.stop(second.session_id)
        self.assertEqual(second.input_cleanup["presses"], 1, second.input_cleanup)
        self.assertEqual(second.input_cleanup["releases"], 1, second.input_cleanup)
        self.assertEqual(second.input_cleanup["held_after"], 0)

    async def test_an_earlier_stop_does_not_block_the_next_session(self) -> None:
        record = await self.start()
        await self.manager.cancel(record.session_id)
        self.assertIs(record.state, SessionState.STOPPED)
        # One runner serves every session, so the stop must not outlive the run
        # it interrupted: a fresh session has to be able to act.
        fresh = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        proposal = plan["proposal"]
        result = await self.manager.execute_action(
            fresh.session_id, {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]}
        )
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual(self.input.presses, ["key:w"])

    async def test_a_lost_target_pauses_the_session_and_refuses_the_action(self) -> None:
        record = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        self.probe_window = None
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["outcome"]["reason"][:52].startswith("The target window could not be read"), True)
        self.assertEqual(self.input.presses, [])
        self.assertIs(record.state, SessionState.PAUSED)
        self.assertEqual(record.pause_reason, "target_lost")

    async def test_a_changed_handle_pauses_and_is_refused(self) -> None:
        record = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        self.probe_window = geometry(handle=0x9999)
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        self.assertEqual(result["status"], "denied")
        self.assertIn("changed", result["outcome"]["reason"])
        self.assertEqual(self.input.presses, [])
        self.assertIs(record.state, SessionState.PAUSED)

    async def test_an_unfocused_target_is_refused(self) -> None:
        record = await self.start()
        plan = await self.plan({"kind": "key", "key": "w"})
        self.probe_window = geometry(foreground=False)
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        self.assertEqual(result["status"], "denied")
        self.assertIn("foreground", result["outcome"]["reason"])
        self.assertEqual(self.input.presses, [])

    async def test_verified_success_completes_the_session(self) -> None:
        record = await self.start()
        self.manager.set_objective(
            record.session_id, {"kind": "window_title", "parameter": "READY"}
        )
        self.probe_window = geometry(title=f"{WINDOW_TITLE} - READY")
        plan = await self.plan({"kind": "key", "key": "w"})
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        self.assertEqual(result["status"], "executed")
        verification = result["verification"]
        self.assertEqual(verification["outcome"], "verified_success")
        self.assertIs(verification["achieved"], True)
        self.assertTrue(verification["evidence"][0]["window_title"].endswith("READY"))
        self.assertIs(record.state, SessionState.COMPLETED)
        self.assertEqual(record.end_reason, "objective_verified")

    async def test_an_unmet_objective_does_not_complete_and_is_not_success(self) -> None:
        record = await self.start()
        self.manager.set_objective(record.session_id, {"kind": "window_title", "parameter": "READY"})
        plan = await self.plan({"kind": "key", "key": "w"})
        result = await self.manager.execute_action(
            record.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        self.assertEqual(result["verification"]["outcome"], "verified_failure")
        self.assertIs(result["verification"]["achieved"], False)
        self.assertIsNot(record.state, SessionState.COMPLETED)

    async def test_no_objective_means_no_verdict(self) -> None:
        record = await self.start()
        verdict = await self.manager.verify_objective(record.session_id)
        self.assertEqual(verdict["outcome"], "unsupported")
        self.assertIsNone(verdict["achieved"])
        self.assertIsNone(verdict["definition"])

    async def test_an_unsupported_objective_kind_is_recorded_and_reported(self) -> None:
        record = await self.start()
        configured = self.manager.set_objective(record.session_id, {"kind": "unsupported", "description": "score > 10"})
        self.assertFalse(configured["supported"])
        verdict = await self.manager.verify_objective(record.session_id)
        self.assertEqual(verdict["outcome"], "unsupported")
        self.assertIsNone(verdict["achieved"])

    async def test_a_malformed_objective_is_refused(self) -> None:
        record = await self.start()
        with self.assertRaises(MissionValidationError):
            self.manager.set_objective(record.session_id, {"kind": "window_title"})
        with self.assertRaises(MissionValidationError):
            self.manager.set_objective(record.session_id, {"kind": "psychic"})

    async def test_the_chain_appears_in_the_timeline_and_the_stages(self) -> None:
        record = await self.start()
        self.manager.set_objective(record.session_id, {"kind": "window_title", "parameter": "READY"})
        self.probe_window = geometry(title=f"{WINDOW_TITLE} - READY")
        plan = await self.plan({"kind": "key", "key": "w"})
        await self.manager.execute_action(
            record.session_id, {"proposal_id": plan["proposal"]["proposal_id"],
                                "digest": plan["proposal"]["digest"]}
        )
        described = self.manager.describe(record, include_telemetry=False)
        stages = {stage["stage"]: stage for stage in described["stages"]}
        self.assertTrue(stages["observed"]["available"])
        self.assertTrue(stages["proposed"]["available"])
        self.assertTrue(stages["executed"]["available"])
        self.assertTrue(stages["verified"]["available"])
        self.assertEqual(
            stages["verified"]["records"][0]["outcome"], "verified_success"
        )
        timeline = self.manager.timeline_payload(record.session_id, limit=100)
        event_types = {event["type"] for event in timeline["events"]}
        for expected in ("objective.configured", "action.proposed", "action.executed", "objective.verified"):
            self.assertIn(expected, event_types)

    async def test_a_restored_session_keeps_its_objective_and_verdict(self) -> None:
        record = await self.start()
        self.manager.set_objective(record.session_id, {"kind": "window_title", "parameter": "READY"})
        verdict = await self.manager.verify_objective(record.session_id)
        self.assertEqual(verdict["outcome"], "verified_failure")
        await self.manager.shutdown()
        restored = NovaForgeManager(
            data_dir=Path(self.temp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry_for,
            window_lines=self.desktop.window_lines,
        )
        try:
            reloaded = restored.get(record.session_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.objective.parameter, "READY")
            self.assertEqual(reloaded.verification["outcome"], "verified_failure")
            self.assertIs(reloaded.to_dict()["actions"]["available"], False)
        finally:
            await restored.shutdown()

    def test_every_action_event_type_is_registered(self) -> None:
        """An unregistered type is DROPPED silently by ``_event`` — pin the table."""
        from novacontrol.novaforge.timeline import EVENT_SPECS

        for event_type in (
            "action.proposed", "action.executed", "action.denied",
            "action.failed", "action.aborted", "objective.configured",
            "objective.verified",
        ):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, EVENT_SPECS)

    async def test_capabilities_name_each_gap(self) -> None:
        capabilities = self.manager.capabilities()
        self.assertTrue(capabilities["action_execution"]["available"])
        self.assertTrue(capabilities["objective_verification"]["available"])
        self.assertFalse(capabilities["decision_pipeline"]["available"])
        self.assertFalse(capabilities["game_launch"]["available"])
        bare = NovaForgeManager(
            data_dir=Path(self.temp.name) / "bare",
            capture=self.desktop.capture,
            geometry=self.desktop.geometry_for,
            window_lines=self.desktop.window_lines,
        )
        try:
            self.assertFalse(bare.capabilities()["action_execution"]["available"])
            self.assertFalse(bare.capabilities()["objective_verification"]["available"])
        finally:
            await bare.shutdown()


if __name__ == "__main__":
    unittest.main()
