"""Capture backends, frame validation, and the false-live refusal.

Two things are pinned here, and they are different questions:

* the BACKEND CONTRACT — every source implements the same small interface
  (declared capability, availability, one frame) and reports failure instead of
  raising;
* the VALIDATION CONTRACT — capture success, target visibility, freshness and
  content are four separate facts, and only their conjunction may be shown as
  a live game frame. H02 is the recorded failure: a fresh PNG of the desktop was
  labelled ``LIVE`` gameplay after the game was minimized.

Nothing here touches a real screen or a real game: the seams are fakes that
write PNG files, so the same suite runs on any OS.
"""

from __future__ import annotations

import asyncio
import random
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image

from novacontrol.desktop.capture_backends import (
    CaptureRouter,
    CaptureTarget,
    DisplayCropBackend,
    FrameCapture,
    WindowSurfaceBackend,
    crop_to_bounds,
)
from novacontrol.desktop.capture_validation import (
    THUMB_BYTES,
    THUMB_HEIGHT,
    THUMB_SUFFIX,
    THUMB_WIDTH,
    FrameInspection,
    inspect_frame,
    reason_code,
    validate_frame,
)
from novacontrol.novaforge import NovaForgeManager, SessionState
from novacontrol.novaforge.models import FrameRecord

WINDOW_TITLE = "NC CAPTURE CONTRACT - disposable"
SCREEN = (900, 700)
WINDOW = (100, 50, 500, 350)


def textured(path: str, size: tuple[int, int] = (400, 300), *, seed: int = 7) -> None:
    """Write a PAINTED image: noise, so the blank/uniform check must pass it."""
    rand = random.Random(seed)
    image = Image.new("RGB", size)
    image.putdata(
        [
            (rand.randrange(256), rand.randrange(256), rand.randrange(256))
            for _ in range(size[0] * size[1])
        ]
    )
    image.save(path, format="PNG")


def solid(
    path: str, color: tuple[int, int, int] = (0, 0, 0), size: tuple[int, int] = (400, 300)
) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


def write_thumb(path: str, colors: tuple[tuple[int, int, int], ...]) -> str:
    """A worker-shaped paint side car: 48x27 of the given colours, BGR, cycled.

    A SINGLE colour is a uniform fill, which the blank check refuses by design;
    pass two or more to describe painted content.
    """
    body = b"".join(bytes((blue, green, red)) for red, green, blue in colors)
    total = THUMB_WIDTH * THUMB_HEIGHT
    repeats, rest = divmod(total, len(colors))
    with open(path, "wb") as handle:
        handle.write(body * repeats + body[: rest * 3])
    return path


def thumbnail_of(source: str, thumb_path: str) -> str:
    """The side car the worker WOULD write for the image at ``source``.

    GDI+ draws the source straight into a 48x27 surface with HighQualityBicubic,
    so that is what this emulates; the point of the test is that the two paths
    reach the same verdict, not that the resamplers are bit-identical.
    """
    with Image.open(source) as image:
        reduced = image.convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT), Image.BICUBIC)
    with open(thumb_path, "wb") as handle:
        handle.write(bytes(c for r, g, b in reduced.getdata() for c in (b, g, r)))
    return thumb_path


@dataclass
class FakeDesktop:
    """A window + capture double that writes real PNG files."""

    title: str = WINDOW_TITLE
    handle: int = 0x2222
    pid: int = 5151
    bounds: tuple[int, int, int, int] = WINDOW
    foreground: bool = True
    minimized: bool = False
    visible: bool = True
    screen: tuple[int, int] = SCREEN

    def __post_init__(self) -> None:
        self.capture_calls: list[str] = []
        self.window_calls: list[int] = []
        self.fail_display = False
        self.fail_window = False
        #: When set, the window seam answers a structured refusal with this code
        #: instead of raising (a window that vanished or cannot be rendered).
        self.refuse_window = ""
        self.paint_window: tuple[int, int, int] | None = None
        self.paint_display: tuple[int, int, int] | None = None
        #: When set, the window seam also writes this solid colour as the 48x27
        #: paint side car and reports its path — the shape the real worker now
        #: produces for a caller that asked for one.
        self.thumb_window: tuple[tuple[int, int, int], ...] | None = None

    async def capture(self, path: str) -> dict[str, Any]:
        self.capture_calls.append(path)
        if self.fail_display:
            raise RuntimeError("display capture exploded")
        if self.paint_display is not None:
            # A display that is black where the target is: the second signal a
            # dark-frame corroboration needs.
            solid(path, self.paint_display, self.screen)
        else:
            # The composited desktop: painted, so content validation judges the CROP.
            textured(path, self.screen)
        return {"fresh": True, "capture_ms": 6.0, "verification": "frame written now"}

    async def capture_window(self, handle: int, path: str) -> Any:
        self.window_calls.append(handle)
        if self.fail_window:
            raise RuntimeError("printwindow exploded")
        if self.refuse_window:
            return {"ok": False, "error": self.refuse_window}
        if self.paint_window is not None:
            solid(path, self.paint_window, (WINDOW[2] - WINDOW[0], WINDOW[3] - WINDOW[1]))
        else:
            textured(path, (WINDOW[2] - WINDOW[0], WINDOW[3] - WINDOW[1]), seed=11)
        thumbnail = ""
        if self.thumb_window is not None:
            thumbnail = write_thumb(f"{path}{THUMB_SUFFIX}", self.thumb_window)
        return {
            "ok": True,
            "width": WINDOW[2] - WINDOW[0],
            "height": WINDOW[3] - WINDOW[1],
            "size_bytes": Path(path).stat().st_size,
            "elapsed_ms": 4.0,
            "error": "",
            "thumbnail": thumbnail,
        }

    def target(self, **overrides: Any) -> CaptureTarget:
        values = {
            "handle": self.handle,
            "title": self.title,
            "bounds": self.bounds,
            "minimized": self.minimized,
            "visible": self.visible,
            "foreground": self.foreground,
        }
        values.update(overrides)
        return CaptureTarget(**values)

    def router(self, *, content_inspection: bool = True) -> CaptureRouter:
        return CaptureRouter(
            (WindowSurfaceBackend(self.capture_window), DisplayCropBackend(self.capture)),
            content_inspection=content_inspection,
        )


class FrameValidationTests(unittest.TestCase):
    """The four facts are separate, and only their conjunction is live."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "frame.png")

    def inspection(self, *, blank: bool = False, repeated: bool = False) -> FrameInspection:
        return FrameInspection(
            readable=True, width=400, height=300, mean_luma=90.0,
            non_black_ratio=0.9, uniform_ratio=0.1, content_hash="hash",
            blank=blank, repeated=repeated,
        )

    def test_a_full_screen_fallback_is_never_a_frame_of_the_target(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="gdi_display", source="screen",
            inspection=self.inspection(),
        )
        self.assertFalse(verdict.livable)
        self.assertFalse(verdict.content_ok)
        self.assertEqual(verdict.content, "not_target")
        self.assertIn("full screen", verdict.reason)

    def test_a_desktop_crop_of_a_background_window_is_not_live(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="gdi_display", source="window_crop",
            foreground=False, inspection=self.inspection(),
        )
        self.assertFalse(verdict.livable)
        self.assertIn("not foreground", verdict.reason)

    def test_a_desktop_crop_of_a_foreground_window_can_be_live(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="gdi_display", source="window_crop",
            foreground=True, inspection=self.inspection(), age_seconds=0.2,
        )
        self.assertTrue(verdict.livable)
        self.assertTrue(verdict.content_ok)
        self.assertEqual(verdict.content, "valid")

    def test_a_window_surface_frame_is_live_while_background(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="printwindow", source="window_surface",
            foreground=False, inspection=self.inspection(), age_seconds=0.2,
        )
        self.assertTrue(verdict.livable)
        self.assertEqual(verdict.backend, "printwindow")

    def test_a_blank_surface_is_reported_not_called_live(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="printwindow", source="window_surface",
            foreground=False, inspection=self.inspection(blank=True),
        )
        self.assertFalse(verdict.livable)
        self.assertEqual(verdict.content, "blank")
        self.assertTrue(verdict.limitations)

    def test_a_blank_frame_confirmed_by_an_independent_capture_is_a_dark_read(self) -> None:
        """A dark scene and a refused surface are the same pixels in ONE image.

        With a second, independent capture agreeing the region is black, the
        frame is served as a dark read: live while fresh, never described as
        painted content, and always carrying the limitation that says how it was
        decided.
        """
        verdict = validate_frame(
            capture_ok=True, backend="gdi_display", source="window_crop",
            foreground=True, inspection=self.inspection(blank=True),
            age_seconds=0.2, dark_confirmed=True,
        )
        self.assertTrue(verdict.livable)
        self.assertTrue(verdict.content_ok)
        self.assertEqual(verdict.content, "dark")
        self.assertIn("black", " ".join(verdict.limitations).lower())
        self.assertNotEqual(verdict.content, "valid", "a dark read is not painted content")

    def test_a_stale_dark_read_is_kept_but_is_not_live(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="gdi_display", source="window_crop",
            foreground=True, inspection=self.inspection(blank=True),
            age_seconds=9.0, max_age_seconds=3.0, dark_confirmed=True,
        )
        self.assertFalse(verdict.livable)
        self.assertEqual(verdict.content, "dark")
        self.assertEqual(verdict.reason, "The frame is stale.")

    def test_an_uninspected_frame_is_unverified_not_live(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="printwindow", source="window_surface",
            foreground=False, inspection=None,
        )
        self.assertFalse(verdict.livable)
        self.assertEqual(verdict.content, "unverified")

    def test_a_stale_frame_keeps_its_content_verdict_but_is_not_live(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="printwindow", source="window_surface",
            foreground=False, inspection=self.inspection(), age_seconds=30.0,
            max_age_seconds=3.0,
        )
        self.assertFalse(verdict.livable)
        self.assertTrue(verdict.content_ok)
        self.assertFalse(verdict.fresh)

    def test_a_repeated_frame_is_live_but_says_it_may_be_frozen(self) -> None:
        verdict = validate_frame(
            capture_ok=True, backend="printwindow", source="window_surface",
            foreground=False, inspection=self.inspection(repeated=True),
        )
        self.assertTrue(verdict.livable)
        self.assertEqual(verdict.content, "repeated")
        self.assertTrue(any("identical" in item for item in verdict.limitations))

    def test_capture_failure_is_not_a_target_visibility_failure(self) -> None:
        verdict = validate_frame(capture_ok=False, error="printwindow refused")
        self.assertFalse(verdict.livable)
        self.assertFalse(verdict.capture_ok)
        self.assertTrue(verdict.target_visible, "a failed capture says nothing about visibility")

    def test_a_minimized_target_is_its_own_verdict(self) -> None:
        verdict = validate_frame(
            capture_ok=True, target_visible=False,
            visibility_reason="The target window is minimized.",
        )
        self.assertFalse(verdict.livable)
        self.assertFalse(verdict.target_visible)
        self.assertTrue(verdict.capture_ok)


class FrameInspectionTests(unittest.TestCase):
    """The pixel checks are measurements, and they must be repeatable."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, name: str, maker) -> str:
        path = str(Path(self.temp.name) / name)
        maker(path)
        return path

    def test_a_painted_frame_is_readable_and_not_blank(self) -> None:
        inspection = inspect_frame(self.write("painted.png", textured))
        self.assertTrue(inspection.readable)
        self.assertFalse(inspection.blank)
        self.assertEqual((inspection.width, inspection.height), (400, 300))
        self.assertGreater(inspection.non_black_ratio or 0.0, 0.5)
        self.assertEqual(len(inspection.content_hash), 64)

    def test_a_solid_black_frame_is_blank(self) -> None:
        inspection = inspect_frame(self.write("black.png", solid))
        self.assertTrue(inspection.blank)
        self.assertTrue(inspection.reason)

    def test_a_solid_light_frame_is_blank_too(self) -> None:
        inspection = inspect_frame(
            self.write("white.png", lambda path: solid(path, (255, 255, 255)))
        )
        self.assertTrue(inspection.blank, "a uniform fill is how a refused render answers")

    def test_the_same_image_twice_is_reported_as_repeated(self) -> None:
        path = self.write("painted.png", textured)
        first = inspect_frame(path)
        second = inspect_frame(path, previous_hash=first.content_hash)
        self.assertTrue(second.repeated)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_an_unreadable_file_is_not_optimistically_valid(self) -> None:
        path = Path(self.temp.name) / "broken.png"
        path.write_bytes(b"not a png")
        inspection = inspect_frame(path)
        self.assertFalse(inspection.readable)
        self.assertTrue(inspection.reason)

    def test_a_missing_file_is_a_reported_gap(self) -> None:
        inspection = inspect_frame(Path(self.temp.name) / "absent.png")
        self.assertFalse(inspection.readable)

    def test_reason_codes_are_stable_and_specific(self) -> None:
        self.assertEqual(reason_code("The target window is minimized."), "target_minimized")
        self.assertEqual(
            reason_code(
                "The target window is outside the captured display; showing the full screen."
            ),
            "target_offscreen",
        )
        self.assertEqual(reason_code("The frame is essentially blank."), "target_unreadable")
        self.assertEqual(
            reason_code(
                "The target is not foreground, so a desktop crop may contain another window."
            ),
            "target_not_foreground",
        )
        self.assertEqual(reason_code(""), "capture_rejected")
        # A token that is ALREADY a code survives unchanged: re-reading a code as
        # prose is how a backend's "target_unreadable" collapsed to the generic
        # "capture_rejected" and lost the diagnosis the Guardian pauses on.
        for token in ("target_unreadable", "capture_timeout", "target_offscreen"):
            self.assertEqual(reason_code(token), token)
        self.assertEqual(reason_code(" target_unreadable "), "target_unreadable")


class ThumbnailInspectionTests(unittest.TestCase):
    """The paint side car is the same statistics from 3888 bytes, or nothing.

    Its whole purpose is to take a 5.2 MP PNG decode off the per-frame path
    without changing what the verdicts mean, so these tests pin BOTH halves:
    the verdict agrees with the full decode, and the reader's byte order,
    dimensions and degradation rules are exactly as documented.
    """

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = str(self.directory / "frame.png")
        self.thumb = str(self.directory / ("frame.png" + THUMB_SUFFIX))

    def test_a_painted_frame_reads_the_same_through_both_paths(self) -> None:
        textured(self.path, (640, 480))
        thumbnail_of(self.path, self.thumb)
        full = inspect_frame(self.path)
        side = inspect_frame(self.path, thumbnail=self.thumb)
        self.assertTrue(full.readable and side.readable)
        self.assertFalse(full.blank)
        self.assertFalse(side.blank)
        self.assertAlmostEqual(
            full.non_black_ratio or 0.0, side.non_black_ratio or 0.0, delta=0.1
        )

    def test_a_black_frame_is_blank_through_both_paths(self) -> None:
        solid(self.path, (0, 0, 0), (640, 480))
        thumbnail_of(self.path, self.thumb)
        self.assertTrue(inspect_frame(self.path).blank)
        self.assertTrue(inspect_frame(self.path, thumbnail=self.thumb).blank)

    def test_the_frames_own_dimensions_come_from_the_frame(self) -> None:
        """The side car is 48x27; the frame it describes is not."""
        textured(self.path, (640, 480))
        thumbnail_of(self.path, self.thumb)
        side = inspect_frame(self.path, thumbnail=self.thumb)
        self.assertEqual((side.width, side.height), (640, 480))

    def test_a_truncated_or_missing_side_car_degrades_to_the_frame(self) -> None:
        textured(self.path, (640, 480))
        with open(self.thumb, "wb") as handle:
            handle.write(b"\x00\x01\x02")
        short = inspect_frame(self.path, thumbnail=self.thumb)
        missing = inspect_frame(self.path, thumbnail=str(self.directory / "nope.thumb"))
        for verdict in (short, missing):
            self.assertTrue(verdict.readable)
            self.assertFalse(verdict.blank)

    def test_the_side_car_bytes_are_read_as_bgr(self) -> None:
        """GDI+ writes 24bppRgb as B,G,R; reading it as R,G,B would swap the
        channels and change every luma. Red and blue are far apart on purpose:
        luma(red) = 54, luma(blue) = 18, so a half-red half-black image tells
        the two orders apart."""
        self.assertEqual(THUMB_BYTES, THUMB_WIDTH * THUMB_HEIGHT * 3)
        solid(self.path, (10, 20, 30), (640, 480))  # the frame the side car describes
        write_thumb(self.thumb, ((255, 0, 0), (0, 0, 0)))  # half red, half black
        verdict = inspect_frame(self.path, thumbnail=self.thumb)
        # 27.1 is what a channel-swapped (blue) read would produce; 36.5 is not.
        self.assertGreater(verdict.mean_luma or 0.0, 20.0)
        self.assertFalse(verdict.blank, "half the read is painted")


class _BackendContractMixin:
    """The suite every backend must pass to be interchangeable.

    Concrete classes mix this with ``unittest.IsolatedAsyncioTestCase`` and
    provide ``make_backend``. Nothing here reaches the OS.
    """

    def make_backend(self, desktop: FakeDesktop):  # pragma: no cover - overridden
        raise NotImplementedError

    def expected_source(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def test_capabilities_are_declared_data(self) -> None:
        backend = self.make_backend(FakeDesktop())
        payload = backend.capabilities().to_dict()
        self.assertEqual(payload["backend"], backend.name)
        self.assertIn(payload["captures"], {"window_surface", "display"})
        self.assertEqual(
            payload["requires_foreground"], not payload["unfocused"],
            "a backend that does not need the foreground must declare it can see a "
            "background target",
        )
        self.assertTrue(payload["notes"], "a capability must say what it cannot do")

    def test_availability_reports_the_wiring(self) -> None:
        backend = self.make_backend(FakeDesktop())
        available, reason = backend.available()
        self.assertTrue(available)
        self.assertEqual(reason, "")

    def test_an_unwired_seam_is_unavailable_with_a_reason(self) -> None:
        backend = type(self.make_backend(FakeDesktop()))(None)  # type: ignore[arg-type]
        available, reason = backend.available()
        self.assertFalse(available)
        self.assertTrue(reason)

    async def test_one_frame_is_reported_with_its_metadata(self) -> None:
        desktop = FakeDesktop()
        backend = self.make_backend(desktop)
        path = str(Path(self.temp.name) / "frame.png")
        result = await backend.capture_frame(path, desktop.target())
        self.assertIsInstance(result, FrameCapture)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.backend, backend.name)
        self.assertEqual(result.source, self.expected_source())
        self.assertGreater(result.width, 0)
        self.assertGreater(result.height, 0)
        self.assertGreater(result.size_bytes, 0)
        self.assertTrue(Path(path).exists())

    async def test_a_raising_seam_is_a_reported_failure(self) -> None:
        desktop = FakeDesktop()
        backend = self.make_backend(desktop)
        if isinstance(backend, WindowSurfaceBackend):
            desktop.fail_window = True
        else:
            desktop.fail_display = True
        result = await backend.capture_frame(str(Path(self.temp.name) / "f.png"), desktop.target())
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error)

    async def test_a_minimized_target_is_refused(self) -> None:
        desktop = FakeDesktop(minimized=True)
        backend = self.make_backend(desktop)
        result = await backend.capture_frame(
            str(Path(self.temp.name) / "f.png"), desktop.target(minimized=True)
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def setUp(self) -> None:  # noqa: D401 - unittest hook
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)


class DisplayCropBackendContract(_BackendContractMixin, unittest.IsolatedAsyncioTestCase):
    """The historical source: full-display copy cropped to the window."""

    def make_backend(self, desktop: FakeDesktop) -> DisplayCropBackend:
        return DisplayCropBackend(desktop.capture)

    def expected_source(self) -> str:
        return "window_crop"

    async def test_a_window_outside_the_display_is_refused_not_shrunk(self) -> None:
        desktop = FakeDesktop(bounds=(5000, 5000, 5600, 5400))
        backend = self.make_backend(desktop)
        result = await backend.capture_frame(
            str(Path(self.temp.name) / "f.png"), desktop.target(bounds=(5000, 5000, 5600, 5400))
        )
        self.assertFalse(result.ok, "a full-screen fallback is not the target")
        self.assertIn("outside the captured display", result.error)

    async def test_a_stale_display_capture_is_a_failure(self) -> None:
        desktop = FakeDesktop()

        async def stale(path: str) -> dict[str, Any]:
            textured(path, desktop.screen)
            return {"fresh": False, "verification": "the file was not rewritten", "capture_ms": 1.0}

        backend = DisplayCropBackend(stale)
        result = await backend.capture_frame(str(Path(self.temp.name) / "f.png"), desktop.target())
        self.assertFalse(result.ok)
        self.assertIn("not rewritten", result.error)


class WindowSurfaceBackendContract(_BackendContractMixin, unittest.IsolatedAsyncioTestCase):
    """The window's own surface, via PrintWindow."""

    def make_backend(self, desktop: FakeDesktop) -> WindowSurfaceBackend:
        return WindowSurfaceBackend(desktop.capture_window)

    def expected_source(self) -> str:
        return "window_surface"

    async def test_a_refused_printwindow_is_a_failure_not_a_black_frame(self) -> None:
        desktop = FakeDesktop()

        async def refused(handle: int, path: str) -> Any:
            return {"ok": False, "error": "printwindow_refused"}

        backend = WindowSurfaceBackend(refused)
        result = await backend.capture_frame(str(Path(self.temp.name) / "f.png"), desktop.target())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "printwindow_refused")


class CropTests(unittest.TestCase):
    """Cropping is a measurement, and an impossible crop must say so."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_crop_inside_the_display_reports_window_crop(self) -> None:
        path = Path(self.temp.name) / "f.png"
        textured(str(path), (600, 400))
        width, height, size, source, note = crop_to_bounds(path, (100, 50, 300, 250))
        self.assertEqual(source, "window_crop")
        self.assertEqual((width, height), (200, 200))
        self.assertGreater(size, 0)
        self.assertEqual(note, "")

    def test_a_clipped_crop_says_it_is_clipped(self) -> None:
        path = Path(self.temp.name) / "f.png"
        textured(str(path), (600, 400))
        _, _, _, source, note = crop_to_bounds(path, (-50, -20, 300, 250))
        self.assertEqual(source, "window_crop")
        self.assertIn("clipped", note)

    def test_a_crop_with_no_usable_area_falls_back_truthfully(self) -> None:
        path = Path(self.temp.name) / "f.png"
        textured(str(path), (600, 400))
        _, _, _, source, note = crop_to_bounds(path, (5000, 5000, 5600, 5400))
        self.assertEqual(source, "screen")
        self.assertIn("outside the captured display", note)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    """Selection is by declared capability, and refusal is a result."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "frame.png")

    async def test_a_covered_target_is_captured_from_its_own_surface(self) -> None:
        desktop = FakeDesktop(foreground=False)
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.backend, "printwindow")
        self.assertEqual(outcome.source, "window_surface")
        self.assertTrue(outcome.verdict["livable"])
        self.assertEqual(desktop.capture_calls, [], "the desktop was never copied")

    async def test_a_blank_surface_falls_through_to_the_desktop_crop_when_focused(self) -> None:
        desktop = FakeDesktop(foreground=True)
        desktop.paint_window = (0, 0, 0)  # a renderer that refuses to paint
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.backend, "gdi_display")
        self.assertEqual(outcome.source, "window_crop")
        self.assertEqual([row["ok"] for row in outcome.attempts], [False, True])

    async def test_a_blank_surface_with_a_background_target_is_refused(self) -> None:
        desktop = FakeDesktop(foreground=False)
        desktop.paint_window = (0, 0, 0)
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_unreadable")
        self.assertFalse(outcome.verdict["livable"])
        self.assertIn("blank", outcome.reason.lower())

    async def test_two_backends_that_both_read_black_are_a_dark_read(self) -> None:
        """Both layers agree on black: a dark scene, not an unpainted surface.

        The window refused to paint into a bitmap AND the compositor shows black
        where the window is. Two independent captures agreeing is the second
        signal, so the frame is served as a dark read — with the limitation that
        says so — rather than pausing a session on a fade or a loading screen.
        """
        desktop = FakeDesktop(foreground=True)
        desktop.paint_window = (0, 0, 0)
        desktop.paint_display = (0, 0, 0)
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.verdict["content"], "dark")
        self.assertTrue(outcome.verdict["livable"])
        self.assertTrue(outcome.verdict["limitations"])
        self.assertIn("black", outcome.reason.lower() or "black")
        self.assertEqual([row["ok"] for row in outcome.attempts], [False, True])
        self.assertEqual(
            sorted(outcome.attempts[-1]["agreement"]), ["gdi_display", "printwindow"]
        )
        # The evidence on disk really is black: the dark read is not a claim.
        self.assertTrue(inspect_frame(self.path).blank)

    async def test_a_black_region_with_one_backend_alone_is_still_a_refusal(self) -> None:
        """One black image is one signal, and one signal cannot tell black from
        unpainted. With a single backend wired there is nothing to corroborate
        with, so the refusal — and its pause code — stand."""
        desktop = FakeDesktop(foreground=True)
        desktop.paint_display = (0, 0, 0)
        router = CaptureRouter((DisplayCropBackend(desktop.capture),))
        outcome = await router.capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_unreadable")
        # The refusal envelope reports the condition code; the attempt row keeps
        # what the single backend actually read.
        self.assertEqual(outcome.attempts[-1]["content"], "blank")
        self.assertIn("blank", outcome.reason.lower())

    async def test_a_backend_that_produced_no_frame_does_not_claim_a_capture(self) -> None:
        """Measured in NF-23: a vanished/unreadable window produced NO frame, yet
        the refusal claimed `capture_ok` and reported `content="not_target"`, so
        the panel diagnosed an identity mismatch ("the frame is not the target")
        for a frame that never existed.

        A refusal with no pixels must say so, and must carry the backend's own
        code — not a code re-derived from its prose.
        """
        desktop = FakeDesktop(foreground=True)
        desktop.refuse_window = "target_unreadable"
        router = CaptureRouter((WindowSurfaceBackend(desktop.capture_window),))
        outcome = await router.capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.verdict["capture_ok"])
        self.assertEqual(outcome.verdict["code"], "target_unreadable")
        self.assertEqual(outcome.attempts[0]["code"], "target_unreadable")
        self.assertFalse(outcome.verdict["livable"])
        # No pixels were read, so the envelope must not describe a frame: with
        # the old unconditional "not_target" the panel diagnosed an identity
        # mismatch against a frame that never existed.
        self.assertEqual(outcome.verdict["content"], "unverified")

    async def test_a_blank_frame_still_reports_that_a_frame_existed(self) -> None:
        """The other half of the same contract: a validated rejection (blank
        content) DID produce pixels, so `capture_ok` stays true — the frame was
        there and the panel's diagnosis is about its content."""
        desktop = FakeDesktop(foreground=True)
        desktop.paint_window = (0, 0, 0)
        desktop.paint_display = None
        router = CaptureRouter((WindowSurfaceBackend(desktop.capture_window),))
        outcome = await router.capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.verdict["capture_ok"])
        self.assertEqual(outcome.verdict["content"], "blank")
        self.assertEqual(outcome.verdict["code"], "target_unreadable")

    async def test_an_uninspected_frame_is_not_a_backend_that_produced_nothing(self) -> None:
        """Content inspection can be switched off, and then a frame exists but has
        no verdict. `capture_ok` answers "did a backend produce pixels?", so the
        refusal must not claim no backend produced a frame — and with no read to
        describe, its content is UNVERIFIED rather than an identity claim."""
        desktop = FakeDesktop(foreground=True)
        router = desktop.router(content_inspection=False)
        outcome = await router.capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.verdict["capture_ok"])
        self.assertEqual(outcome.verdict["content"], "unverified")
        self.assertFalse(outcome.verdict["livable"])
        self.assertTrue(Path(self.path).exists())

    async def test_a_backend_that_produced_no_frame_breaks_the_agreement(self) -> None:
        desktop = FakeDesktop(foreground=True)
        desktop.fail_window = True  # the surface never produced an image
        desktop.paint_display = (0, 0, 0)
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_unreadable")
        self.assertEqual(outcome.attempts[0]["ok"], False)

    async def test_a_black_surface_with_a_painted_display_is_not_called_dark(self) -> None:
        """The classic refused-surface case must stay a switch, not a dark read:
        the compositor shows the game, so the black bitmap was the renderer."""
        desktop = FakeDesktop(foreground=True)
        desktop.paint_window = (0, 0, 0)
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.backend, "gdi_display")
        self.assertEqual(outcome.verdict["content"], "valid")
        self.assertEqual([row["ok"] for row in outcome.attempts], [False, True])

    async def test_a_foreground_crop_that_is_only_desktop_is_refused(self) -> None:
        desktop = FakeDesktop(bounds=(5000, 5000, 5600, 5400))
        desktop.fail_window = True
        outcome = await desktop.router().capture(
            self.path, desktop.target(bounds=(5000, 5000, 5600, 5400))
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_offscreen")

    async def test_a_minimized_target_captures_nothing_and_calls_no_backend(self) -> None:
        desktop = FakeDesktop(minimized=True)
        outcome = await desktop.router().capture(self.path, desktop.target(minimized=True))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_minimized")
        self.assertEqual(desktop.window_calls, [])
        self.assertEqual(desktop.capture_calls, [])

    async def test_a_hidden_target_captures_nothing(self) -> None:
        desktop = FakeDesktop(visible=False)
        outcome = await desktop.router().capture(self.path, desktop.target(visible=False))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_hidden")

    async def test_a_repeat_is_reported_as_a_limitation_not_silence(self) -> None:
        desktop = FakeDesktop(foreground=False)
        router = desktop.router()
        first = await router.capture(self.path, desktop.target())
        self.assertTrue(first.ok)
        second = await router.capture(
            self.path, desktop.target(), previous_hash=router.last_content_hash
        )
        self.assertTrue(second.ok)
        self.assertEqual(second.verdict["content"], "repeated")
        self.assertTrue(second.verdict["limitations"])

    async def test_a_frame_clipped_by_the_display_says_so_in_its_note(self) -> None:
        """A window that extends past the display yields a PARTIAL frame.

        The crop still succeeds (the visible part is painted), so `ok` is True —
        but calling that frame the whole target is wrong. The backend reports it
        as a note, and the router must carry it through rather than dropping it:
        NF-09 refuses to describe a clipped frame, and that refusal reads this
        fact.
        """
        bounds = (-40, 20, 400, 300)
        desktop = FakeDesktop(bounds=bounds)
        desktop.fail_window = True  # the display crop is the only eligible source
        outcome = await desktop.router().capture(self.path, desktop.target(bounds=bounds))
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.source, "window_crop")
        self.assertIn("clipped", outcome.note.lower())
        self.assertIn("clipped", outcome.to_dict()["note"].lower())
        self.assertEqual(outcome.width, 400, "the off-display 40 px are not in the frame")

    async def test_an_unclipped_frame_carries_no_clipping_note(self) -> None:
        desktop = FakeDesktop(foreground=True)
        desktop.fail_window = True
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.note, "")

    async def test_no_backend_at_all_is_an_explicit_refusal(self) -> None:
        router = CaptureRouter(())
        desktop = FakeDesktop()
        outcome = await router.capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "capture_unavailable")
        self.assertIn("none wired", outcome.reason)

    async def test_capabilities_list_every_wired_backend_with_availability(self) -> None:
        rows = FakeDesktop().router().capabilities()
        self.assertEqual([row["backend"] for row in rows], ["printwindow", "gdi_display"])
        self.assertTrue(all(row["available"] for row in rows))
        self.assertFalse(rows[0]["requires_foreground"])
        self.assertTrue(rows[1]["requires_foreground"])

    async def test_an_unavailable_backend_is_skipped_with_its_reason(self) -> None:
        desktop = FakeDesktop(foreground=False)
        router = CaptureRouter(
            (WindowSurfaceBackend(None), DisplayCropBackend(desktop.capture))  # type: ignore[arg-type]
        )
        outcome = await router.capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        # The background target is the condition the operator must fix, and the
        # refusal names BOTH facts: no surface seam is wired, and a desktop copy
        # cannot see the window.
        self.assertEqual(outcome.verdict["code"], "target_not_foreground")
        self.assertIn("window-surface seam", outcome.reason)
        self.assertEqual(desktop.capture_calls, [], "a desktop copy is never a background target")


    async def test_a_black_side_car_makes_the_frame_a_blank_refusal(self) -> None:
        """The side car is what is judged, not the PNG next to it.

        The frame is painted but its paint read is black: that is the shape of a
        renderer that refused to paint while a stale/composited image sat in the
        file. If the router judged the PNG instead, this frame would be served
        as painted content.
        """
        desktop = FakeDesktop(foreground=False)
        desktop.thumb_window = ((0, 0, 0),)
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict["code"], "target_unreadable")
        self.assertEqual(outcome.attempts[-1]["content"], "blank")

    async def test_a_painted_side_car_is_what_the_router_judges(self) -> None:
        desktop = FakeDesktop(foreground=False)
        desktop.paint_window = (0, 0, 0)  # the PNG is black...
        desktop.thumb_window = ((200, 40, 40), (10, 60, 200))  # ...the read is not
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.verdict["content"], "valid")

    async def test_the_side_car_is_removed_once_it_has_been_read(self) -> None:
        """It is evidence for one verdict, not a second artefact to keep."""
        desktop = FakeDesktop(foreground=False)
        desktop.thumb_window = ((120, 120, 60), (60, 20, 120))
        side_car = self.path + THUMB_SUFFIX
        self.assertFalse(Path(side_car).exists())
        outcome = await desktop.router().capture(self.path, desktop.target())
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertFalse(
            Path(side_car).exists(), "the paint read must not be left on disk"
        )


def _geometry(desktop: FakeDesktop) -> Any:
    """The shape the desktop layer's WindowGeometry provides."""

    @dataclass
    class Geometry:
        title: str
        handle: int
        pid: int
        bounds: tuple[int, int, int, int]
        client: tuple[int, int, int, int]
        scale: float
        foreground: bool
        minimized: bool
        visible: bool

    return Geometry(
        title=desktop.title,
        handle=desktop.handle,
        pid=desktop.pid,
        bounds=desktop.bounds,
        client=(0, 0, desktop.bounds[2] - desktop.bounds[0], desktop.bounds[3] - desktop.bounds[1]),
        scale=1.0,
        foreground=desktop.foreground,
        minimized=desktop.minimized,
        visible=desktop.visible,
    )


class ManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A session frame carries WHICH backend produced it and WHY it was accepted."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.decoded = Path(self.temp.name)

    def manager(self, desktop: FakeDesktop, *, window_seam: bool = True) -> NovaForgeManager:
        async def geometry(_title: str) -> Any | None:
            return _geometry(desktop)

        async def window_lines() -> list[str] | None:
            return [f"python|{desktop.title}"]

        return NovaForgeManager(
            data_dir=self.decoded,
            capture=desktop.capture,
            capture_window=desktop.capture_window if window_seam else None,
            geometry=geometry,
            window_lines=window_lines,
            frame_interval=0.05,
        )

    def mission(self) -> dict[str, Any]:
        return {
            "name": "Capture contract run",
            "objective": "Observe the disposable window for a few frames.",
            "target_title": WINDOW_TITLE,
            "success_criteria": "Frames are captured.",
            "time_limit_seconds": 20,
            "max_actions": 5,
        }

    async def wait_for(
        self, predicate, *, timeout: float = 4.0, message: str = "condition"
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"Timed out waiting for {message}.")

    async def test_a_background_target_records_a_validated_surface_frame(self) -> None:
        desktop = FakeDesktop(foreground=False)
        manager = self.manager(desktop)
        self.addAsyncCleanup(manager.shutdown)
        session = await manager.start(self.mission())
        session_id = session["session_id"]
        await self.wait_for(lambda: manager.get(session_id).frames, message="a frame")
        record = manager.get(session_id)
        frame = record.frames[-1]
        self.assertEqual(frame.backend, "printwindow")
        self.assertEqual(frame.source, "window_surface")
        self.assertTrue(frame.validation.get("livable"))
        self.assertEqual(frame.validation.get("backend"), "printwindow")
        payload = manager.describe(record)["frame"]
        self.assertEqual(payload["backend"], "printwindow")
        self.assertTrue(payload["validation"]["content_ok"])
        await manager.stop(session_id)

    async def test_a_clipped_frame_is_reported_as_clipped_by_the_manager(self) -> None:
        """The NF-15 refactor must not lose NF-09's clipped-frame fact."""
        from novacontrol.novaforge.manager import _target_from_geometry
        from novacontrol.novaforge.observer import window_state

        bounds = (-40, 20, 400, 300)
        desktop = FakeDesktop(bounds=bounds)
        desktop.fail_window = True
        manager = self.manager(desktop)
        self.addAsyncCleanup(manager.shutdown)
        state = window_state(_geometry(desktop))
        assert state is not None
        path = self.decoded / "clipped.png"
        frame = await manager.capture_observation(state, str(path))
        self.assertTrue(frame.ok, frame.reason)
        self.assertTrue(frame.clipped, "a partial frame is not the whole target")
        self.assertIn("clipped", frame.note.lower())
        payload = await manager._capture_observation_frame(  # noqa: SLF001
            _target_from_geometry(_geometry(desktop))
        )
        self.assertTrue(payload["clipped"])
        self.assertIn("clipped", payload["note"].lower())

    async def test_an_offscreen_target_pauses_instead_of_recording_the_desktop(self) -> None:
        desktop = FakeDesktop(bounds=(5000, 5000, 5600, 5400))
        desktop.fail_window = True
        manager = self.manager(desktop)
        self.addAsyncCleanup(manager.shutdown)
        session = await manager.start(self.mission())
        session_id = session["session_id"]
        await self.wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED,
            message="an off-screen pause",
        )
        record = manager.get(session_id)
        self.assertEqual(record.pause_reason, "target_offscreen")
        self.assertEqual(record.frames, [])
        self.assertFalse(record.observations[-1].evidence)
        await manager.stop(session_id)

    async def test_a_blank_surface_pauses_as_target_unreadable(self) -> None:
        desktop = FakeDesktop(foreground=False)
        desktop.paint_window = (0, 0, 0)
        manager = self.manager(desktop)
        self.addAsyncCleanup(manager.shutdown)
        session = await manager.start(self.mission())
        session_id = session["session_id"]
        await self.wait_for(
            lambda: manager.get(session_id).state is SessionState.PAUSED,
            message="a blank-surface pause",
        )
        record = manager.get(session_id)
        self.assertEqual(record.pause_reason, "target_unreadable")
        self.assertEqual(record.frames, [])
        await manager.stop(session_id)

    async def test_without_the_window_seam_the_display_backend_is_reported(self) -> None:
        desktop = FakeDesktop(foreground=True)
        manager = self.manager(desktop, window_seam=False)
        self.addAsyncCleanup(manager.shutdown)
        rows = manager.capabilities()["capture"]["backends"]
        self.assertEqual([row["backend"] for row in rows], ["gdi_display"])
        self.assertTrue(rows[0]["requires_foreground"])
        self.assertIn("Cannot see a covered or minimized window", rows[0]["notes"])

    async def test_with_the_window_seam_both_backends_are_reported(self) -> None:
        desktop = FakeDesktop(foreground=True)
        manager = self.manager(desktop)
        self.addAsyncCleanup(manager.shutdown)
        rows = manager.capabilities()["capture"]["backends"]
        self.assertEqual([row["backend"] for row in rows], ["printwindow", "gdi_display"])
        self.assertFalse(rows[0]["requires_foreground"])
        self.assertTrue(rows[0]["unfocused"])

    async def test_stop_leaves_no_router_state_behind(self) -> None:
        desktop = FakeDesktop(foreground=False)
        manager = self.manager(desktop)
        self.addAsyncCleanup(manager.shutdown)
        for _ in range(2):
            session = await manager.start(self.mission())
            session_id = session["session_id"]
            await self.wait_for(
                lambda sid=session_id: manager.get(sid).frames, message="a frame"
            )
            await manager.stop(session_id)
            self.assertIsNone(manager.active())


class FrameRecordPersistenceTests(unittest.TestCase):
    """The backend and verdict must survive a reload, or the panel forgets them."""

    def test_round_trip_keeps_backend_content_and_validation(self) -> None:
        frame = FrameRecord(
            seq=3,
            captured_at="2026-10-23T10:00:00+00:00",
            path="C:/tmp/000003.png",
            width=800,
            height=600,
            size_bytes=1234,
            capture_ms=12.0,
            capture_mode="persistent",
            fresh=True,
            source="window_surface",
            note="",
            backend="printwindow",
            content="valid",
            validation={"livable": True, "content_ok": True, "backend": "printwindow"},
        )
        payload = frame.to_dict()
        self.assertEqual(payload["backend"], "printwindow")
        self.assertEqual(payload["validation"]["livable"], True)
        from novacontrol.novaforge.models import SessionRecord

        record = SessionRecord.from_dict(
            {
                "session_id": "abc",
                "state": "STOPPED",
                "mission": {"title": WINDOW_TITLE},
                "target": {"title": WINDOW_TITLE, "handle": 1, "pid": 2},
                "frames": [payload],
            }
        )
        self.assertIsNotNone(record)
        assert record is not None
        reloaded = record.frames[0]
        self.assertEqual(reloaded.backend, "printwindow")
        self.assertEqual(reloaded.content, "valid")
        self.assertEqual(reloaded.validation["livable"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
