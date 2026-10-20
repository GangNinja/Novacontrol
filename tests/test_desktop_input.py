"""Phase C of the game-control audit: the input SAFETY contract.

These tests are pure Python — no screen, no Windows, no real input — because
the contract they pin is the one that keeps a stuck Shift or a held mouse
button from being left behind by a controller that crashed mid-action. The
guard is the only way input may be sent, so presses==releases is structural,
not a convention.
"""

from __future__ import annotations

import asyncio
import unittest

from novacontrol.desktop.capture import CaptureResult
from novacontrol.desktop.controller import LocalDesktopRunner
from novacontrol.desktop.input import (
    InputAbort,
    InputGuard,
    InputStop,
    MouseButton,
    WindowGeometry,
    client_to_screen,
    key_release_command,
    mouse_flags,
    normalize_key,
    scale_point,
    screen_to_client,
    validate_input_target,
    vk_for_key,
)
from novacontrol.desktop.models import DesktopAction, DesktopActionType


class _Recorder:
    """Records the press/release edges a guard emits, in order."""

    def __init__(self) -> None:
        self.edges: list[tuple[str, str]] = []
        self.fail_on_release: str | None = None

    async def press(self, kind: str, name: str) -> None:
        self.edges.append(("down", f"{kind}:{name}"))

    async def release(self, kind: str, name: str) -> None:
        if self.fail_on_release == f"{kind}:{name}":
            raise RuntimeError("release failed")
        self.edges.append(("up", f"{kind}:{name}"))


def _guard(recorder: _Recorder) -> InputGuard:
    return InputGuard(press=recorder.press, release=recorder.release)


class PressesEqualReleasesTests(unittest.IsolatedAsyncioTestCase):
    """The invariant: every press is matched by exactly one release."""

    async def test_normal_completion_releases_everything(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)
        async with guard:
            await guard.press_key("w")
            await guard.press_button(MouseButton.LEFT)
            await guard.press_key("shift")
        self.assertEqual(guard.presses, 3)
        self.assertEqual(guard.releases, 3)
        self.assertTrue(guard.presses_match_releases())
        self.assertEqual(guard.held, ())
        # Release order is newest-first (reverse of press).
        self.assertEqual(
            recorder.edges,
            [
                ("down", "key:w"), ("down", "button:left"), ("down", "key:shift"),
                ("up", "key:shift"), ("up", "button:left"), ("up", "key:w"),
            ],
        )

    async def test_exception_still_releases(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)
        with self.assertRaises(ValueError):
            async with guard:
                await guard.press_key("w")
                await guard.press_button("right")
                raise ValueError("the action failed halfway")
        self.assertEqual(guard.presses, 2)
        self.assertEqual(guard.releases, 2)
        self.assertTrue(guard.presses_match_releases())

    async def test_cancellation_still_releases(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)

        async def hold_forever() -> None:
            async with guard:
                await guard.press_key("w")
                await guard.press_button("left")
                await asyncio.sleep(60)  # the caller cancels the task here

        task = asyncio.create_task(hold_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The guard must have released on the cancellation's way out.
        self.assertEqual(guard.presses, 2)
        self.assertEqual(guard.releases, 2)
        self.assertTrue(guard.presses_match_releases())

    async def test_timeout_still_releases(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)

        async def hold() -> None:
            async with guard:
                await guard.press_key("w")
                await asyncio.sleep(60)

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(hold(), timeout=0.1)
        self.assertEqual(guard.presses, 1)
        self.assertEqual(guard.releases, 1)
        self.assertTrue(guard.presses_match_releases())

    async def test_release_all_continues_past_a_failed_release(self) -> None:
        """One stuck key must not stop the others from being freed."""
        recorder = _Recorder()
        recorder.fail_on_release = "key:shift"
        guard = _guard(recorder)
        await guard.press_key("w")
        await guard.press_key("shift")
        await guard.press_key("a")
        await guard.release_all()
        self.assertEqual(guard.releases, 2, "the two that could be released were")
        self.assertEqual(len(guard.release_failures), 1)
        self.assertIn("shift", guard.release_failures[0])
        self.assertFalse(guard.presses_match_releases(), "a failed release is reported, not hidden")

    async def test_a_press_that_failed_is_not_remembered_as_held(self) -> None:
        class _FailingPress(_Recorder):
            async def press(self, kind: str, name: str) -> None:
                raise RuntimeError("the press did not go out")

        recorder = _FailingPress()
        guard = _guard(recorder)
        with self.assertRaises(RuntimeError):
            await guard.press_key("w")
        self.assertEqual(guard.presses, 0)
        self.assertEqual(guard.held, (), "nothing was pressed, so nothing is held")
        # release_all must have nothing to do and must not raise.
        await guard.release_all()
        self.assertTrue(guard.presses_match_releases())


class InputAbortTests(unittest.IsolatedAsyncioTestCase):
    """A stop request interrupts in-flight input and still releases."""

    async def test_stop_raises_and_releases(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)
        async with guard:
            await guard.press_key("w")
            guard.abort.stop()
            with self.assertRaises(InputAbort):
                await guard.press_key("a")
            # The already-held W is released on the way out.
        self.assertEqual(guard.presses, 1)
        self.assertEqual(guard.releases, 1)
        self.assertTrue(guard.presses_match_releases())

    async def test_stop_is_reported_and_idempotent(self) -> None:
        stop = InputStop()
        self.assertFalse(stop.stopped)
        stop.stop()
        stop.stop()
        self.assertTrue(stop.stopped)
        with self.assertRaises(InputAbort):
            stop.check("moving the mouse")


class KeyAndButtonVocabularyTests(unittest.TestCase):
    """Unknown keys and buttons are refused, never guessed."""

    def test_known_keys_resolve_to_vcodes(self) -> None:
        self.assertEqual(vk_for_key("w"), 0x57)
        self.assertEqual(vk_for_key("Enter"), 0x0D)
        self.assertEqual(vk_for_key("ctrl"), 0x11)
        self.assertEqual(vk_for_key("F5"), 0x74)

    def test_key_names_are_normalized(self) -> None:
        self.assertEqual(normalize_key("<Enter>"), "enter")
        self.assertEqual(normalize_key("CTRL"), "ctrl")
        self.assertEqual(key_release_command("Control"), "control")
        self.assertEqual(key_release_command("page_up"), "pageup")

    def test_unknown_key_is_not_guessed(self) -> None:
        self.assertIsNone(vk_for_key("hyper"))
        self.assertIsNone(key_release_command("hyper"))

    def test_mouse_flags_pair_down_and_up(self) -> None:
        self.assertEqual(mouse_flags(MouseButton.LEFT, down=True), 0x0002)
        self.assertEqual(mouse_flags(MouseButton.LEFT, down=False), 0x0004)
        self.assertEqual(mouse_flags(MouseButton.RIGHT, down=True), 0x0008)
        self.assertEqual(mouse_flags(MouseButton.RIGHT, down=False), 0x0010)
        self.assertEqual(mouse_flags(MouseButton.MIDDLE, down=True), 0x0020)
        self.assertEqual(mouse_flags(MouseButton.MIDDLE, down=False), 0x0040)


class KeyAndButtonRefusalTests(unittest.IsolatedAsyncioTestCase):
    """Refusals are raised BEFORE any press goes out."""

    async def test_pressing_an_unknown_key_refuses(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)
        with self.assertRaises(ValueError):
            await guard.press_key("hyper")
        self.assertEqual(guard.presses, 0)
        self.assertEqual(recorder.edges, [])

    async def test_an_unknown_button_refuses(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)
        with self.assertRaises(ValueError):
            await guard.press_button("thumb")
        self.assertEqual(guard.presses, 0)

    async def test_releasing_something_not_held_refuses(self) -> None:
        recorder = _Recorder()
        guard = _guard(recorder)
        with self.assertRaises(ValueError):
            await guard.release_key("w")
        self.assertEqual(guard.releases, 0)


def _window(*, handle: int = 0x100, scale: float = 2.0, foreground: bool = True) -> WindowGeometry:
    """A 1920x1080 physical-pixel window with a 32px title bar, at 200% scaling."""
    return WindowGeometry(
        handle=handle,
        title="NovaGame - Steam",
        bounds=(0, 0, 1920, 1080),
        client=(0, 32, 1920, 1048),
        pid=4242,
        scale=scale,
        foreground=foreground,
    )


def _placed_window() -> WindowGeometry:
    """A real host's window: NOT at the screen origin (measured on this machine).

    The worker reports ``client`` as (origin_x, origin_y, width, height), which
    collapsed to the same thing as absolute edges only while ``bounds`` started
    at (0, 0) — the fixture above. This window starts at (440, 360), where
    reading width as an absolute right edge refused every point past x=840.
    """
    return WindowGeometry(
        handle=0x200,
        title="NC INPUT PROBE - disposable",
        bounds=(440, 360, 1280, 1000),
        client=(0, 0, 840, 640),
        pid=5150,
        scale=2.0,
        foreground=True,
    )


class CoordinateConversionTests(unittest.TestCase):
    """DPI-aware conversion between the space a coordinate was measured in and
    the space input is injected in."""

    def test_same_scale_is_identity(self) -> None:
        self.assertEqual(scale_point(960, 540, from_scale=1.0, to_scale=1.0), (960, 540))

    def test_200_percent_downscale(self) -> None:
        # A logical point at 200% maps to half the physical extent.
        self.assertEqual(scale_point(1920, 1080, from_scale=2.0, to_scale=1.0), (960, 540))

    def test_200_percent_upscale(self) -> None:
        self.assertEqual(scale_point(960, 540, from_scale=1.0, to_scale=2.0), (1920, 1080))

    def test_invalid_scale_refused(self) -> None:
        with self.assertRaises(ValueError):
            scale_point(1, 1, from_scale=0.0, to_scale=2.0)

    def test_client_and_screen_convert_back_and_forth(self) -> None:
        window = _window()
        screen = client_to_screen(10, 20, window)
        self.assertEqual(screen, (10, 52))  # client origin is (0, 32)
        self.assertEqual(screen_to_client(*screen, window), (10, 20))

    def test_geometry_reports_its_size(self) -> None:
        window = _window()
        self.assertEqual((window.width, window.height), (1920, 1080))
        self.assertEqual(window.client_origin, (0, 32))


class _FakeInputWorker:
    """Stands in for the DPI-aware worker: records input commands, never injects."""

    def __init__(self, geometry: WindowGeometry | None = None) -> None:
        self.geometry = geometry
        self.commands: list[str] = []
        self.closed = 0

    async def send_input(self, command: str, **_: object) -> CaptureResult:
        self.commands.append(command)
        return CaptureResult(True, detail="ok", persistent=True)

    async def window_geometry(
        self, *, title: str = "", foreground: bool = False, at: object = None,
        handle: int | None = None,
    ) -> WindowGeometry | None:
        return self.geometry

    async def aclose(self) -> None:
        self.closed += 1


def _game_window(*, foreground: bool = True, handle: int = 0x100) -> WindowGeometry:
    return WindowGeometry(
        handle=handle, title="NovaGame", bounds=(0, 0, 1920, 1080),
        client=(0, 32, 1920, 1080), pid=4242, scale=2.0, foreground=foreground,
    )


def _runner(worker: _FakeInputWorker) -> LocalDesktopRunner:
    return LocalDesktopRunner(screen_capture=worker)


class RunnerInputDispatchTests(unittest.IsolatedAsyncioTestCase):
    """The runner routes coordinate input through the guard and the worker."""

    async def test_click_pairs_a_press_and_a_release(self) -> None:
        worker = _FakeInputWorker(_game_window())
        runner = _runner(worker)
        result = dict(await runner.run(DesktopAction(
            type=DesktopActionType.MOUSE_CLICK, target="100,500",
            description="click", parameters={"x": 100, "y": 500, "window_title": "NovaGame"},
        )))
        self.assertEqual(result["action"], "mouse_click")
        self.assertEqual(result["window"], "NovaGame")
        # move + left-down + left-up, in that order.
        self.assertEqual(worker.commands, ["move 100 500", "button 2", "button 4"])
        guard = runner._input_guard
        self.assertIsNotNone(guard)
        self.assertTrue(guard.presses_match_releases())
        await runner.close()

    async def test_move_addresses_exact_coordinates(self) -> None:
        worker = _FakeInputWorker(_game_window())
        runner = _runner(worker)
        result = dict(await runner.run(DesktopAction(
            type=DesktopActionType.MOUSE_MOVE, target="40,60", description="move",
            parameters={"x": 40, "y": 60, "window_title": "NovaGame"},
        )))
        self.assertEqual(worker.commands, ["move 40 60"])
        self.assertEqual(result["coordinates"], [40, 60])

    async def test_key_down_then_key_up_match(self) -> None:
        worker = _FakeInputWorker()
        runner = _runner(worker)
        await runner.run(DesktopAction(
            type=DesktopActionType.KEY_DOWN, target="w", description="hold w", parameters={}))
        await runner.run(DesktopAction(
            type=DesktopActionType.KEY_UP, target="w", description="release w", parameters={}))
        self.assertEqual(worker.commands, ["key 87 1", "key 87 0"])
        guard = runner._input_guard
        self.assertTrue(guard.presses_match_releases())
        self.assertEqual(guard.held, ())

    async def test_close_releases_a_held_key(self) -> None:
        worker = _FakeInputWorker()
        runner = _runner(worker)
        await runner.run(DesktopAction(
            type=DesktopActionType.KEY_DOWN, target="shift", description="hold", parameters={}))
        await runner.run(DesktopAction(
            type=DesktopActionType.MOUSE_BUTTON_DOWN, target="left", description="hold",
            parameters={"button": "left"}))
        guard = runner._input_guard
        self.assertEqual(len(guard.held), 2)
        await runner.close()
        self.assertEqual(guard.held, ())
        self.assertTrue(guard.presses_match_releases())
        # Release order is newest-first: the button (pressed last) goes first.
        self.assertEqual(worker.commands, ["key 16 1", "button 2", "button 4", "key 16 0"])

    async def test_stop_input_releases_and_clears_the_flag(self) -> None:
        worker = _FakeInputWorker()
        runner = _runner(worker)
        await runner.run(DesktopAction(
            type=DesktopActionType.KEY_DOWN, target="w", description="hold", parameters={}))
        outcome = await runner.stop_input()
        self.assertTrue(outcome["aborted"])
        self.assertEqual(outcome["presses"], 1)
        self.assertEqual(outcome["releases"], 1)
        self.assertFalse(runner._input_guard.abort.stopped, "a stop is one-shot")
        # The runner is usable again after a stop: a fresh press/release pair works.
        await runner.run(DesktopAction(
            type=DesktopActionType.KEY_DOWN, target="a", description="press a", parameters={}))
        await runner.run(DesktopAction(
            type=DesktopActionType.KEY_UP, target="a", description="release a", parameters={}))
        self.assertTrue(runner._input_guard.presses_match_releases())

    async def test_a_click_into_a_foreign_window_is_refused(self) -> None:
        """Identity is checked: a window that is no longer the target refuses."""
        worker = _FakeInputWorker(_game_window(handle=0x200))
        runner = _runner(worker)
        with self.assertRaises(ValueError) as ctx:
            await runner.run(DesktopAction(
                type=DesktopActionType.MOUSE_CLICK, target="100,500", description="click",
                parameters={"x": 100, "y": 500, "window_handle": 0x100},
            ))
        self.assertIn("identity changed", str(ctx.exception))
        self.assertEqual(worker.commands, [], "no input is sent to a foreign window")

    async def test_a_click_outside_the_client_area_is_refused(self) -> None:
        worker = _FakeInputWorker(_game_window())
        runner = _runner(worker)
        with self.assertRaises(ValueError):
            await runner.run(DesktopAction(
                type=DesktopActionType.MOUSE_CLICK, target="5000,5000", description="click",
                parameters={"x": 5000, "y": 5000, "window_title": "NovaGame"},
            ))
        self.assertEqual(worker.commands, [])

    async def test_a_missing_target_window_is_refused(self) -> None:
        worker = _FakeInputWorker(geometry=None)
        runner = _runner(worker)
        with self.assertRaises(Exception) as ctx:
            await runner.run(DesktopAction(
                type=DesktopActionType.MOUSE_CLICK, target="10,10", description="click",
                parameters={"x": 10, "y": 10, "window_title": "Gone"},
            ))
        self.assertIn("no longer exists", str(ctx.exception).lower())
        self.assertEqual(worker.commands, [])

    async def test_malformed_coordinates_are_refused_not_defaulted(self) -> None:
        worker = _FakeInputWorker()
        runner = _runner(worker)
        with self.assertRaises(ValueError):
            await runner.run(DesktopAction(
                type=DesktopActionType.MOUSE_MOVE, target="x", description="move",
                parameters={"x": "not-a-number", "y": 10}))
        self.assertEqual(worker.commands, [], "(0,0) is never a silent default")

    async def test_window_geometry_reports_the_target(self) -> None:
        worker = _FakeInputWorker(_game_window())
        runner = _runner(worker)
        result = dict(await runner.run(DesktopAction(
            type=DesktopActionType.WINDOW_GEOMETRY, target="NovaGame", description="geometry",
            parameters={"window_title": "NovaGame"})))
        self.assertEqual(result["title"], "NovaGame")
        self.assertEqual(result["bounds"]["width"], 1920)
        self.assertEqual(result["scale"], 2.0)


class ClientAreaAddressingTests(unittest.TestCase):
    """A point is judged against the client area's ABSOLUTE extent.

    ``client`` carries an offset plus a size, so a window that is not at the
    screen origin has its right/bottom edges at ``origin + size``. Treating the
    size as an absolute edge made every click on the right half of a real
    window refuse with "outside the client area" — a silent 2.0x-space bug
    invisible to tests whose window starts at (0, 0).
    """

    def test_the_client_bounds_are_absolute(self) -> None:
        self.assertEqual(_placed_window().client_bounds(), (440, 360, 1280, 1000))

    def test_a_point_in_the_right_half_is_accepted(self) -> None:
        target = validate_input_target(_placed_window(), 852, 672)
        self.assertEqual((target.x, target.y), (852, 672))

    def test_a_point_on_the_far_edge_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_input_target(_placed_window(), 1280, 672)

    def test_a_point_below_the_bottom_edge_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_input_target(_placed_window(), 852, 1000)

    def test_a_client_offset_is_added_not_treated_as_an_edge(self) -> None:
        window = WindowGeometry(
            handle=0x201, title="Offset", bounds=(100, 120, 900, 720),
            client=(8, 40, 792, 560), pid=5151, scale=1.0, foreground=True,
        )
        self.assertEqual(window.client_bounds(), (108, 160, 900, 720))
        self.assertEqual(validate_input_target(window, 300, 400).x, 300)
        with self.assertRaises(ValueError):
            validate_input_target(window, 107, 400)  # left of the client origin
        with self.assertRaises(ValueError):
            validate_input_target(window, 900, 400)  # right edge is exclusive


class InputTargetValidationTests(unittest.TestCase):
    """Input is refused when the target window is not where we think it is."""

    def test_a_point_in_the_client_area_is_accepted(self) -> None:
        target = validate_input_target(_window(), 100, 500)
        self.assertEqual((target.x, target.y), (100, 500))

    def test_a_point_on_the_title_bar_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_input_target(_window(), 100, 10)
        self.assertIn("client area", str(ctx.exception))

    def test_a_point_outside_the_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_input_target(_window(), 5000, 5000)

    def test_a_missing_window_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_input_target(None, 100, 500)
        self.assertIn("no longer exists", str(ctx.exception))

    def test_a_changed_handle_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_input_target(_window(), 100, 500, expected_handle=0x999)
        self.assertIn("identity changed", str(ctx.exception))

    def test_a_changed_pid_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_input_target(_window(), 100, 500, expected_pid=1)

    def test_a_different_title_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_input_target(_window(), 100, 500, expected_title="Firefox")

    def test_the_matching_title_is_accepted(self) -> None:
        target = validate_input_target(_window(), 100, 500, expected_title="NovaGame")
        self.assertEqual(target.geometry.pid, 4242)

    def test_not_foreground_is_refused_when_required(self) -> None:
        blurred = _window(foreground=False)
        with self.assertRaises(ValueError) as ctx:
            validate_input_target(blurred, 100, 500, require_foreground=True)
        self.assertIn("foreground", str(ctx.exception))

    def test_foreground_is_ignored_when_not_required(self) -> None:
        target = validate_input_target(_window(foreground=False), 100, 500)
        self.assertFalse(target.geometry.foreground)

    def test_validated_target_serializes(self) -> None:
        payload = validate_input_target(_window(), 100, 500).to_dict()
        self.assertEqual(payload["window"], "NovaGame - Steam")
        self.assertEqual(payload["pid"], 4242)
        geometry = _window().to_dict()
        self.assertEqual(geometry["bounds"]["width"], 1920)
        self.assertEqual(geometry["scale"], 2.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
