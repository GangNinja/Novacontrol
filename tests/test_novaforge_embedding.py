"""NF-13A/NF-14 — window embedding and compatibility, through a fake Win32 API.

No test in this file reparents a real window, starts a game or touches a display.
The fake implements the same ``WindowApi`` protocol the Win32 adapter does, so the
host's decisions (identity, style, geometry, restoration, classification) are
exercised rather than its ctypes plumbing. The live evidence is separate and lives
in the NF-13A and NF-14 reports.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime

from novacontrol.novaforge.compat import (
    CompatibilityClass,
    CompatibilityLog,
    CompatibilityRecord,
    apply_restore,
    classify_preflight,
)
from novacontrol.novaforge.embedding import (
    CLOSED,
    DETACHED,
    EMBEDDED,
    EMBEDDING_UNSUPPORTED,
    RUNNING,
    GameIdentity,
    Win32WindowApi,
    fit_rect,
    parse_start_time,
    window_mode,
)
from novacontrol.novaforge.embedding import (
    SWP_FRAMECHANGED,
    EmbeddedGameHost,
    Rect,
    WindowApiError,
    WindowInfo,
    WindowLookup,
)

WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_VISIBLE = 0x10000000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000

START = 1000.0
START_ISO = datetime.fromtimestamp(START, UTC).isoformat()


@dataclass
class _FakeWindow:
    handle: int
    pid: int
    title: str
    style: int
    rect: Rect
    visible: bool = True
    minimized: bool = False
    parent: int = 0
    owner: int = 0
    class_name: str = "FakeGameWindow"
    #: A framed window's client area is smaller than its window rect; None
    #: means "no frame" (client == rect).
    client: Rect | None = None


@dataclass
class FakeWindowApi:
    """A stateful stand-in for User32/Kernel32 at the protocol level."""

    host: int = 2000
    host_size: tuple[int, int] = (800, 600)
    host_origin: tuple[int, int] = (100, 100)
    #: The surface itself can be hidden (a tab switch) or destroyed; both are
    #: properties of the HOST, not of the game window.
    host_visible: bool = True
    host_alive: bool = True
    position_flags: list[int] = field(default_factory=list)
    monitor: Rect = field(default_factory=lambda: Rect(0, 0, 1280, 720))
    platform_supported: bool = True
    created: dict[int, float] = field(default_factory=dict)
    windows: dict[int, _FakeWindow] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    focus_calls: list[int] = field(default_factory=list)
    refuse_parent: bool = False
    refuse_style: bool = False
    refuse_position: bool = False
    find_hidden: bool = False
    find_any: bool = False
    misplace: bool = False
    fail_next_position: bool = False

    def add_window(self, **kwargs: object) -> _FakeWindow:
        window = _FakeWindow(**kwargs)  # type: ignore[arg-type]
        self.windows[window.handle] = window
        self.created.setdefault(window.pid, START)
        return window

    def find_window(self, pid: int, title_hint: str = "") -> WindowLookup:
        self.calls.append(f"find_window:{pid}")
        matches = [
            window for window in self.windows.values()
            if window.pid == pid
            and (window.visible or self.find_hidden)
            and (window.parent == 0 or self.find_any)
            and not window.owner and window.rect.area > 0
        ]
        if not matches:
            return WindowLookup(0, 0, f"No visible top-level window belongs to pid {pid}.")
        hint = str(title_hint or "").lower()
        matches.sort(key=lambda item: (0 if hint and hint in item.title.lower() else 1,
                                       -item.rect.area))
        return WindowLookup(matches[0].handle, len(matches))

    def window(self, handle: int) -> WindowInfo:
        if handle == self.host:
            if not self.host_alive:
                raise WindowApiError(f"Window {handle} does not exist.")
            return WindowInfo(
                handle=handle, pid=0, title="NovaControl surface",
                class_name="QtWindow", style=WS_CHILD, ex_style=0,
                rect=Rect(self.host_origin[0], self.host_origin[1], *self.host_size),
                client=Rect(0, 0, *self.host_size),
                visible=self.host_visible, minimized=False, owner=0, parent=0, dpi=96,
            )
        if handle not in self.windows:
            raise WindowApiError(f"Window {handle} does not exist.")
        window = self.windows[handle]
        rect = self.screen_rect(handle)
        return WindowInfo(
            handle=window.handle,
            pid=window.pid,
            title=window.title,
            class_name=window.class_name,
            style=window.style,
            ex_style=0,
            rect=rect,
            client=window.client or Rect(0, 0, rect.width, rect.height),
            visible=window.visible,
            minimized=window.minimized,
            owner=window.owner,
            parent=window.parent,
            dpi=96,
        )

    def is_window(self, handle: int) -> bool:
        return (handle == self.host and self.host_alive) or handle in self.windows

    def parent(self, handle: int) -> int:
        if handle not in self.windows:
            return 0
        return self.windows[handle].parent

    def set_parent(self, child: int, parent: int) -> None:
        self.calls.append(f"set_parent:{child}:{parent}")
        if self.refuse_parent:
            return
        self.windows[child].parent = parent

    def set_style(self, handle: int, style: int) -> int:
        self.calls.append(f"set_style:{handle}:0x{style:08X}")
        if not self.refuse_style:
            self.windows[handle].style = style & 0xFFFFFFFF
        return self.windows[handle].style

    def set_window_pos(
        self, handle: int, x: int, y: int, width: int, height: int, flags: int
    ) -> bool:
        self.calls.append(f"set_window_pos:{handle}:{x},{y},{width},{height}")
        self.position_flags.append(flags)
        if self.refuse_position:
            return False
        if self.fail_next_position:
            self.fail_next_position = False
            return False
        window = self.windows[handle]
        if self.misplace and window.parent:
            width, height = 10, 10
        if window.parent:
            origin = self.client_origin(window.parent)
            window.rect = Rect(origin[0] + x, origin[1] + y, width, height)
        else:
            window.rect = Rect(x, y, width, height)
        return True

    def client_rect(self, handle: int) -> Rect:
        return Rect(0, 0, *self.host_size)

    def client_origin(self, handle: int) -> tuple[int, int]:
        return self.host_origin

    def screen_rect(self, handle: int) -> Rect:
        return self.windows[handle].rect

    def monitor_rect(self, handle: int) -> Rect | None:
        return self.monitor

    def process_creation_time(self, pid: int) -> float | None:
        return self.created.get(pid)

    def focus(self, handle: int) -> bool:
        self.focus_calls.append(handle)
        return True


def windowed_window(api: FakeWindowApi, **overrides: object) -> _FakeWindow:
    values: dict[str, object] = {
        "handle": 3000,
        "pid": 4242,
        "title": "Shadow Fight Arena",
        "style": WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
        "rect": Rect(40, 60, 1280, 720),
    }
    values.update(overrides)
    return api.add_window(**values)


def identity(**overrides: object) -> GameIdentity:
    values: dict[str, object] = {
        "pid": 4242,
        "start_time": START_ISO,
        "window_title": "Shadow Fight Arena",
        "name": "Shadow Fight Arena",
        "owned": True,
        "launch_id": "launch-1",
    }
    values.update(overrides)
    return GameIdentity(**values)  # type: ignore[arg-type]


class GeometryTests(unittest.TestCase):
    def test_fit_rect_letterboxes_without_stretching(self) -> None:
        wide_host = fit_rect(1600, 900, 4 / 3)
        self.assertEqual((wide_host.width, wide_host.height), (1200, 900))
        self.assertEqual(wide_host.x, 200)
        self.assertEqual(wide_host.y, 0)

        tall_host = fit_rect(800, 800, 16 / 9)
        self.assertEqual((tall_host.width, tall_host.height), (800, 450))
        self.assertEqual(tall_host.x, 0)
        self.assertEqual(tall_host.y, 175)

    def test_exact_aspect_fills_the_host(self) -> None:
        self.assertEqual(fit_rect(800, 450, 16 / 9), Rect(0, 0, 800, 450))

    def test_degenerate_inputs_do_not_produce_a_zero_sized_window(self) -> None:
        self.assertEqual(fit_rect(0, 0, 16 / 9), Rect(0, 0, 0, 0))
        self.assertEqual(fit_rect(800, 600, 0), Rect(0, 0, 800, 600))
        self.assertEqual(fit_rect(800, 600, -2), Rect(0, 0, 800, 600))
        self.assertGreaterEqual(fit_rect(1, 1, 16 / 9).width, 1)

    def test_parse_start_time_reads_powershell_precision(self) -> None:
        parsed = parse_start_time("2026-10-23T01:43:30.5955071Z")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertAlmostEqual(
            parsed,
            datetime(2026, 10, 23, 1, 43, 30, 595507, tzinfo=UTC).timestamp(),
            places=3,
        )

    def test_parse_start_time_treats_a_naive_value_as_utc(self) -> None:
        self.assertEqual(
            parse_start_time("1970-01-01T00:16:40"),
            parse_start_time("1970-01-01T00:16:40+00:00"),
        )
        self.assertIsNone(parse_start_time(""))
        self.assertIsNone(parse_start_time("not-a-time"))


class WindowModeTests(unittest.TestCase):
    def info(self, style: int, rect: Rect, *, minimized: bool = False) -> WindowInfo:
        return WindowInfo(
            handle=1, pid=2, title="t", class_name="c", style=style, ex_style=0,
            rect=rect, client=Rect(0, 0, rect.width, rect.height),
            visible=True, minimized=minimized, owner=0, parent=0, dpi=96,
        )

    def test_borderless_fullscreen_is_recognised_by_style_and_monitor_geometry(self) -> None:
        monitor = Rect(0, 0, 2880, 1800)
        info = self.info(WS_POPUP | WS_VISIBLE, Rect(0, 0, 2880, 1800))
        self.assertEqual(window_mode(info, monitor), "borderless_fullscreen")
        self.assertEqual(
            window_mode(self.info(WS_CAPTION | WS_VISIBLE, Rect(0, 0, 2880, 1800)), monitor),
            "windowed",
        )
        self.assertEqual(
            window_mode(self.info(WS_POPUP | WS_VISIBLE, Rect(0, 0, 1280, 720)), monitor),
            "borderless",
        )

    def test_minimized_wins_over_style(self) -> None:
        info = self.info(WS_POPUP | WS_VISIBLE, Rect(-32000, -32000, 314, 50), minimized=True)
        self.assertEqual(window_mode(info, Rect(0, 0, 2880, 1800)), "minimized")


class Win32GeometryReadTests(unittest.TestCase):
    """A failed Win32 geometry call is a refusal, never a zero/garbage rectangle.

    Live NF-13A run `20261024T104209Z` recorded a window at x=-31405: the fixture
    was already gone, ``GetWindowRect`` had failed, and the uninitialised RECT was
    reported as a measurement. A figure that was not measured must be an error.
    """

    def adapter(self, user32: object) -> Win32WindowApi:
        api = Win32WindowApi(platform="nt")
        api._user32 = user32  # noqa: SLF001 - the binding the adapter caches once
        api._kernel32 = object()
        return api

    def test_unreadable_geometry_raises_instead_of_reporting_a_rect(self) -> None:
        class Refusing:
            def GetWindowRect(self, handle: int, rect: object) -> int:
                return 0

            def GetClientRect(self, handle: int, rect: object) -> int:
                return 0

            def ClientToScreen(self, handle: int, point: object) -> int:
                return 0

        api = self.adapter(Refusing())
        for call in (api.screen_rect, api.client_rect, api.client_origin):
            with self.subTest(call=call.__name__):
                with self.assertRaises(WindowApiError):
                    call(1234)

    def test_a_window_that_vanishes_mid_read_is_not_measured_as_zero(self) -> None:
        class Vanishing:
            def IsWindow(self, handle: int) -> int:
                return 1

            def GetWindowThreadProcessId(self, handle: int, pid: object) -> int:
                return 1

            def GetWindowRect(self, handle: int, rect: object) -> int:
                return 0

            def GetClientRect(self, handle: int, rect: object) -> int:
                return 0

        api = self.adapter(Vanishing())
        with self.assertRaises(WindowApiError):
            api.window(9)

    def test_successful_reads_are_converted(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Measuring:
            def GetWindowRect(self, handle: int, rect: object) -> int:
                target = ctypes.cast(rect, ctypes.POINTER(wintypes.RECT)).contents
                target.left, target.top, target.right, target.bottom = 10, 20, 110, 220
                return 1

            def GetClientRect(self, handle: int, rect: object) -> int:
                target = ctypes.cast(rect, ctypes.POINTER(wintypes.RECT)).contents
                target.left, target.top, target.right, target.bottom = 0, 0, 90, 180
                return 1

            def ClientToScreen(self, handle: int, point: object) -> int:
                spot = ctypes.cast(point, ctypes.POINTER(wintypes.POINT)).contents
                spot.x, spot.y = 40, 50
                return 1

        api = self.adapter(Measuring())
        self.assertEqual(api.screen_rect(7), Rect(10, 20, 100, 200))
        self.assertEqual(api.client_rect(7), Rect(0, 0, 90, 180))
        self.assertEqual(api.client_origin(7), (40, 50))


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWindowApi()
        self.host = EmbeddedGameHost(self.api)
        self.game = windowed_window(self.api)

    def test_windowed_game_is_supported_without_confirmation(self) -> None:
        pre = self.host.preflight(identity())
        self.assertTrue(pre.supported)
        self.assertEqual(pre.window_mode, "windowed")
        self.assertFalse(pre.requires_confirmation)
        self.assertEqual(pre.window.handle if pre.window else 0, 3000)
        self.assertEqual(self.api.calls, ["find_window:4242"])

    def test_fullscreen_game_is_refused_by_preflight(self) -> None:
        """Live NF-13A: SetParent on these windows killed the game process.

        The refusal is therefore the policy, not a warning: no native call may be
        made for a borderless/fullscreen window, and there is no bypass.
        """
        self.game.style = WS_POPUP | WS_VISIBLE
        self.game.rect = Rect(0, 0, 1280, 720)
        pre = self.host.preflight(identity())
        self.assertFalse(pre.supported)
        self.assertEqual(pre.window_mode, "borderless_fullscreen")
        self.assertEqual(pre.reason_code, "fullscreen_presentation_unsupported")
        self.assertIn("Windowed mode in ITS OWN settings", pre.reason)

    def test_non_windows_host_refuses(self) -> None:
        api = Win32WindowApi(platform="posix")
        host = EmbeddedGameHost(api)
        pre = host.preflight(identity())
        self.assertFalse(api.platform_supported)
        self.assertEqual(pre.reason_code, "platform_unsupported")
        self.assertFalse(pre.supported)

    def test_a_window_that_vanishes_during_preflight_is_window_not_found(self) -> None:
        class VanishOnRead(FakeWindowApi):
            def window(self, handle: int) -> WindowInfo:
                if handle == self.host:
                    return super().window(handle)
                raise WindowApiError(f"Window {handle} disappeared while it was being measured.")

        api = VanishOnRead()
        windowed_window(api)
        pre = EmbeddedGameHost(api).preflight(identity())
        self.assertFalse(pre.supported)
        self.assertEqual(pre.reason_code, "window_not_found")
        self.assertIn("disappeared", pre.reason)

    def test_missing_window_is_reported_without_touching_anything(self) -> None:
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        pre = host.preflight(identity())
        self.assertEqual(pre.reason_code, "window_not_found")
        self.assertFalse(pre.supported)
        self.assertEqual(api.calls, ["find_window:4242"])

    def test_minimized_hidden_and_hosted_windows_are_refused(self) -> None:
        cases = {
            "window_minimized": ({"minimized": True}, {}),
            "window_hidden": ({"visible": False}, {"find_hidden": True}),
            "already_child": ({"parent": 777}, {"find_any": True}),
        }
        for code, (overrides, api_flags) in cases.items():
            with self.subTest(code=code):
                api = FakeWindowApi(**api_flags)
                host = EmbeddedGameHost(api)
                windowed_window(api, **overrides)
                pre = host.preflight(identity())
                self.assertFalse(pre.supported)
                self.assertEqual(pre.reason_code, code)

    def test_pid_reuse_is_refused(self) -> None:
        self.api.created[4242] = START + 5000
        pre = self.host.preflight(identity())
        self.assertFalse(pre.supported)
        self.assertEqual(pre.reason_code, "process_identity_mismatch")
        self.assertIn("reused PID", pre.reason)

    def test_a_small_creation_time_delta_is_the_same_process(self) -> None:
        self.api.created[4242] = START + 0.75
        self.assertTrue(self.host.preflight(identity()).supported)

    def test_unreadable_identity_is_refused_instead_of_guessed(self) -> None:
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        windowed_window(api)
        pre = host.preflight(identity(start_time=""))
        self.assertEqual(pre.reason_code, "process_identity_mismatch")
        api.created.pop(4242)
        pre = host.preflight(identity())
        self.assertEqual(pre.reason_code, "process_identity_mismatch")

    def test_no_pid_is_refused(self) -> None:
        pre = self.host.preflight(identity(pid=0))
        self.assertEqual(pre.reason_code, "no_process")


class EmbedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWindowApi()
        self.host = EmbeddedGameHost(self.api)
        self.game = windowed_window(self.api)

    def test_child_style_is_applied_before_set_parent(self) -> None:
        """SetParent with WS_POPUP still present makes GetParent read 0.

        The live probe found this: the window WAS parented, but the old order
        checked GetParent before WS_CHILD existed, called it a failure, and the
        restore path then destroyed the window. Style first is the contract.
        """
        result = self.host.embed(identity(), self.api.host)
        self.assertTrue(result["embedded"])
        mutations = [
            call for call in self.api.calls
            if call.startswith("set_style:") or call.startswith("set_parent:")
        ]
        self.assertTrue(mutations and mutations[0].startswith("set_style:"))

    def test_content_aspect_is_taken_from_the_client_not_the_framed_rect(self) -> None:
        """A framed window keeps its CONTENT ratio after the frame is cleared.

        The probe's real numbers: a 1800x1000 window holds 1768x922 of content
        (1.80 vs 1.92). Sizing by the window rect would stretch the game by the
        frame's share, so the client rect decides.
        """
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        windowed_window(
            api,
            rect=Rect(40, 60, 1600, 1000),
            client=Rect(0, 0, 1584, 880),
        )
        result = host.embed(identity(), api.host)
        self.assertTrue(result["embedded"])
        target = result["embedding"]["target"]
        self.assertAlmostEqual(target["width"] / target["height"], 1584 / 880, places=2)
        self.assertNotAlmostEqual(target["width"] / target["height"], 1.6, places=1)

    def test_successful_embed_verifies_parent_style_and_geometry(self) -> None:
        result = self.host.embed(identity(), self.api.host)
        self.assertTrue(result["embedded"])
        self.assertEqual(self.host.state, EMBEDDED)
        self.assertTrue(result["embedding"]["verified"])
        self.assertEqual(self.api.windows[3000].parent, self.api.host)
        self.assertTrue(self.game.style & WS_CHILD)
        self.assertFalse(self.game.style & WS_POPUP)
        self.assertTrue(self.game.style & WS_VISIBLE)
        self.assertEqual((self.game.rect.width, self.game.rect.height), (800, 450))
        self.assertEqual(self.game.rect.x, self.api.host_origin[0])
        self.assertEqual(self.game.rect.y, self.api.host_origin[1] + 75)
        self.assertEqual(result["identity"]["pid"], 4242)
        self.assertTrue(result["close_allowed"])

    def test_fullscreen_embed_is_refused_without_touching_the_window(self) -> None:
        self.game.style = WS_POPUP | WS_VISIBLE
        self.game.rect = Rect(0, 0, 1280, 720)
        original_style = self.game.style
        result = self.host.embed(identity(), self.api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(
            result["embedding"]["reason_code"], "fullscreen_presentation_unsupported"
        )
        self.assertNotIn("set_parent:3000:2000", self.api.calls)
        self.assertNotIn("set_style:3000", " ".join(self.api.calls))
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.style, original_style)
        self.assertEqual(self.game.rect, Rect(0, 0, 1280, 720))

    def test_missing_host_handle_is_refused(self) -> None:
        result = self.host.embed(identity(), 0)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "host_not_a_window")
        self.assertEqual(self.game.parent, 0)

    def test_set_parent_failure_restores_the_original_window(self) -> None:
        self.api.refuse_parent = True
        result = self.host.embed(identity(), self.api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "embedding_failed")
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.style, WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE)
        self.assertEqual(self.game.rect, Rect(40, 60, 1280, 720))
        self.assertIn(f"set_style:3000:0x{self.game.style:08X}", self.api.calls)

    def test_style_failure_restores_parent_and_rect(self) -> None:
        self.api.refuse_style = True
        result = self.host.embed(identity(), self.api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "embedding_failed")
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.rect, Rect(40, 60, 1280, 720))

    def test_position_failure_restores_the_window(self) -> None:
        self.api.refuse_position = True
        result = self.host.embed(identity(), self.api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "embedding_failed")
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.style, WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE)

    def test_embedding_while_embedded_requires_detach(self) -> None:
        self.assertTrue(self.host.embed(identity(), self.api.host)["embedded"])
        again = self.host.embed(identity(), self.api.host)
        self.assertTrue(again["embedded"])
        self.assertTrue(again["already_embedded"])
        other = self.host.embed(identity(window_title="Other"), self.api.host)
        self.assertFalse(other["embedded"])
        self.assertEqual(other["embedding"]["reason_code"], "already_embedded")
        # A refusal must not forget the window it is still hosting: the state
        # has to stay EMBEDDED so the panel keeps its Detach path.
        self.assertEqual(other["state"], EMBEDDED)
        self.assertEqual(self.host.state, EMBEDDED)
        self.assertEqual(self.game.parent, self.api.host)
        self.assertTrue(self.host.detach()["detached"])

    def test_embed_off_windows_names_the_platform_not_the_handle(self) -> None:
        api = FakeWindowApi(platform_supported=False)
        host = EmbeddedGameHost(api)
        windowed_window(api)
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "platform_unsupported")
        self.assertEqual(api.calls, [])

    def test_placement_requests_a_frame_change(self) -> None:
        """The caption/frame was just cleared; SetWindowPos must recalc it."""
        self.assertTrue(self.host.embed(identity(), self.api.host)["embedded"])
        self.assertTrue(self.api.position_flags)
        self.assertTrue(self.api.position_flags[-1] & SWP_FRAMECHANGED)

    def test_running_state_does_not_claim_embedding(self) -> None:
        self.host.set_running({"pid": 4242, "start_time": START_ISO,
                               "window": "Shadow Fight Arena", "owned": True},
                              name="Shadow Fight Arena")
        self.assertEqual(self.host.state, RUNNING)
        payload = self.host.status_payload()
        self.assertFalse(payload["embedding"]["attempted"])
        self.assertFalse(payload["embedding"]["verified"])

    def test_set_running_needs_a_pid(self) -> None:
        with self.assertRaises(ValueError):
            self.host.set_running({"pid": 0})

    def test_a_running_record_without_ownership_is_not_closable(self) -> None:
        """Adoption is the safe default: no owned flag means "not ours to close"."""
        payload = self.host.set_running(
            {"pid": 4242, "start_time": START_ISO, "window": "Shadow Fight Arena"}
        )
        self.assertFalse(payload["owned"])
        self.assertFalse(payload["close_allowed"])
        launched = self.host.set_running(
            {"pid": 4242, "start_time": START_ISO, "window": "Shadow Fight Arena",
             "owned": True, "launch_id": "launch-1"}
        )
        self.assertTrue(launched["close_allowed"])

    def test_user_owned_instance_is_never_closable(self) -> None:
        self.host.adopt_running(
            {"pid": 4242, "start_time": START_ISO, "window": "Shadow Fight Arena"},
            name="Shadow Fight Arena",
        )
        payload = self.host.status_payload()
        self.assertFalse(payload["owned"])
        self.assertFalse(payload["close_allowed"])
        self.assertTrue(self.host.embed(identity(owned=False), self.api.host)["embedded"])
        self.assertFalse(self.host.status_payload()["close_allowed"])


class DetachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWindowApi()
        self.host = EmbeddedGameHost(self.api)
        self.game = windowed_window(self.api)
        self.assertTrue(self.host.embed(identity(), self.api.host)["embedded"])

    def test_detach_unparents_before_restoring_the_original_style(self) -> None:
        self.host.detach()
        tail = self.api.calls[self.api.calls.index("set_parent:3000:2000") + 1:]
        mutations = [
            call for call in tail
            if call.startswith("set_style:") or call.startswith("set_parent:")
        ]
        self.assertTrue(mutations and mutations[0] == "set_parent:3000:0")

    def test_detach_restores_parent_style_and_screen_rect(self) -> None:
        result = self.host.detach()
        self.assertTrue(result["detached"])
        self.assertTrue(result["restored"])
        self.assertEqual(self.host.state, DETACHED)
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.style, WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE)
        self.assertEqual(self.game.rect, Rect(40, 60, 1280, 720))

    def test_reembedding_after_detach_works(self) -> None:
        self.host.detach()
        self.assertTrue(self.host.embed(identity(), self.api.host)["embedded"])
        self.assertEqual(self.game.parent, self.api.host)

    def test_detach_with_nothing_embedded_is_refused(self) -> None:
        self.host.detach()
        again = self.host.detach()
        self.assertFalse(again["detached"])

    def test_window_destroyed_while_embedded_reports_closed(self) -> None:
        self.api.windows.pop(3000)
        payload = self.host.check()
        self.assertEqual(payload["state"], CLOSED)
        self.assertEqual(payload["embedding"]["reason_code"], "process_exited")

    def test_recreated_window_is_reported_detached_without_reattaching(self) -> None:
        self.api.windows.pop(3000)
        replacement = windowed_window(self.api, handle=3001, rect=Rect(10, 10, 800, 450))
        payload = self.host.check()
        self.assertEqual(payload["state"], DETACHED)
        self.assertEqual(payload["embedding"]["reason_code"], "window_replaced")
        self.assertEqual(replacement.parent, 0)
        self.assertIn("will not attach the replacement automatically",
                      payload["embedding"]["reason"])

    def test_window_leaving_the_host_is_reported_detached(self) -> None:
        self.game.parent = 0
        payload = self.host.check()
        self.assertEqual(payload["state"], DETACHED)
        self.assertEqual(payload["embedding"]["reason_code"], "window_left_host")

    def test_minimized_embedded_window_is_detached_and_restored(self) -> None:
        self.game.minimized = True
        payload = self.host.check()
        self.assertEqual(payload["state"], DETACHED)
        self.assertEqual(payload["embedding"]["reason_code"], "window_minimized")
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.rect, Rect(40, 60, 1280, 720))

    def test_hidden_embedded_window_is_detached(self) -> None:
        self.game.visible = False
        payload = self.host.check()
        self.assertEqual(payload["state"], DETACHED)
        self.assertEqual(payload["embedding"]["reason_code"], "window_hidden")

    def test_resize_letterboxes_within_the_new_client_area(self) -> None:
        self.api.host_size = (1200, 600)
        result = self.host.resize()
        self.assertTrue(result["resized"])
        target = result["target"]
        self.assertEqual((target["width"], target["height"]), (1067, 600))
        self.assertEqual(target["x"], 66)

    def test_resize_verification_failure_restores_the_window(self) -> None:
        self.api.misplace = True
        result = self.host.resize()
        self.assertFalse(result["resized"])
        self.assertTrue(result["restored"])
        self.assertEqual(result["embedding"]["reason_code"], "resize_failed")
        assert self.host.compatibility is not None
        self.assertTrue(self.host.compatibility.restored)
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.rect, Rect(40, 60, 1280, 720))

    def test_resize_that_cannot_be_applied_keeps_the_last_valid_geometry(self) -> None:
        before = self.game.rect
        self.api.fail_next_position = True
        result = self.host.resize()
        self.assertFalse(result["resized"])
        self.assertIn("last verified geometry was kept", result["reason"])
        self.assertEqual(self.game.parent, self.api.host)
        self.assertEqual(self.game.rect, before)
        self.assertEqual(self.host.state, EMBEDDED)

    def test_hidden_host_keeps_the_embedding(self) -> None:
        """A dashboard tab switch hides the surface and its child with it.

        Win32 reports the child as not visible in that state; treating that as
        "the game hid itself" detached a perfectly good embedding every time
        the operator looked at another tab.
        """
        self.api.host_visible = False
        self.game.visible = False  # what IsWindowVisible reports for a hidden parent chain
        payload = self.host.check()
        self.assertEqual(payload["state"], EMBEDDED)
        self.assertEqual(payload["embedding"]["last_check_reason"], "host_hidden")
        self.assertEqual(self.game.parent, self.api.host)

    def test_resize_is_deferred_while_the_surface_is_hidden(self) -> None:
        before = self.game.rect
        calls = len(self.api.calls)
        self.api.host_visible = False
        result = self.host.resize()
        self.assertFalse(result["resized"])
        self.assertTrue(result["deferred"])
        self.assertEqual(self.host.state, EMBEDDED)
        self.assertEqual(self.game.rect, before)
        self.assertEqual(len(self.api.calls), calls)

    def test_host_destroyed_detaches_and_returns_the_window(self) -> None:
        self.api.host_alive = False
        payload = self.host.check()
        self.assertEqual(payload["state"], DETACHED)
        self.assertEqual(payload["embedding"]["reason_code"], "host_destroyed")
        self.assertEqual(self.game.parent, 0)
        self.assertEqual(self.game.style, WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE)
        self.assertEqual(self.game.rect, Rect(40, 60, 1280, 720))

    def test_detach_after_a_refused_embed_claims_nothing(self) -> None:
        self.host.detach()  # start from a clean state
        self.api.windows.pop(3000)
        screen = windowed_window(
            self.api, handle=3002, style=WS_POPUP | WS_VISIBLE, rect=Rect(0, 0, 1280, 720)
        )
        refused = self.host.embed(identity(), self.api.host)
        self.assertFalse(refused["embedded"])
        self.assertEqual(refused["state"], EMBEDDING_UNSUPPORTED)
        again = self.host.detach()
        self.assertFalse(again["detached"])
        self.assertIn("nothing to restore", again["reason"])
        self.assertEqual(again["state"], EMBEDDING_UNSUPPORTED)
        self.assertEqual(screen.parent, 0)

    def test_detach_after_the_window_was_destroyed_claims_no_restore(self) -> None:
        self.api.windows.pop(3000)
        result = self.host.detach()
        self.assertFalse(result["detached"])
        self.assertIsNone(result["restored"])
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["embedding"]["reason_code"], "window_destroyed")
        assert self.host.compatibility is not None
        self.assertIsNone(self.host.compatibility.restored)
        self.assertFalse(
            any(
                "could not confirm" in note
                for note in self.host.compatibility.limitations
            )
        )

    def test_detach_clears_the_host_handle(self) -> None:
        result = self.host.detach()
        self.assertTrue(result["detached"])
        self.assertEqual(result["host_handle"], 0)
        self.assertEqual(self.host.host_handle, 0)

    def test_focus_is_only_requested_explicitly(self) -> None:
        self.assertEqual(self.api.focus_calls, [])
        result = self.host.focus_game()
        self.assertTrue(result["focused"])
        self.assertEqual(self.api.focus_calls, [3000])
        self.host.detach()
        refused = self.host.focus_game()
        self.assertFalse(refused["focused"])
        self.assertEqual(self.api.focus_calls, [3000])

    def test_lifecycle_polling_never_steals_focus(self) -> None:
        """A timer-driven check/resize must not create a focus loop."""
        for _ in range(3):
            self.host.check()
            self.host.resize()
        self.assertEqual(self.api.focus_calls, [])
        self.assertEqual(self.host.state, EMBEDDED)

    def test_process_identity_change_while_embedded_is_reported(self) -> None:
        """A reused PID is not the launched game, even with the same window."""
        self.api.created[4242] = START + 60
        payload = self.host.check()
        self.assertEqual(payload["state"], CLOSED)
        self.assertEqual(payload["embedding"]["reason_code"], "process_identity_changed")
        self.assertFalse(payload["embedding"]["verified"])

    def test_mark_closed_after_the_service_confirms_exit(self) -> None:
        payload = self.host.mark_closed("The game exited after a graceful close.")
        self.assertEqual(payload["state"], CLOSED)
        self.assertEqual(payload["embedding"]["reason"], "The game exited after a graceful close.")


class ReportingTests(unittest.TestCase):
    def test_embedding_and_capture_observation_stay_separate(self) -> None:
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        windowed_window(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        payload = host.status_payload(
            observation={"available": False, "reason": "No capture backend validated a frame."}
        )
        self.assertTrue(payload["embedding"]["verified"])
        self.assertFalse(payload["observation"]["available"])
        self.assertFalse(payload["observation"]["implied_by_embedding"])
        self.assertNotIn("observation", host.status_payload())

    def test_state_labels_and_initial_state(self) -> None:
        host = EmbeddedGameHost(FakeWindowApi())
        payload = host.status_payload()
        self.assertEqual(payload["state"], CLOSED)
        self.assertEqual(payload["state_label"], "Closed")
        self.assertFalse(payload["embedding"]["attempted"])
        self.assertIsNone(payload["identity"])
        fallback = payload["fallback"]
        self.assertFalse(fallback["active"])
        self.assertEqual(fallback["next_steps"], [])
        self.assertIn("No game", fallback["reason"])

    def test_a_second_launch_while_embedded_is_refused(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        with self.assertRaises(RuntimeError):
            host.set_launching("Another Game")
        self.assertEqual(host.state, EMBEDDED)
        self.assertEqual(api.parent(3000), api.host)
        self.assertTrue(host.compatibility is not None)

    def test_set_launching_does_not_claim_a_window(self) -> None:
        host = EmbeddedGameHost(FakeWindowApi())
        payload = host.set_launching("Shadow Fight Arena")
        self.assertEqual(payload["state"], "launching")
        self.assertFalse(payload["embedding"]["verified"])
        self.assertIsNone(payload["window"])

    def test_unsupported_state_is_named_and_reasoned(self) -> None:
        api = FakeWindowApi(find_hidden=True)
        host = EmbeddedGameHost(api)
        windowed_window(api, visible=False)
        payload = host.embed(identity(), api.host)
        self.assertEqual(payload["state"], EMBEDDING_UNSUPPORTED)
        self.assertFalse(payload["embedding"]["verified"])
        self.assertEqual(payload["embedding"]["reason_code"], "window_hidden")


class CompatibilityClassificationTests(unittest.TestCase):
    """NF-14 Scope A: a class comes from measured facts, never from a title."""

    def host(self, **overrides: object) -> tuple[FakeWindowApi, EmbeddedGameHost]:
        api = FakeWindowApi()
        windowed_window(api, **overrides)
        return api, EmbeddedGameHost(api)

    def test_a_windowed_window_is_supported_with_measured_evidence(self) -> None:
        _api, host = self.host()
        record = host.probe_compatibility(identity(), app_id="steam:1446890")
        self.assertEqual(str(record.classification), "SUPPORTED")
        self.assertEqual(record.reason_code, "ok")
        self.assertEqual(record.capture, "not_tested")
        self.assertEqual(record.embedding, "not_attempted")
        self.assertEqual((record.pid, record.handle), (4242, 3000))
        self.assertEqual(record.app_id, "steam:1446890")
        self.assertEqual(record.launch_id, "launch-1")
        self.assertTrue(record.checked_at)
        self.assertTrue(any(item.code == "geometry" and item.measured for item in record.evidence))
        self.assertEqual(record.limitations, ())

    def test_borderless_is_supported_with_a_stated_limitation(self) -> None:
        _api, host = self.host(style=WS_POPUP | WS_VISIBLE, rect=Rect(40, 60, 1280, 720))
        record = host.probe_compatibility(identity())
        self.assertEqual(str(record.classification), "SUPPORTED_WITH_LIMITATIONS")
        self.assertEqual(record.window_mode, "borderless")
        self.assertTrue(record.limitations)

    def test_fullscreen_is_unsupported_and_reads_only(self) -> None:
        api, host = self.host(style=WS_POPUP | WS_VISIBLE, rect=Rect(0, 0, 1280, 720))
        record = host.probe_compatibility(identity())
        self.assertEqual(str(record.classification), "UNSUPPORTED")
        self.assertEqual(record.reason_code, "fullscreen_presentation_unsupported")
        self.assertEqual(api.calls, ["find_window:4242"])
        self.assertEqual(api.windows[3000].style, WS_POPUP | WS_VISIBLE)

    def test_an_owned_child_is_unsupported(self) -> None:
        api = FakeWindowApi(find_any=True)
        windowed_window(api, parent=77, style=WS_CHILD | WS_VISIBLE)
        record = EmbeddedGameHost(api).probe_compatibility(identity())
        self.assertEqual(str(record.classification), "UNSUPPORTED")
        self.assertEqual(record.reason_code, "already_child")

    def test_minimized_or_hidden_is_unknown_not_unsupported(self) -> None:
        cases = (
            ({"minimized": True}, "window_minimized", {}),
            ({"visible": False}, "window_hidden", {"find_hidden": True}),
        )
        for overrides, code, api_options in cases:
            with self.subTest(code=code):
                api = FakeWindowApi(**api_options)
                windowed_window(api, **overrides)
                record = EmbeddedGameHost(api).probe_compatibility(identity())
                self.assertEqual(str(record.classification), "UNKNOWN")
                self.assertEqual(record.reason_code, code)
                self.assertTrue(any(item.measured for item in record.evidence))

    def test_no_window_yet_is_unknown_and_unmeasured(self) -> None:
        host = EmbeddedGameHost(FakeWindowApi())
        record = host.probe_compatibility(identity())
        self.assertEqual(str(record.classification), "UNKNOWN")
        self.assertEqual(record.reason_code, "window_not_found")
        self.assertTrue(
            any(item.code == "window_unmeasured" and not item.measured for item in record.evidence)
        )
        self.assertEqual(record.capture, "not_tested")

    def test_wrong_owner_and_a_reused_pid_are_unknown(self) -> None:
        class WrongOwner(FakeWindowApi):
            """A lookup that hands back another process's window."""

            def find_window(self, pid: int, title_hint: str = "") -> WindowLookup:
                return WindowLookup(3000, 1)

        api = WrongOwner()
        windowed_window(api, pid=9999)
        wrong_owner = EmbeddedGameHost(api).probe_compatibility(identity())
        self.assertEqual(wrong_owner.reason_code, "window_owner_mismatch")
        self.assertEqual(str(wrong_owner.classification), "UNKNOWN")

        api = FakeWindowApi()
        windowed_window(api)
        reused = EmbeddedGameHost(api).probe_compatibility(
            identity(start_time=datetime.fromtimestamp(START + 60, UTC).isoformat())
        )
        self.assertEqual(reused.reason_code, "process_identity_mismatch")
        self.assertEqual(str(reused.classification), "UNKNOWN")

    def test_a_non_windows_host_is_unknown_and_marked_unmeasured(self) -> None:
        host = EmbeddedGameHost(Win32WindowApi(platform="posix"))
        record = host.probe_compatibility(identity())
        self.assertEqual(str(record.classification), "UNKNOWN")
        self.assertEqual(record.reason_code, "platform_unsupported")
        platform_evidence = [item for item in record.evidence if item.code == "platform"]
        self.assertTrue(platform_evidence)
        self.assertFalse(platform_evidence[0].measured)


class CompatibilityAttemptTests(unittest.TestCase):
    """NF-14 Scopes B/E: an attempt refines one record, and never touches capture."""

    def test_a_verified_embed_is_supported_and_leaves_capture_untested(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        result = host.embed(identity(), api.host)
        self.assertTrue(result["embedded"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["embedding"]["applied"], ["style", "parent", "placement"])
        record = host.compatibility
        assert record is not None
        self.assertEqual(str(record.classification), "SUPPORTED")
        self.assertEqual(record.embedding, "embedded")
        self.assertEqual(record.capture, "not_tested")
        status = host.status_payload(
            observation={"available": True, "reason": "a capture backend is wired"}
        )
        self.assertEqual(status["compatibility"]["classification"], "SUPPORTED")
        self.assertEqual(status["compatibility"]["capture"], "not_tested")
        self.assertFalse(status["observation"]["implied_by_embedding"])
        self.assertFalse(status["fallback"]["active"])

    def test_a_repeated_failed_restore_adds_one_limitation(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        record = host.compatibility
        assert record is not None
        apply_restore(record, {"restored": False})
        apply_restore(record, {"restored": False})
        self.assertEqual(
            sum("Detach could not confirm" in note for note in record.limitations), 1
        )

    def test_a_measured_capture_result_never_changes_embedding(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertIsNone(host.note_capture(True))
        host.embed(identity(), api.host)
        failed = host.note_capture(False, error="the capture worker stopped responding")
        assert failed is not None
        self.assertEqual(failed.capture, "unavailable")
        self.assertEqual(str(failed.classification), "SUPPORTED")
        self.assertEqual(failed.embedding, "embedded")
        self.assertEqual(failed.evidence[-1].code, "capture")
        self.assertIn("no valid frame", failed.evidence[-1].detail)
        ok = host.note_capture(True)
        assert ok is not None
        self.assertEqual(ok.capture, "available")
        self.assertEqual(str(ok.classification), "SUPPORTED")
        self.assertEqual(ok.embedding, "embedded")

    def test_a_missing_window_is_unknown_not_unsupported(self) -> None:
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "window_not_found")
        self.assertEqual(result["compatibility"]["classification"], "UNKNOWN")
        self.assertEqual(result["compatibility"]["embedding"], "refused")
        self.assertEqual(result["embedding"]["applied"], [])

    def test_a_platform_refusal_after_an_attempt_stays_unknown(self) -> None:
        api = Win32WindowApi(platform="posix")
        host = EmbeddedGameHost(api)
        result = host.embed(identity(), 1)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "platform_unsupported")
        self.assertEqual(result["compatibility"]["classification"], "UNKNOWN")
        self.assertEqual(result["compatibility"]["embedding"], "refused")

    def test_capture_can_be_available_while_embedding_is_unsupported(self) -> None:
        api = FakeWindowApi()
        windowed_window(api, style=WS_POPUP | WS_VISIBLE, rect=Rect(0, 0, 1280, 720))
        host = EmbeddedGameHost(api)
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["compatibility"]["classification"], "UNSUPPORTED")
        record = host.note_capture(True)
        assert record is not None
        self.assertEqual(record.capture, "available")
        self.assertEqual(str(record.classification), "UNSUPPORTED")
        self.assertEqual(record.embedding, "refused")

    def test_a_fullscreen_refusal_is_unsupported_with_no_applied_steps(self) -> None:
        api = FakeWindowApi()
        windowed_window(api, style=WS_POPUP | WS_VISIBLE, rect=Rect(0, 0, 1280, 720))
        host = EmbeddedGameHost(api)
        # A nonsense host handle must NOT mask the window's own refusal.
        result = host.embed(identity(), 1)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "fullscreen_presentation_unsupported")
        self.assertEqual(result["embedding"]["applied"], [])
        self.assertFalse(result["embedding"]["partial"])
        self.assertFalse(result["embedding"]["recovery"]["attempted"])
        self.assertEqual(result["compatibility"]["classification"], "UNSUPPORTED")
        self.assertEqual(result["compatibility"]["embedding"], "refused")
        self.assertEqual(api.windows[3000].style, WS_POPUP | WS_VISIBLE)

    def test_a_failed_embed_reports_the_partial_mutation_and_restores(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        api.refuse_parent = True
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertTrue(result["embedding"]["partial"])
        self.assertEqual(result["embedding"]["applied"], ["style", "parent"])
        self.assertTrue(result["embedding"]["recovery"]["attempted"])
        self.assertTrue(result["embedding"]["recovery"]["restored"])
        assert host.compatibility is not None
        self.assertEqual(str(host.compatibility.classification), "UNSUPPORTED")
        self.assertEqual(host.compatibility.embedding, "failed")
        self.assertEqual(
            api.windows[3000].style,
            WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
        )

    def test_a_failed_restore_is_reported_not_claimed(self) -> None:
        class StickyChild(FakeWindowApi):
            """Applies the child style once, then refuses every later style write."""

            def set_style(self, handle: int, style: int) -> int:
                applied = super().set_style(handle, style)
                self.refuse_style = True
                return applied

        api = StickyChild()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        api.refuse_parent = True
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertTrue(result["embedding"]["partial"])
        self.assertFalse(result["embedding"]["recovery"]["restored"])
        self.assertIn("NOT restored", result["embedding"]["reason"])
        assert host.compatibility is not None
        self.assertTrue(
            any("could not be confirmed restored" in note for note in host.compatibility.limitations)
        )

    def test_a_window_destroyed_during_embedding_is_unknown_not_unsupported(self) -> None:
        class Vanishing(FakeWindowApi):
            """The game window disappears between the style write and SetParent."""

            def set_parent(self, child: int, parent: int) -> None:
                super().set_parent(child, parent)
                del self.windows[child]

        api = Vanishing()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "window_destroyed")
        self.assertEqual(result["embedding"]["applied"], ["style", "parent"])
        self.assertTrue(result["embedding"]["partial"])
        self.assertIsNone(result["embedding"]["recovery"]["restored"])
        self.assertIn("no window left to restore", result["embedding"]["reason"])
        self.assertEqual(result["compatibility"]["classification"], "UNKNOWN")
        self.assertIsNone(result["compatibility"]["restored"])
        self.assertFalse(
            any(
                "could not be confirmed restored" in note
                for note in result["compatibility"]["limitations"]
            )
        )
        assert host.compatibility is not None
        self.assertEqual(host.compatibility.embedding, "failed")
        cleaned = host.cleanup()
        self.assertTrue(cleaned["cleaned"])
        self.assertFalse(cleaned["changed"])
        self.assertIsNone(cleaned["restored"])

    def test_a_window_that_changes_owner_before_the_mutation_is_not_touched(self) -> None:
        class ChangingOwner(FakeWindowApi):
            reads = 0

            def window(self, handle: int) -> WindowInfo:
                if handle == self.host:
                    return super().window(handle)
                ChangingOwner.reads += 1
                info = super().window(handle)
                if ChangingOwner.reads >= 2:
                    return WindowInfo(
                        handle=info.handle,
                        pid=9999,
                        title=info.title,
                        class_name=info.class_name,
                        style=info.style,
                        ex_style=info.ex_style,
                        rect=info.rect,
                        client=info.client,
                        visible=info.visible,
                        minimized=info.minimized,
                        owner=info.owner,
                        parent=info.parent,
                        dpi=info.dpi,
                    )
                return info

        ChangingOwner.reads = 0
        api = ChangingOwner()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        result = host.embed(identity(), api.host)
        self.assertFalse(result["embedded"])
        self.assertEqual(result["embedding"]["reason_code"], "window_changed_before_embed")
        self.assertEqual(result["embedding"]["applied"], [])
        self.assertFalse(result["embedding"]["partial"])
        self.assertEqual(result["compatibility"]["classification"], "UNKNOWN")
        self.assertEqual(
            api.windows[3000].style,
            WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
        )

    def test_a_refused_second_embed_keeps_the_record_and_the_state(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        first = host.compatibility
        second = host.embed(identity(), api.host + 1)
        self.assertFalse(second["embedded"])
        self.assertEqual(second["embedding"]["reason_code"], "already_embedded")
        self.assertEqual(host.state, "embedded")
        assert first is not None and host.compatibility is not None
        self.assertEqual(host.compatibility.embedding, "embedded")


class RecoveryLifecycleTests(unittest.TestCase):
    """NF-14 Scope C/D: cleanup is idempotent and recovery is never implied."""

    def test_cleanup_is_idempotent(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        first = host.cleanup()
        self.assertTrue(first["cleaned"])
        self.assertTrue(first["changed"])
        self.assertTrue(first["detached"])
        second = host.cleanup()
        self.assertTrue(second["cleaned"])
        self.assertFalse(second["changed"])
        self.assertIsNone(second["restored"])
        self.assertEqual(host.state, "detached")
        self.assertEqual(api.parent(3000), 0)

    def test_cleanup_before_any_attempt_changes_nothing(self) -> None:
        api = FakeWindowApi()
        result = EmbeddedGameHost(api).cleanup()
        self.assertTrue(result["cleaned"])
        self.assertFalse(result["changed"])
        self.assertIsNone(result["restored"])
        self.assertEqual(api.calls, [])

    def test_a_reused_handle_is_not_reported_as_the_game_window(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        host.set_running({"pid": 4242, "start_time": START_ISO, "name": "Shadow Fight Arena"})
        self.assertTrue(host.refresh_window()["window_found"])
        # The cached HWND is reused by another process, and the game's real
        # window is a different handle: the cache must not answer for it.
        api.windows[3000].pid = 9999
        api.add_window(
            handle=5000,
            pid=4242,
            title="Shadow Fight Arena",
            style=WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
            rect=Rect(40, 60, 1280, 720),
        )
        found = host.refresh_window()
        self.assertTrue(found["window_found"])
        self.assertEqual(found["window"]["handle"], 5000)
        self.assertEqual(found["window"]["pid"], 4242)
        self.assertEqual(found["state"], "running")

    def test_a_game_that_minimizes_itself_is_recorded_as_a_limitation(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        api.windows[3000].minimized = True
        payload = host.check()
        self.assertEqual(payload["state"], "detached")
        self.assertEqual(payload["embedding"]["reason_code"], "window_minimized")
        assert host.compatibility is not None
        self.assertEqual(str(host.compatibility.classification), "SUPPORTED_WITH_LIMITATIONS")
        self.assertTrue(
            any("minimized its window" in note for note in host.compatibility.limitations)
        )
        self.assertTrue(any(item.code == "window_minimized" for item in host.compatibility.evidence))
        self.assertTrue(host.compatibility.restored)
        self.assertTrue(host.compatibility.restored)

    def test_a_recreated_window_is_recorded_as_a_limitation(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        del api.windows[3000]
        api.add_window(
            handle=5000,
            pid=4242,
            title="Shadow Fight Arena",
            style=WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
            rect=Rect(40, 60, 1280, 720),
        )
        payload = host.check()
        self.assertEqual(payload["state"], "detached")
        self.assertEqual(payload["embedding"]["reason_code"], "window_replaced")
        assert host.compatibility is not None
        self.assertEqual(str(host.compatibility.classification), "SUPPORTED_WITH_LIMITATIONS")
        self.assertTrue(
            any("destroyed its window" in note for note in host.compatibility.limitations)
        )

    def test_a_window_that_reverses_the_child_style_is_detached_and_restored(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        api.windows[3000].style = WS_CAPTION | WS_VISIBLE
        payload = host.check()
        self.assertEqual(payload["state"], "detached")
        self.assertEqual(payload["embedding"]["reason_code"], "window_style_reverted")
        self.assertEqual(api.parent(3000), 0)
        self.assertEqual(
            api.windows[3000].style,
            WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
        )
        assert host.compatibility is not None
        self.assertEqual(str(host.compatibility.classification), "SUPPORTED_WITH_LIMITATIONS")

    def test_a_failed_check_restore_stays_retryable(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        api.refuse_style = True
        api.windows[3000].minimized = True
        payload = host.check()
        self.assertEqual(payload["state"], "detached")
        self.assertIn("NOT restored", payload["embedding"]["reason"])
        assert host.compatibility is not None
        self.assertFalse(host.compatibility.restored)
        # The saved original state and the handle survived the failed restore,
        # so cleanup can still put the window back once the API cooperates.
        api.refuse_style = False
        api.windows[3000].minimized = False
        cleaned = host.cleanup()
        self.assertTrue(cleaned["cleaned"])
        self.assertTrue(cleaned["changed"])
        self.assertTrue(cleaned["restored"])
        self.assertEqual(api.parent(3000), 0)
        self.assertEqual(
            api.windows[3000].style,
            WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_VISIBLE,
        )

    def test_a_window_that_appears_after_the_launch_is_adopted(self) -> None:
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        host.set_running({"pid": 4242, "start_time": START_ISO, "name": "Shadow Fight Arena"})
        self.assertIsNone(host.status_payload()["window"])
        self.assertFalse(host.refresh_window()["window_found"])
        windowed_window(api)
        found = host.refresh_window()
        self.assertTrue(found["window_found"])
        self.assertEqual(found["state"], "running")
        self.assertEqual(found["window"]["handle"], 3000)
        self.assertEqual(found["window_mode"], "windowed")
        self.assertEqual(found["embedding"]["reason_code"], "window_detected")

    def test_a_reused_pid_is_not_adopted_as_the_window(self) -> None:
        api = FakeWindowApi()
        host = EmbeddedGameHost(api)
        host.set_running({
            "pid": 4242,
            "start_time": datetime.fromtimestamp(START - 60, UTC).isoformat(),
            "name": "Shadow Fight Arena",
        })
        windowed_window(api)
        result = host.refresh_window()
        self.assertFalse(result["window_found"])
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["embedding"]["reason_code"], "process_identity_changed")

    def test_a_foreign_window_is_never_adopted(self) -> None:
        class WrongOwner(FakeWindowApi):
            def find_window(self, pid: int, title_hint: str = "") -> WindowLookup:
                return WindowLookup(3000, 1)

        api = WrongOwner()
        host = EmbeddedGameHost(api)
        host.set_running({"pid": 4242, "start_time": START_ISO, "name": "Shadow Fight Arena"})
        windowed_window(api, pid=9999)
        result = host.refresh_window()
        self.assertFalse(result["window_found"])
        self.assertIn("not the requested pid", result["reason"])

    def test_a_destroyed_window_ends_the_embedding_without_lying(self) -> None:
        api = FakeWindowApi()
        windowed_window(api)
        host = EmbeddedGameHost(api)
        self.assertTrue(host.embed(identity(), api.host)["embedded"])
        del api.windows[3000]
        payload = host.check()
        self.assertEqual(payload["state"], "closed")
        self.assertEqual(payload["embedding"]["reason_code"], "process_exited")

    def test_the_fallback_offers_the_desktop_workflow(self) -> None:
        api = FakeWindowApi()
        windowed_window(api, style=WS_POPUP | WS_VISIBLE, rect=Rect(0, 0, 1280, 720))
        host = EmbeddedGameHost(api)
        host.embed(identity(), api.host)
        fallback = host.status_payload()["fallback"]
        self.assertTrue(fallback["active"])
        self.assertEqual(fallback["workflow"], "desktop")
        self.assertFalse(fallback["native_embedding"])
        self.assertTrue(any("Game Library" in step for step in fallback["next_steps"]))
        self.assertTrue(any("Windowed" in step for step in fallback["next_steps"]))


class CompatibilityLogTests(unittest.TestCase):
    """NF-14 Scope A: records carry evidence, persist, and never fail the check."""

    class Store:
        def __init__(self) -> None:
            self.data: dict[str, dict[str, object]] = {}

        def read(self, name: str) -> dict[str, object]:
            return dict(self.data.get(name) or {})

        def write(self, name: str, payload: dict[str, object]) -> None:
            self.data[name] = payload

    def record_for(self, game: str = "Shadow Fight Arena") -> CompatibilityRecord:
        api = FakeWindowApi()
        windowed_window(api, title=game)
        host = EmbeddedGameHost(api)
        game_identity = identity(name=game, window_title=game)
        return classify_preflight(game_identity, host.preflight(game_identity))

    def test_records_persist_and_latest_matches_the_game(self) -> None:
        store = self.Store()
        log = CompatibilityLog(store)
        log.record(self.record_for())
        self.assertIn("novaforge_embedding_compat", store.data)
        self.assertEqual(log.latest("shadow fight arena").reason_code, "ok")
        reloaded = CompatibilityLog(store)
        self.assertEqual(len(reloaded.all()), 1)
        latest = reloaded.latest("Shadow Fight Arena")
        assert latest is not None
        self.assertEqual(str(latest.classification), "SUPPORTED")
        self.assertEqual(latest.capture, "not_tested")

    def test_a_recorded_evidence_row_is_a_snapshot(self) -> None:
        log = CompatibilityLog()
        record = self.record_for()
        stored = log.record(record)
        self.assertIsNot(stored, record)
        record.classification = CompatibilityClass.UNSUPPORTED
        record.limitations = ("changed after recording",)
        self.assertEqual(log.latest().classification, CompatibilityClass.SUPPORTED)
        self.assertEqual(log.latest().limitations, ())

    def test_the_log_is_capped(self) -> None:
        log = CompatibilityLog()
        for index in range(CompatibilityLog.MAX_RECORDS + 5):
            log.record(self.record_for(f"Game {index}"))
        self.assertEqual(len(log.all()), CompatibilityLog.MAX_RECORDS)
        self.assertEqual(log.all()[0].game, f"Game {CompatibilityLog.MAX_RECORDS + 4}")

    def test_a_broken_store_never_fails_a_record(self) -> None:
        class Broken:
            def read(self, name: str) -> dict[str, object]:
                raise OSError("unreadable")

            def write(self, name: str, payload: dict[str, object]) -> None:
                raise OSError("unwritable")

        log = CompatibilityLog(Broken())
        record = log.record(self.record_for())
        self.assertEqual(len(log.all()), 1)
        self.assertEqual(str(record.classification), "SUPPORTED")

    def test_a_corrupt_row_does_not_lose_the_readable_history(self) -> None:
        store = self.Store()
        store.data[CompatibilityLog.NAMESPACE] = {
            "records": [
                {"game": "Bad", "identity": {"pid": "not-a-number"}, "evidence": 3},
                {"game": "Good", "classification": "SUPPORTED"},
            ]
        }
        log = CompatibilityLog(store)
        self.assertEqual([record.game for record in log.all()], ["Good"])
        tolerant = CompatibilityRecord.from_dict(
            {"identity": {"pid": "x", "handle": "y"}, "dpi": "z"}
        )
        self.assertEqual((tolerant.pid, tolerant.handle, tolerant.dpi), (0, 0, 0))

    def test_unknown_persisted_values_do_not_raise(self) -> None:
        record = CompatibilityRecord.from_dict(
            {"game": "X", "classification": "SOMETHING_ELSE", "evidence": [{"code": "a"}]}
        )
        self.assertEqual(str(record.classification), "UNKNOWN")
        self.assertEqual(record.capture, "not_tested")
        self.assertEqual(record.embedding, "not_attempted")


if __name__ == "__main__":
    unittest.main()
