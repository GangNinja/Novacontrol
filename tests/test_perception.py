"""Phase 21 — real-time perception and abstraction.

Everything here drives the REAL pipeline: real frames, real preprocessing, real
classical providers, a real tracker, and the real Phase 6 ``VisionManager`` —
with two deliberate doubles, each for a reason:

  * ``ScriptedOcr`` replaces the OCR engine so text assertions are portable.
    The shipped chain reads a PNG only through Windows OCR, and a test that
    only passes on the author's operating system is not a test of this build.
  * ``ScriptedVisionProvider`` replaces the VLM so the DEEP path can be pinned
    without a model on disk. It is asked through the same ``VisionManager`` the
    application uses, which is what makes the escalation contract real rather
    than simulated.

The images are generated with Pillow, so the suite needs no fixtures on disk and
CI's Ubuntu runner produces exactly what the author's Windows machine does.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image, ImageDraw

from novacontrol.perception import (
    CAPABILITY_TABLE,
    DEFERRED_PHASES,
    PERCEPTION_SCHEMA_VERSION,
    BBox,
    CameraFrameSource,
    CapabilityState,
    DetectedObject,
    Frame,
    FrameSampler,
    FrameUnreadable,
    GrayPreview,
    ImageFrameSource,
    NullDetectionProvider,
    NullSegmentationProvider,
    OcrText,
    OcrTextDetectionProvider,
    PerceptionCapability,
    PerceptionEngine,
    PerceptionMode,
    PerceptionProfile,
    PerceptionRequest,
    PerceptionResourceGate,
    PerceptionStatus,
    RegionDetectionProvider,
    RegionSegmentationProvider,
    RelationshipKind,
    SamplingPolicy,
    SceneRepresentation,
    ScreenFrameSource,
    SequenceFrameSource,
    SpatialTracker,
    TemporalEvent,
    TemporalEventKind,
    TemporalPerception,
    TrackState,
    abstraction_for,
    build_scene,
    capability_rows,
    derive_relationships,
    describe_frame,
    describe_question,
    events_by_kind,
    focus_for,
    max_objects_for,
    overview,
    perception_observation,
    prepare_frame,
    probe_image_file,
    sampling_for,
    scene_observation,
    validate_frame,
)
from novacontrol.perception import TestFrameSource as PerceptionTestFrameSource
from novacontrol.perception.engine import PerceptionTelemetry
from novacontrol.vision import (
    NullVisionProvider,
    VisionManager,
    VisionProviderError,
    VisionRequest,
)
from novacontrol.vision.ocr import OcrWord


def run(coro: Any) -> Any:
    """Drive one coroutine — the pipeline is async, the assertions are not."""
    return asyncio.run(coro)


# ── the doubles ────────────────────────────────────────────────────────────


class ScriptedOcr:
    """An OCR engine with declared words, in the image's own pixels.

    ``pages`` lets one engine answer differently per read (the temporal case: a
    line appears in the second frame); the last page repeats once the script runs
    out, so a single-page script behaves like a fixed reader.
    """

    name = "scripted-ocr"
    available = True

    def __init__(
        self,
        words: tuple[OcrWord, ...] = (),
        *,
        fail: bool = False,
        pages: tuple[tuple[OcrWord, ...], ...] = (),
    ) -> None:
        self.words = words
        self.pages = pages or (words,)
        self.fail = fail
        self.reads = 0

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        del source
        self.reads += 1
        if self.fail:
            raise OSError("scripted OCR failure")
        index = min(self.reads - 1, len(self.pages) - 1)
        return self.pages[index]


class ScriptedVisionProvider:
    """A working vision model: reports what it is told to, or fails on purpose."""

    def __init__(self, answer: str = "", *, name: str = "scripted", fail: str = "") -> None:
        self._answer = answer
        self._name = name
        self._fail = fail
        self.calls: list[VisionRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return "scripted-vision"

    @property
    def available(self) -> bool:
        return True

    async def see(self, *, prompt: str, image_path: str) -> str:
        self.calls.append(VisionRequest(source=image_path, question=prompt))
        if self._fail:
            raise VisionProviderError(self._fail)
        return self._answer


SCRIPTED_WORDS: tuple[OcrWord, ...] = (
    OcrWord("SAVE", line=0, x=40, y=40, width=80, height=24, kind="scripted"),
    OcrWord("CANCEL", line=0, x=160, y=40, width=120, height=24, kind="scripted"),
    OcrWord("Error: disk full", line=1, x=40, y=120, width=240, height=24, kind="scripted"),
)


class _ImageSet:
    """The seven scenarios the phase names, generated once per test class."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.blank = self._save("blank.png", self._blank())
        self.text = self._save("text.png", self._text())
        self.shapes = self._save("shapes.png", self._shapes())
        self.multi = self._save("multi.png", self._multi())
        self.complex = self._save("complex.png", self._complex())
        self.static_a = self._save("static_a.png", self._shapes())
        self.static_b = self._save("static_b.png", self._shapes())
        self.frame_a = self._save("frame_a.png", self._moving(offset=0))
        self.frame_b = self._save("frame_b.png", self._moving(offset=120))
        self.pair_a = self._save("pair_a.png", self._moving(offset=0))
        self.pair_b = self._save("pair_b.png", self._moving(offset=24))
        self.missing = str(root / "does_not_exist.png")
        self.not_an_image = self._text_file()

    def _save(self, name: str, image: Image.Image) -> str:
        path = self.root / name
        image.save(path)
        return str(path)

    @staticmethod
    def _blank() -> Image.Image:
        return Image.new("RGB", (320, 240), "white")

    @staticmethod
    def _text() -> Image.Image:
        image = Image.new("RGB", (480, 200), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle([20, 20, 440, 70], fill="black")
        draw.text((30, 80), "NovaControl perception", fill="black")
        draw.text((30, 120), "phase 21 text scenario", fill="black")
        return image

    @staticmethod
    def _shapes() -> Image.Image:
        image = Image.new("RGB", (640, 480), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle([40, 60, 260, 240], fill="black")
        draw.ellipse([300, 120, 460, 280], fill="red")
        return image

    @staticmethod
    def _multi() -> Image.Image:
        image = Image.new("RGB", (640, 480), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle([10, 10, 110, 110], fill="black")
        draw.rectangle([200, 20, 300, 120], fill="blue")
        draw.rectangle([400, 30, 500, 130], fill="green")
        draw.ellipse([20, 300, 160, 440], fill="orange")
        return image

    @staticmethod
    def _complex() -> Image.Image:
        image = Image.new("RGB", (640, 480), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        # A panel with two nested controls, a poster on the wall, and a text band.
        draw.rectangle([30, 40, 610, 400], outline="black", width=3)
        draw.rectangle([60, 80, 280, 200], fill="black")
        draw.rectangle([90, 110, 250, 170], fill="white")
        draw.ellipse([340, 90, 470, 220], fill="blue")
        draw.rectangle([120, 250, 520, 300], fill="white")
        return image

    @staticmethod
    def _moving(offset: int) -> Image.Image:
        image = Image.new("RGB", (640, 480), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle([80 + offset, 120, 220 + offset, 260], fill="black")
        draw.rectangle([40, 380, 200, 440], fill="green")
        return image

    def _text_file(self) -> str:
        path = self.root / "notes.txt"
        path.write_text("just text, not an image\n", encoding="utf-8")
        return str(path)


# ── models, frames, preprocessing ──────────────────────────────────────────


class PerceptionModelTests(unittest.TestCase):
    """The value types: arithmetic that can be checked and defaults that never lie."""

    def test_bbox_arithmetic_is_real(self) -> None:
        left = BBox(x=0, y=0, width=100, height=100)
        right = BBox(x=50, y=0, width=100, height=100)

        self.assertEqual(left.center, (50, 50))
        self.assertAlmostEqual(left.iou(right), 1 / 3, places=4)
        self.assertTrue(left.intersects(right))
        self.assertFalse(left.contains(right))
        self.assertTrue(BBox(0, 0, 200, 200).contains(left))
        self.assertFalse(BBox().has_extent)

    def test_request_from_mapping_never_guesses_a_mode(self) -> None:
        request = PerceptionRequest.from_mapping(
            {"mode": "nonsense", "allow_vlm": "false", "source_kind": "SCREEN", "max_objects": "7"}
        )

        self.assertIs(request.mode, PerceptionMode.AUTO)
        self.assertFalse(request.allow_vlm)
        self.assertEqual(request.source_kind, "screen")
        self.assertEqual(request.max_objects, 7)

    def test_unmeasured_confidence_is_none_not_zero(self) -> None:
        self.assertIsNone(DetectedObject(label="region").confidence)
        self.assertIsNone(DetectedObject(label="region").class_id)
        self.assertIsNone(OcrText(text="hello").confidence)
        # The tracker's state is a named vocabulary, never a bare string.
        self.assertEqual(TrackState.NEW.value, "new")

    def test_no_serialized_shape_carries_pixels(self) -> None:
        scene = build_scene(
            Frame(path="/tmp/x.png", width=640, height=480),
            objects=(DetectedObject(label="region", bbox=BBox(1, 2, 3, 4)),),
        )
        dumped = json.dumps(scene.to_dict())

        for forbidden in ("mask", "pixels", "base64", "frame_bytes"):
            self.assertNotIn(forbidden, dumped)
        self.assertNotIn("/tmp/x.png", dumped, "a scene must not carry the frame reference either")
        self.assertEqual(scene.to_dict()["width"], 640)


class FrameAndPreprocessingTests(unittest.TestCase):
    """Frame acquisition, probing and the preprocessing budget."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_probe_reports_real_extent(self) -> None:
        info = probe_image_file(self.images.shapes)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual((info.width, info.height), (640, 480))
        self.assertEqual(info.format, "png")
        self.assertGreater(info.size_bytes or 0, 0)

    def test_event_payload_carries_no_path(self) -> None:
        frame = describe_frame(self.images.shapes, source_id="image", sequence=0)

        payload = frame.event_payload()
        self.assertIn("frame_id", payload)
        self.assertEqual(payload["sequence"], 0)
        self.assertNotIn("path", payload)
        self.assertNotIn(self.images.shapes, json.dumps(payload))

    def test_prepare_frame_downscales_and_maps_boxes_back(self) -> None:
        frame = describe_frame(self.images.shapes, source_id="image", sequence=0)

        prepared = prepare_frame(frame)

        self.assertEqual(prepared.preview_size, (192, 144))
        self.assertGreater(prepared.scale_x, 1.0)
        self.assertAlmostEqual(prepared.scale_y, 480 / 144, places=3)
        self.assertIn("decode", prepared.steps)
        self.assertIn("downscale", prepared.steps)
        # A preview-space box comes back in the frame's own pixels.
        mapped = prepared.to_frame_bbox(BBox(50, 50, 20, 20))
        self.assertAlmostEqual(mapped.x / mapped.width, 2.5, places=1)
        self.assertAlmostEqual(mapped.center[0], 200.0, delta=2)

    def test_preview_measurements_and_diff(self) -> None:
        first = prepare_frame(describe_frame(self.images.shapes, source_id="i", sequence=0)).preview
        second = prepare_frame(describe_frame(self.images.multi, source_id="i", sequence=0)).preview
        same = prepare_frame(describe_frame(
            self.images.static_b, source_id="i", sequence=0)).preview

        self.assertIsInstance(first, GrayPreview)
        self.assertTrue(first.ok)
        self.assertIsNotNone(first.mean_luminance)
        self.assertTrue(first.signature())
        self.assertTrue(first.diff(same).comparable)
        self.assertFalse(first.diff(same).changed)
        diff = first.diff(second)
        self.assertTrue(diff.comparable)
        self.assertGreater(diff.changed_fraction, 0.0)

    def test_unreadable_and_non_image_sources_are_reported(self) -> None:
        missing = describe_frame(self.images.missing, source_id="image", sequence=0)
        with self.assertRaises(FrameUnreadable):
            prepare_frame(missing)

        text_file = describe_frame(self.images.not_an_image, source_id="image", sequence=0)
        self.assertTrue(validate_frame(text_file))
        self.assertFalse(text_file.measured_extent)

    def test_sources_declare_availability_honestly(self) -> None:
        self.assertFalse(ImageFrameSource("").available)
        self.assertIn("no image path", ImageFrameSource("").unavailable_reason)
        self.assertFalse(CameraFrameSource().available)
        self.assertIn("camera", CameraFrameSource().unavailable_reason)
        self.assertFalse(ScreenFrameSource().available)
        self.assertEqual(SequenceFrameSource([self.images.shapes, self.images.blank]).remaining, 2)

    def test_screen_source_reads_through_the_capture_callable(self) -> None:
        calls: list[int] = []

        def capture() -> str:
            calls.append(1)
            return self.images.shapes

        source = ScreenFrameSource(capture)
        frame = run(source.read())

        self.assertEqual(calls, [1])
        assert frame is not None
        self.assertEqual(frame.source_id, "screen")
        self.assertEqual((frame.width, frame.height), (640, 480))

    def test_test_frame_source_yields_supplied_frames(self) -> None:
        source = PerceptionTestFrameSource([self.images.shapes, self.images.blank])

        collected = [run(source.read()) for _ in range(3)]
        self.assertEqual([item is None for item in collected], [False, False, True])
        self.assertFalse(PerceptionTestFrameSource([], repeats=2).available)


# ── fast perception: detection, segmentation ───────────────────────────────


class DetectionProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))
        cls.prepared = prepare_frame(describe_frame(cls.images.shapes, source_id="i", sequence=0))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_classical_detector_finds_regions_without_labels(self) -> None:
        objects = run(RegionDetectionProvider().detect(self.prepared))

        self.assertGreaterEqual(len(objects), 2)
        for item in objects:
            self.assertEqual(item.label, "region")
            self.assertIsNone(item.class_id)
            self.assertIsNone(item.confidence, "a connected component is not a probability")
            self.assertTrue(item.source.startswith("region:"))
            self.assertTrue(item.located)

    def test_text_detections_come_from_the_ocr_engine(self) -> None:
        provider = OcrTextDetectionProvider(ScriptedOcr(SCRIPTED_WORDS))

        objects = run(provider.detect(self.prepared))
        labels = [item.label for item in objects]

        self.assertIn("SAVE", labels)
        self.assertIn("CANCEL", labels)
        for item in objects:
            self.assertIsNone(item.confidence)
            self.assertTrue(item.bbox.has_extent, "a placed word has real geometry")

    def test_an_ocr_failure_is_a_provider_error_not_an_empty_frame(self) -> None:
        provider = OcrTextDetectionProvider(ScriptedOcr(fail=True))

        with self.assertRaises(Exception) as caught:
            run(provider.detect(self.prepared))
        self.assertIn("OCR engine failed", str(caught.exception))

    def test_null_providers_refuse_rather_than_pretend(self) -> None:
        detection = NullDetectionProvider()
        segmentation = NullSegmentationProvider()

        self.assertFalse(detection.available)
        self.assertTrue(detection.unavailable_reason)
        self.assertEqual(run(detection.detect(self.prepared)), ())
        self.assertFalse(segmentation.available)
        self.assertEqual(run(segmentation.segment(self.prepared)), ())

    def test_segmentation_masks_are_pixels_not_boxes(self) -> None:
        provider = RegionSegmentationProvider()

        masks = run(provider.segment(self.prepared))

        self.assertTrue(masks)
        ellipse = max(masks, key=lambda item: item.pixel_count)
        pixels = provider.mask_pixels(ellipse.mask_id)
        self.assertIsNotNone(pixels)
        assert pixels is not None
        inside = sum(row.count(255) for row in pixels)
        self.assertEqual(len(pixels), int(ellipse.metadata.get("mask_height", len(pixels))))
        self.assertEqual(inside, ellipse.pixel_count)
        # A mask that were really a bounding box would cover width × height.
        self.assertLess(ellipse.pixel_count, ellipse.bbox.width * ellipse.bbox.height)
        # What serializes is the REFERENCE; the rows live behind ``mask_pixels``.
        dumped = ellipse.to_dict()
        self.assertEqual(dumped["mask_id"], ellipse.mask_id)
        self.assertEqual(dumped["pixel_count"], ellipse.pixel_count)
        self.assertNotIn("rows", dumped)
        self.assertIsNone(provider.mask_pixels("no-such-mask"))


# ── tracking, relationships, temporal perception ───────────────────────────


class TrackingTests(unittest.TestCase):
    """Spatial tracking: continuity, occlusion, loss — and honest naming."""

    def _object(self, x: int, label: str = "region") -> DetectedObject:
        return DetectedObject(label=label, bbox=BBox(x, 100, 140, 140))

    def test_life_cycle_from_new_to_removed(self) -> None:
        tracker = SpatialTracker()
        first = tracker.update((self._object(100),), frame_id="f1", width=640, height=480)
        second = tracker.update((self._object(150),), frame_id="f2", width=640, height=480)
        third = tracker.update((), frame_id="f3", width=640, height=480)
        fourth = tracker.update((), frame_id="f4", width=640, height=480)
        fifth = tracker.update((), frame_id="f5", width=640, height=480)

        track_id = first.tracks[0].track_id
        self.assertIs(first.tracks[0].state, TrackState.NEW)
        self.assertIs(second.tracks[0].state, TrackState.VISIBLE)
        self.assertEqual(second.tracks[0].frame_count, 2)
        self.assertIs(third.tracks[0].state, TrackState.OCCLUDED)
        self.assertIs(fourth.tracks[0].state, TrackState.LOST)
        self.assertIn("not seen for 2 frames", fourth.events[0].detail)
        self.assertIs(fifth.tracks[0].state, TrackState.REMOVED)
        self.assertEqual([item.track_id for item in fifth.tracks], [track_id])
        self.assertEqual(tracker.live_tracks, 0)

    def test_movement_is_measured_and_reported_in_pixels(self) -> None:
        tracker = SpatialTracker()
        tracker.update(
            (self._object(100),),
            frame_id="f1",
            timestamp="2026-10-08T00:00:00+00:00",
            width=640,
            height=480,
        )

        update = tracker.update(
            (self._object(150),),
            frame_id="f2",
            timestamp="2026-10-08T00:00:01+00:00",
            width=640,
            height=480,
        )

        counts = events_by_kind(update.events)
        self.assertIn(TemporalEventKind.OBJECT_MOVED.value, counts)
        moved = [item for item in update.events if item.kind is TemporalEventKind.OBJECT_MOVED]
        self.assertIn("50px right", moved[0].detail)
        self.assertIn("0px down", moved[0].detail)
        self.assertIsNotNone(update.tracks[0].velocity)

    def test_relabelling_keeps_identity_and_remembers_the_old_label(self) -> None:
        tracker = SpatialTracker()
        tracker.update(
            (DetectedObject(label="region", bbox=BBox(10, 10, 50, 50)),),
            frame_id="f1",
            width=640,
            height=480,
        )

        update = tracker.update(
            (DetectedObject(label="person", bbox=BBox(12, 10, 50, 50)),),
            frame_id="f2",
            width=640,
            height=480,
        )

        track = update.tracks[0]
        self.assertEqual(track.label, "person")
        self.assertEqual(track.previous_label, "region")
        self.assertTrue(track.spatial_only, "this tracker never claims to recognize anything")


class SpatialRelationshipTests(unittest.TestCase):
    def _desk_scene(self) -> tuple[DetectedObject, ...]:
        return (
            DetectedObject(label="desk", bbox=BBox(0, 0, 400, 300), confidence=0.7),
            DetectedObject(label="laptop", bbox=BBox(50, 50, 200, 150), confidence=0.8),
            DetectedObject(label="mouse", bbox=BBox(300, 200, 60, 40)),
            DetectedObject(label="poster", bbox=BBox(420, 20, 100, 80)),
        )

    def test_containment_and_ordering_carry_their_arithmetic(self) -> None:
        report = derive_relationships(self._desk_scene())
        kinds = {item.kind for item in report.relationships}

        self.assertIn(RelationshipKind.INSIDE, kinds)
        self.assertIn(RelationshipKind.CONTAINS, kinds)
        self.assertIn(RelationshipKind.LEFT_OF, kinds)
        self.assertIn(RelationshipKind.RIGHT_OF, kinds)
        for item in report.relationships:
            self.assertTrue(item.provenance.startswith("geometry"))
            self.assertTrue(item.evidence, "every relation states the numbers behind it")

    def test_shared_confidence_is_the_weaker_one_and_none_when_unknown(self) -> None:
        report = derive_relationships(self._desk_scene())
        contained = [item for item in report.relationships if item.kind is RelationshipKind.INSIDE]

        laptop = next(item for item in contained if item.confidence is not None)
        self.assertAlmostEqual(laptop.confidence or 0.0, 0.7, places=3)
        self.assertTrue(
            any(item.confidence is None for item in report.relationships),
            "an unmeasured box yields an unmeasured relation",
        )

    def test_the_pair_budget_is_reported_not_hidden(self) -> None:
        many = tuple(DetectedObject(label=f"item{i}", bbox=BBox(i, i, 10, 10)) for i in range(30))

        report = derive_relationships(many, max_pairs=5)

        self.assertTrue(report.capped)
        self.assertEqual(report.pairs_considered, 5)
        self.assertGreater(report.pairs_skipped, 0)


class TemporalPerceptionTests(unittest.TestCase):
    """Change between two observed scenes — what was seen, never what it means."""

    def _scene(self, *, lines: tuple[str,
        ...], confidence: float, objects: tuple[DetectedObject, ...]) -> SceneRepresentation:
        del confidence
        return build_scene(
            Frame(path="/tmp/t.png", width=100, height=100),
            objects=objects,
            text=tuple(OcrText(text=line, reading_order=index) for index, line in enumerate(lines)),
        )

    def test_first_observation_establishes_a_baseline_without_events(self) -> None:
        perception = TemporalPerception()
        scene = self._scene(lines=("hello",), confidence=0.5, objects=())

        events = perception.observe(scene)

        self.assertEqual(events, ())
        self.assertTrue(perception.has_baseline)
        self.assertEqual(perception.observations, 1)

    def test_re_wrapping_text_is_static_and_case_folding_is_respected(self) -> None:
        perception = TemporalPerception()
        perception.observe(self._scene(lines=("HELLO",), confidence=0.5, objects=()))
        first = perception.observe(self._scene(lines=("hello",), confidence=0.5, objects=()))
        second = perception.observe(self._scene(
            lines=("hello world", "second"), confidence=0.5, objects=()))

        self.assertEqual([item.kind for item in first], [TemporalEventKind.SCENE_STATIC])
        kinds = [item.kind for item in second]
        self.assertIn(TemporalEventKind.TEXT_CHANGED, kinds)

    def test_removed_text_is_reported_as_removal(self) -> None:
        perception = TemporalPerception()
        perception.observe(self._scene(lines=("line one", "line two"), confidence=0.5, objects=()))

        events = perception.observe(self._scene(lines=(), confidence=0.5, objects=()))

        kinds = [item.kind for item in events]
        self.assertIn(TemporalEventKind.TEXT_REMOVED, kinds)
        removed = next(item for item in events if item.kind is TemporalEventKind.TEXT_REMOVED)
        self.assertTrue(removed.detail)

    def test_tracker_events_travel_through_the_temporal_layer_unchanged(self) -> None:
        perception = TemporalPerception()
        perception.observe(self._scene(lines=("a",), confidence=0.5, objects=()))
        injected = TemporalEvent(kind=TemporalEventKind.OBJECT_MOVED,
            object_id="t1", label="region")

        events = perception.observe(self._scene(
            lines=("a",), confidence=0.5, objects=()), object_events=(injected,))

        self.assertIn(injected, events)


# ── scene, abstraction, focus ──────────────────────────────────────────────


class SceneAndAbstractionTests(unittest.TestCase):
    def _scene(self) -> SceneRepresentation:
        objects = (
            DetectedObject(label="person", bbox=BBox(10, 10, 50, 100), confidence=0.8),
            DetectedObject(label="region", bbox=BBox(200, 10, 50, 50)),
        )
        text = (OcrText(text="Hello perception", reading_order=0, bbox=BBox(10, 200, 100, 20)),)
        relationships = derive_relationships(objects).relationships
        return build_scene(
            Frame(path="/tmp/s.png", width=640, height=480),
            objects=objects,
            text=text,
            relationships=relationships,
            providers={"detection": "ocr:text-file+region:classical"},
            metadata={"escalated": False},
        )

    def test_scene_carries_the_parts_it_was_given(self) -> None:
        scene = self._scene()

        self.assertEqual(len(scene.objects), 2)
        self.assertEqual(scene.text_block, "Hello perception")
        self.assertIsNotNone(scene.abstraction)
        self.assertIsNotNone(scene.confidence)
        self.assertAlmostEqual((scene.confidence or 0) + (scene.uncertainty or 0), 1.0, places=3)

    def test_abstraction_counts_quotes_and_names_its_basis(self) -> None:
        abstraction = abstraction_for(self._scene())

        self.assertIn("2 objects detected", abstraction.summary)
        self.assertIn("person", abstraction.summary)
        self.assertEqual(abstraction.basis, "deterministic")
        self.assertTrue(abstraction.provenance)
        self.assertIn("Hello perception", abstraction.text_excerpt)
        self.assertTrue(any("person" in item for item in abstraction.highlights) or
            abstraction.highlights)

    def test_focus_matches_the_question_or_says_it_matched_nothing(self) -> None:
        scene = self._scene()

        matched = focus_for("where is the person", scene)
        unmatched = focus_for("where is the zzz", scene)

        self.assertNotEqual(matched.get("match"), "none")
        self.assertTrue(matched.get("matched_objects"))
        self.assertEqual(unmatched["match"], "none")
        self.assertEqual(unmatched["matched_objects"], [])
        self.assertEqual(unmatched["matched_lines"], [])

    def test_escalated_objects_change_the_declared_basis(self) -> None:
        scene = build_scene(
            Frame(path="/tmp/s.png"),
            objects=(
                DetectedObject(label="person", bbox=BBox(1, 1, 10, 10), source="vlm:scripted"),
            ),
        )

        abstraction = abstraction_for(scene)
        self.assertEqual(abstraction.basis, "deterministic+vlm")


# ── routing, sampling, governance ──────────────────────────────────────────


class RoutingTests(unittest.TestCase):
    def test_question_kinds_produce_fast_plans(self) -> None:
        text = PerceptionRouterPlan.for_question("read the text in this image")
        objects = PerceptionRouterPlan.for_question("what objects are visible")
        describe = PerceptionRouterPlan.for_question("describe what is happening in this scene")

        self.assertIn(PerceptionCapability.OCR, text.fast)
        self.assertFalse(text.deep)
        self.assertIn(PerceptionCapability.DETECTION, objects.fast)
        self.assertFalse(objects.deep)
        self.assertTrue(describe.deep, "a description is a question only a model can answer")
        self.assertTrue(text.reasons and objects.reasons and describe.reasons)

    def test_modes_force_and_forbid_the_deep_path(self) -> None:
        fast_only = PerceptionRouterPlan.plan(
            PerceptionRequest(question="describe it", mode=PerceptionMode.FAST))
        deep = PerceptionRouterPlan.plan(
            PerceptionRequest(question="read the text", mode=PerceptionMode.DEEP, allow_vlm=True)
        )
        refused = PerceptionRouterPlan.plan(
            PerceptionRequest(question="describe it", mode=PerceptionMode.DEEP, allow_vlm=False)
        )

        self.assertFalse(fast_only.deep)
        self.assertTrue(deep.deep)
        self.assertFalse(refused.deep)
        self.assertIn("forbids a vision model", " ".join(refused.reasons))
        self.assertIn("fast path only", " ".join(fast_only.reasons))

    def test_describe_question_names_the_kind(self) -> None:
        self.assertEqual(describe_question("read the text on screen"), "text")
        self.assertEqual(describe_question("where is the save button", target="save"), "locate")


class PerceptionRouterPlan:
    """Thin helper: read the real router's plan without repeating the wiring."""

    @staticmethod
    def for_question(question: str) -> Any:
        from novacontrol.perception import PerceptionRouter

        return PerceptionRouter().plan(PerceptionRequest(question=question))

    @staticmethod
    def plan(request: PerceptionRequest) -> Any:
        from novacontrol.perception import PerceptionRouter

        return PerceptionRouter().plan(request)


class SamplingTests(unittest.TestCase):
    def _preview(self, fill: int) -> GrayPreview:
        return GrayPreview(width=8, height=8, rows=tuple(bytes([fill]) * 8 for _ in range(8)))

    def test_the_first_frame_is_always_processed(self) -> None:
        sampler = FrameSampler(SamplingPolicy(target_fps=2.0, max_fps=5.0, min_interval_ms=100))

        decision = sampler.decide(self._preview(255), now_ms=0.0)

        self.assertTrue(decision.process)
        self.assertEqual(decision.mode, "initial")

    def test_a_static_stream_is_skipped_and_accounted_for(self) -> None:
        sampler = FrameSampler(SamplingPolicy(target_fps=2.0, max_fps=5.0, min_interval_ms=100))
        sampler.decide(self._preview(255), now_ms=0.0)
        sampler.decide(self._preview(10), now_ms=10.0)

        decision = sampler.decide(self._preview(10), now_ms=20.0)

        self.assertFalse(decision.process)
        self.assertGreaterEqual(sampler.frames_skipped, 1)
        self.assertIn(decision.mode, {"skipped", "dropped"})

    def test_change_is_processed_and_counted(self) -> None:
        sampler = FrameSampler(SamplingPolicy(target_fps=2.0, max_fps=5.0, min_interval_ms=100))
        sampler.decide(self._preview(255), now_ms=0.0)

        decision = sampler.decide(self._preview(0), now_ms=500.0)

        self.assertTrue(decision.process)
        self.assertEqual(decision.mode, "changed")
        self.assertEqual(sampler.changes, 1)

    def test_profiles_scale_the_budget_and_unknown_names_fall_back(self) -> None:
        low = sampling_for(PerceptionProfile.LOW_RESOURCE)
        performance = sampling_for(PerceptionProfile.PERFORMANCE)

        self.assertGreater(performance.target_fps, low.target_fps)
        self.assertGreater(max_objects_for(
            PerceptionProfile.PERFORMANCE), max_objects_for("nonsense"))
        self.assertEqual(PerceptionProfile.resolve("nonsense"), PerceptionProfile.BALANCED)


@dataclass(frozen=True, slots=True)
class _Advice:
    """A governor's advice in the shape ``ResourceGovernor.advise_load`` returns."""

    allow: bool | None = None
    reason: str = ""
    unload: tuple[str, ...] = ()
    prefer_lightweight: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "reason": self.reason, "unload": list(self.unload)}


class _AllowGovernor:
    """A governor that says yes — the application wires the real one.

    The deep-path tests need one, because the gate refuses a model load when no
    governor is wired at all: "nothing to ask" must never mean "yes".
    """

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    def advise_load(self, needed_bytes: Any, *, model: str, loaded: Any, active: Any) -> _Advice:
        self.asked.append({"model": model, "needed_bytes": needed_bytes, "loaded": loaded})
        return _Advice(allow=True, reason="plenty of memory")


class ResourceGateTests(unittest.TestCase):
    def test_the_fast_path_needs_no_gate_at_all(self) -> None:
        decision = PerceptionResourceGate().admit(needs_model=False)

        self.assertTrue(decision.allowed)
        self.assertIn("fast path", decision.reason)

    def test_absence_of_a_governor_is_not_permission(self) -> None:
        decision = PerceptionResourceGate().admit(needs_model=True, model="llava")

        self.assertTrue(decision.denied)
        self.assertIn("no resource governor", decision.reason)
        self.assertEqual(decision.degrade_to, PerceptionProfile.LOW_RESOURCE)

    def test_a_wired_silent_governor_is_refused_after_a_fresh_probe(self) -> None:
        class SilentGovernor:
            def advise_load(self, needed_bytes: Any,
                *, model: str, loaded: Any, active: Any) -> Any:
                del needed_bytes, model, loaded, active
                return _Advice(allow=None, reason="the memory probe failed")

        decision = PerceptionResourceGate(governor=SilentGovernor()).admit(needs_model=True)

        self.assertTrue(decision.denied)
        self.assertIn("could not measure", decision.reason)

    def test_a_resident_model_is_allowed_without_a_governor(self) -> None:
        class Manager:
            def active_models(self) -> tuple[str, ...]:
                return ("scripted-vision",)

            def runtime_status(self) -> dict[str, Any]:
                return {}

            def model_size_bytes(self, name: str) -> int:
                return 0

        decision = PerceptionResourceGate(model_manager=Manager()).admit(
            needs_model=True, model="scripted-vision"
        )

        self.assertTrue(decision.allowed)
        self.assertIn("already resident", decision.reason)

    def test_profile_change_rebuilds_the_gate_with_the_same_collaborators(self) -> None:
        governor = object()
        gate = PerceptionResourceGate(governor=governor, profile=PerceptionProfile.BALANCED)

        degraded = gate.with_profile(PerceptionProfile.LOW_RESOURCE)

        self.assertIs(degraded.governor, governor)
        self.assertEqual(degraded.profile, PerceptionProfile.LOW_RESOURCE)
        self.assertEqual(gate.profile, PerceptionProfile.BALANCED)

    def test_status_never_asks_the_model_runtime(self) -> None:
        """gate.status() travels in every app.status() poll — it must not probe.

        The model manager's runtime_status() is one HTTP round trip (a bounded
        2 s timeout when the local runtime is down), and asking it here put a
        two-second stall on every /system/telemetry poll.
        """

        class ProbingManager:
            def __init__(self) -> None:
                self.probes = 0

            def runtime_status(self) -> Any:
                self.probes += 1
                raise AssertionError("status must never ask the runtime")

            def active_models(self) -> tuple[str, ...]:
                return ()

        manager = ProbingManager()
        row = PerceptionResourceGate(model_manager=manager).status()

        self.assertEqual(manager.probes, 0)
        # With no governor, nothing measured residency: unknown, not "none".
        self.assertIsNone(row["resident_models"])

    def test_status_reads_residency_from_the_governor_assessment(self) -> None:
        class Governor:
            def report(self) -> dict[str, Any]:
                return {"assessment": {"loaded_models": ["vision-model"]}}

        class Manager:
            def runtime_status(self) -> Any:
                raise AssertionError("status must never ask the runtime")

        row = PerceptionResourceGate(governor=Governor(), model_manager=Manager()).status()

        self.assertEqual(row["resident_models"], ["vision-model"])

    def test_status_reports_the_monitors_real_memory_figures(self) -> None:
        class Monitor:
            def available_ram_bytes(self) -> int | None:
                return 4_437_975_040

            def total_ram_bytes(self) -> int | None:
                return 16_503_619_584

        class BlindMonitor:
            def available_ram_bytes(self) -> int | None:
                return None

            def total_ram_bytes(self) -> int | None:
                return None

        class BrokenMonitor:
            def available_ram_bytes(self) -> int | None:
                raise OSError("no sensor")

            def total_ram_bytes(self) -> int | None:
                raise OSError("no sensor")

        row = PerceptionResourceGate(monitor=Monitor()).status()
        self.assertTrue(row["memory"]["available"])
        self.assertEqual(row["available_ram_bytes"], 4_437_975_040)
        self.assertEqual(row["memory"]["total_bytes"], 16_503_619_584)

        blind = PerceptionResourceGate(monitor=BlindMonitor()).status()
        self.assertFalse(blind["memory"]["available"])
        self.assertIn("no figure", blind["memory"]["reason"])
        self.assertIsNone(blind["available_ram_bytes"])

        broken = PerceptionResourceGate(monitor=BrokenMonitor()).status()
        self.assertFalse(broken["memory"]["available"])
        self.assertIn("failed", broken["memory"]["reason"])

    def test_admit_reads_residency_from_the_loaded_models_field(self) -> None:
        """ModelRuntimeStatus calls it ``loaded_models``; ``resident`` is a
        misspelling that silently yielded an empty list forever, so the
        governor was told nothing was ever loaded."""

        class Runtime:
            provider = "ollama"
            active_model = ""
            loaded_models = ("vision-model",)

        class Manager:
            def runtime_status(self) -> Runtime:
                return Runtime()

            def active_models(self) -> tuple[str, ...]:
                return ()

        governor = _AllowGovernor()
        decision = PerceptionResourceGate(
            governor=governor, model_manager=Manager()
        ).admit(needs_model=True, model="llava", required_bytes=1_000)

        self.assertTrue(decision.allowed)
        self.assertEqual(governor.asked[0]["loaded"], ("vision-model",))


# ── the engine: fast path, deep path, honesty ──────────────────────────────


class EngineFastPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _engine(self, **kwargs: Any) -> PerceptionEngine:
        ocr = kwargs.pop("ocr", ScriptedOcr(SCRIPTED_WORDS))
        return PerceptionEngine(vision=VisionManager(
            provider=NullVisionProvider(), ocr=ocr), ocr=ocr, **kwargs)

    def test_blank_frame_is_success_with_nothing_in_it(self) -> None:
        engine = self._engine(ocr=ScriptedOcr(()))

        result = run(engine.perceive({"source": self.images.blank, "question": "read the text"}))

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertEqual(result.objects, ())
        self.assertEqual(result.text, ())
        self.assertIn("Nothing was detected", result.summary)

    def test_shapes_produce_regions_in_frame_space(self) -> None:
        result = run(self._engine().perceive(
            {"source": self.images.shapes, "question": "what objects are visible"}))

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertGreaterEqual(len(result.objects), 2)
        self.assertTrue(result.plan and PerceptionCapability.DETECTION in result.plan.fast)
        self.assertFalse(result.escalated)
        for item in result.objects:
            self.assertGreater(item.bbox.width, 0)
            self.assertLessEqual(item.bbox.x + item.bbox.width, 640 + 4)

    def test_text_question_is_answered_by_the_reader(self) -> None:
        result = run(self._engine().perceive(
            {"source": self.images.shapes, "question": "read the text"}))

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertIn("SAVE", result.text_block)
        self.assertIn("Error: disk full", result.text_block)
        row = result.capability(PerceptionCapability.OCR)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(row.available)

    def test_complex_frame_is_counted_without_invention(self) -> None:
        result = run(self._engine().perceive(
            {"source": self.images.complex, "question": "what is on the screen"}))

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertGreaterEqual(len(result.objects), 3)
        abstraction = result.abstraction
        self.assertIsNotNone(abstraction)
        assert abstraction is not None
        self.assertTrue(abstraction.summary)
        self.assertEqual(abstraction.basis, "deterministic")

    def test_segmentation_runs_only_when_it_is_allowed(self) -> None:
        # Segmentation is planned for an OBJECTS question and only when allowed.
        question = "what objects are visible"
        denied = run(
            self._engine().perceive(
                {"source": self.images.shapes, "question": question, "allow_segmentation": False}
            )
        )
        allowed = run(
            self._engine().perceive(
                {"source": self.images.shapes, "question": question, "allow_segmentation": True}
            )
        )

        self.assertIsNone(denied.capability(PerceptionCapability.SEGMENTATION))
        row = allowed.capability(PerceptionCapability.SEGMENTATION)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(row.available)

    def test_camera_is_unavailable_with_a_reason(self) -> None:
        result = run(self._engine().perceive({"source_kind": "camera"}))

        self.assertIs(result.status, PerceptionStatus.UNAVAILABLE)
        self.assertIn("camera", result.status_reason)

    def test_missing_image_fails_with_the_reason(self) -> None:
        result = run(self._engine().perceive({"source": self.images.missing}))

        self.assertIs(result.status, PerceptionStatus.FAILED)
        self.assertTrue(result.status_reason)
        self.assertEqual(result.objects, ())

    def test_a_two_frame_stream_reports_temporal_change(self) -> None:
        # Frame two: the box moved a little (so the tracker keeps its identity)
        # and a NEW line of text is on screen — the two things a temporal layer
        # exists to notice.
        engine = self._engine(
            ocr=ScriptedOcr(
                pages=(
                    (OcrWord("SAVE", line=0, x=40, y=40, width=80, height=24, kind="scripted"),),
                    (
                        OcrWord("SAVE", line=0, x=40, y=40, width=80, height=24, kind="scripted"),
                        OcrWord("Saving…",
                            line=1, x=40, y=120, width=90, height=24, kind="scripted"),
                    ),
                )
            )
        )

        result = run(
            engine.perceive(
                {"source_kind": "sequence", "temporal_context": True},
                frames=(
                    describe_frame(self.images.pair_a, source_id="sequence", sequence=0),
                    describe_frame(self.images.pair_b, source_id="sequence", sequence=1),
                ),
            )
        )

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        kinds = {item.kind for item in result.temporal_events}
        self.assertTrue(kinds, "a walk over two frames must report what changed")
        self.assertIn(TemporalEventKind.TEXT_CHANGED, kinds)
        self.assertEqual(engine.telemetry.frames_acquired, 2)
        self.assertEqual(result.scene.sequence if result.scene else None, 1)

    def test_single_frame_keyword_runs_the_same_walk(self) -> None:
        frame = describe_frame(self.images.shapes, source_id="supplied", sequence=3)

        result = run(self._engine().perceive({"question": "what objects are visible"}, frame=frame))

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertEqual(result.scene.sequence if result.scene else None, 3)

    def test_object_ceiling_is_enforced_and_declared(self) -> None:
        engine = self._engine(max_objects=2)

        result = run(engine.perceive({"source": self.images.multi,
            "question": "what objects are visible"}))

        self.assertLessEqual(len(result.objects), 2)
        self.assertGreaterEqual(engine.max_objects, 1)

    def test_status_reports_wiring_and_no_content(self) -> None:
        engine = self._engine()
        run(engine.perceive({"source": self.images.shapes}))

        status = engine.status()

        for key in (
            "profile",
            "providers",
            "capabilities",
            "sampling",
            "tracking",
            "temporal",
            "resources",
            "telemetry",
            "last_scene",
        ):
            self.assertIn(key, status)
        self.assertFalse(status["raw_frames_stored"])
        self.assertFalse(status["action_execution"])
        self.assertFalse(status["cuda_required"])
        self.assertFalse(status["automatic_model_loading"])
        self.assertNotIn(self.images.shapes, json.dumps(status))
        self.assertIn("scene_id", status["last_scene"])

    def test_latency_is_measured_per_stage(self) -> None:
        result = run(
            self._engine().perceive(
                {"source": self.images.shapes, "question": "what objects are visible"}
            )
        )

        self.assertIn("preprocess", result.latency)
        self.assertIn("ocr", result.latency)
        self.assertIn("detection", result.latency)
        self.assertIn("relationships", result.latency)
        self.assertIn("tracking", result.latency)
        self.assertIn("temporal", result.latency)
        self.assertGreater(result.latency["total"], 0)
        self.assertNotIn("segmentation", result.latency, "a stage that did not run has no key")
        row = result.capability(PerceptionCapability.DETECTION)
        assert row is not None
        self.assertIsNotNone(row.latency_ms)

    def test_sampling_actually_skips_the_work_it_declines(self) -> None:
        engine = self._engine()

        result = run(
            engine.perceive(
                {"source_kind": "sequence", "temporal_context": True},
                source=PerceptionTestFrameSource(
                    [self.images.static_a, self.images.static_b], repeats=4
                ),
            )
        )

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertEqual(engine.telemetry.frames_acquired, 8)
        self.assertLess(engine.telemetry.frames_analysed, 8, "a static stream is not re-analysed")
        self.assertGreater(engine.telemetry.frames_skipped, 0)
        self.assertGreater(engine.sampler.frames_skipped, 0)

    def test_a_fresh_request_does_not_inherit_the_previous_tracks(self) -> None:
        engine = self._engine(ocr=ScriptedOcr(()))
        run(engine.perceive({"source": self.images.shapes, "question": "what objects are visible"}))
        self.assertGreaterEqual(len(engine.tracker.snapshot()), 1)

        fresh = run(
            engine.perceive({"source": self.images.blank, "question": "what objects are visible"})
        )

        kinds = {item.kind for item in fresh.temporal_events}
        self.assertNotIn(TemporalEventKind.OBJECT_DISAPPEARED, kinds)
        self.assertEqual(fresh.objects, ())

    def test_temporal_context_is_what_makes_change_reports_possible(self) -> None:
        pages = (
            (OcrWord("SAVE", line=0, x=10, y=10, width=60, height=20, kind="scripted"),),
            (
                OcrWord("SAVE", line=0, x=10, y=10, width=60, height=20, kind="scripted"),
                OcrWord("CANCEL", line=1, x=10, y=60, width=70, height=20, kind="scripted"),
            ),
        )
        # Two DIFFERENT pictures, so the sampler has a real change to see.
        isolated = self._engine(ocr=ScriptedOcr(pages=pages))
        run(isolated.perceive({"source": self.images.shapes, "question": "read the text"}))
        unconnected = run(
            isolated.perceive({"source": self.images.multi, "question": "read the text"})
        )
        self.assertNotIn(
            TemporalEventKind.TEXT_CHANGED, {item.kind for item in unconnected.temporal_events}
        )

        connected = self._engine(ocr=ScriptedOcr(pages=pages))
        run(
            connected.perceive(
                {
                    "source": self.images.shapes,
                    "question": "read the text",
                    "temporal_context": True,
                }
            )
        )
        follow = run(
            connected.perceive(
                {
                    "source": self.images.multi,
                    "question": "read the text",
                    "temporal_context": True,
                }
            )
        )
        self.assertIn(
            TemporalEventKind.TEXT_CHANGED, {item.kind for item in follow.temporal_events}
        )

    def test_telemetry_counts_requests_and_frames(self) -> None:
        engine = self._engine()
        run(engine.perceive({"source": self.images.shapes}))
        run(engine.perceive({"source": self.images.blank}))

        self.assertEqual(engine.telemetry.requests, 2)
        # A still image is ONE frame: the stream ceiling must not multiply it.
        self.assertEqual(engine.telemetry.frames_acquired, 2)
        self.assertIsInstance(engine.telemetry, PerceptionTelemetry)
        self.assertGreater(engine.telemetry.to_dict()["requests"], 0)

    def test_capability_table_reports_live_availability(self) -> None:
        rows = {row.name: row for row in capability_rows(self._engine())}

        self.assertEqual(set(rows), {item.value for item in PerceptionCapability})
        self.assertTrue(rows[PerceptionCapability.OCR.value].available)
        self.assertTrue(rows[PerceptionCapability.DETECTION.value].available)
        self.assertFalse(rows[PerceptionCapability.VLM.value].available)
        self.assertEqual(
            rows[PerceptionCapability.VLM.value].reason, "no vision model is wired"
        )
        self.assertIs(rows[PerceptionCapability.VLM.value].state,
            CapabilityState.PROVIDER_DEPENDENT)

    def test_observer_receives_events_and_never_a_path(self) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        def observer(event_type: str, payload: dict[str, Any]) -> None:
            seen.append((event_type, payload))

        engine = self._engine(observer=observer)
        run(engine.perceive({"source": self.images.shapes, "question": "what objects are visible"}))

        kinds = [item[0] for item in seen]
        self.assertIn("perception.started", kinds)
        self.assertIn("perception.frame", kinds)
        self.assertIn("perception.completed", kinds)
        self.assertNotIn(self.images.shapes, json.dumps(seen))
        completed = next(payload for kind, payload in seen if kind == "perception.completed")
        self.assertIn("status", completed)
        self.assertIn("objects", completed)

    def test_a_broken_observer_never_breaks_perception(self) -> None:
        def observer(event_type: str, payload: dict[str, Any]) -> None:
            del event_type, payload
            raise RuntimeError("observer exploded")

        engine = self._engine(observer=observer)

        result = run(engine.perceive({"source": self.images.shapes}))

        self.assertIs(result.status, PerceptionStatus.SUCCESS)
        self.assertGreater(engine.telemetry.observer_failures, 0)

    def test_a_broken_provider_is_reported_not_swallowed(self) -> None:
        engine = self._engine(ocr=ScriptedOcr(fail=True))

        result = run(engine.perceive({"source": self.images.shapes, "question": "read the text"}))

        row = result.capability(PerceptionCapability.OCR)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertFalse(row.available)
        self.assertIn("OCR", row.reason.upper())

    def test_profile_changes_the_budget_on_the_same_engine(self) -> None:
        engine = self._engine()

        engine.set_profile(PerceptionProfile.LOW_RESOURCE)

        self.assertEqual(engine.profile, PerceptionProfile.LOW_RESOURCE)
        self.assertEqual(engine.max_objects, max_objects_for(PerceptionProfile.LOW_RESOURCE))
        self.assertEqual(engine.sampler.policy.target_fps,
            sampling_for(PerceptionProfile.LOW_RESOURCE).target_fps)


class EngineEscalationTests(unittest.TestCase):
    """The deep path: asked through Phase 6, gated, and refused honestly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _engine(self, provider: Any,
        gate: PerceptionResourceGate | None = None) -> PerceptionEngine:
        manager = VisionManager(provider=provider, ocr=ScriptedOcr(SCRIPTED_WORDS))
        return PerceptionEngine(
            vision=manager,
            ocr=manager.ocr,
            # The deep path needs a governor: the default gate refuses a model
            # load with no governor wired, which is its own test below.
            gate=gate or PerceptionResourceGate(governor=_AllowGovernor()),
        )

    def test_a_working_model_answers_the_description_question(self) -> None:
        """The deep path runs through the SAME manager the application uses."""
        answer = json.dumps(
            {
                "summary": "A person is sitting at a desk.",
                "ui_elements": [
                    {
                        "label": "person",
                        "kind": "person",
                        "bounds": {"x": 60, "y": 60, "width": 160, "height": 200},
                        "confidence": 0.82,
                    }
                ],
                "detected_text": ["Meeting at 3pm"],
                "confidence": 0.8,
            }
        )
        provider = ScriptedVisionProvider(answer)
        gate = PerceptionResourceGate(governor=_AllowGovernor())

        result = run(
            self._engine(provider, gate).perceive(
                {"source": self.images.shapes,
                    "question": "describe what is happening in this scene"}
            )
        )

        self.assertEqual(len(provider.calls), 1, "the model is asked exactly once")
        self.assertEqual(provider.calls[0].source, self.images.shapes)
        self.assertTrue(result.escalated)
        labels = [item.label for item in result.objects]
        self.assertIn("person", labels)
        semantic = next(item for item in result.objects if item.label == "person")
        self.assertTrue(semantic.source.startswith("vlm:"))
        self.assertAlmostEqual(semantic.confidence or 0.0, 0.82, places=2)
        self.assertIn("Meeting at 3pm", result.text_block)
        self.assertEqual(result.abstraction.basis if result.abstraction else "",
            "deterministic+vlm")
        self.assertIs(result.status, PerceptionStatus.SUCCESS)

    def test_the_same_request_is_refused_when_no_governor_is_wired(self) -> None:
        provider = ScriptedVisionProvider('{"summary": "a desk"}')
        engine = self._engine(provider, gate=PerceptionResourceGate())

        result = run(
            engine.perceive(
                {"source": self.images.shapes,
                    "question": "describe what is happening in this scene"}
            )
        )

        self.assertEqual(provider.calls, [], "nothing is loaded without a gate")

        row = result.capability(PerceptionCapability.VLM)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertFalse(row.available)
        self.assertIn("no resource governor", row.reason)
        self.assertFalse(result.escalated)
        self.assertIn(result.status, (PerceptionStatus.PARTIAL, PerceptionStatus.UNAVAILABLE))

    def test_a_failing_model_is_a_capability_failure_with_a_reason(self) -> None:
        provider = ScriptedVisionProvider(fail="the model is not loaded")
        engine = self._engine(provider)

        result = run(
            engine.perceive({"source": self.images.shapes,
                "question": "describe what is happening in this scene"})
        )

        row = result.capability(PerceptionCapability.VLM)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("not loaded", row.reason)
        self.assertGreater(engine.telemetry.escalations, 0, "the model WAS consulted")
        self.assertFalse(result.escalated, "a failure is never reported as an escalation")
        self.assertIs(result.status, PerceptionStatus.PARTIAL)

    def test_allow_vlm_false_never_asks_a_model(self) -> None:
        provider = ScriptedVisionProvider('{"summary": "never asked"}')

        result = run(
            self._engine(provider).perceive(
                {
                    "source": self.images.shapes,
                    "question": "describe what is happening in this scene",
                    "allow_vlm": False,
                }
            )
        )

        self.assertFalse(result.escalated)
        self.assertIsNone(result.capability(PerceptionCapability.VLM))
        self.assertNotIn("never asked", result.summary)

    def test_a_spent_latency_budget_refuses_escalation_by_name(self) -> None:
        provider = ScriptedVisionProvider('{"summary": "too late"}')
        governor = _AllowGovernor()
        engine = self._engine(provider, gate=PerceptionResourceGate(governor=governor))

        result = run(
            engine.perceive(
                {
                    "source": self.images.shapes,
                    "question": "describe what is happening in this scene",
                    "max_latency_ms": 0,
                }
            )
        )

        row = result.capability(PerceptionCapability.VLM)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("latency budget", row.reason)
        self.assertEqual(governor.asked,
            [], "the governor is not asked about a budget already spent")
        self.assertEqual(provider.calls, [])


# ── consumers, privacy, honesty ────────────────────────────────────────────


@dataclass
class _FakeAgentState:
    """Phase 20's ``advanced`` contract, minimal — the adapter is duck-typed on it."""

    observations: tuple[dict[str, Any], ...] = ()
    environment_state: dict[str, Any] = field(default_factory=dict)

    def advanced(
        self,
        *,
        observation: dict[str, Any] | None = None,
        environment_state: dict[str, Any] | None = None,
        **_: Any,
    ) -> _FakeAgentState:
        return replace(
            self,
            observations=(*self.observations, observation) if observation else self.observations,
            environment_state=dict(environment_state or self.environment_state),
        )


class ConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_observation_adapter_fits_agent_state(self) -> None:
        from novacontrol.perception import attach_observation

        scene = build_scene(
            Frame(path="/tmp/a.png"),
            objects=(DetectedObject(label="region", bbox=BBox(1, 1, 4, 4)),),
        )
        observation = scene_observation(scene)

        self.assertEqual(observation["kind"], "perception.scene")
        self.assertEqual(observation["object_count"], 1)
        self.assertTrue(observation["present"])
        result = run(
            PerceptionEngine(vision=VisionManager(provider=NullVisionProvider())).perceive(
                {"source": self.images.shapes, "question": "what objects are visible"}
            )
        )

        advanced = attach_observation(_FakeAgentState(), result)

        self.assertEqual(len(advanced.observations), 1)
        self.assertIn("status", advanced.observations[0])
        self.assertIn("perception", advanced.environment_state)

    def test_perception_observation_of_a_refusal_says_so(self) -> None:
        result = run(
            PerceptionEngine(vision=VisionManager(provider=NullVisionProvider())).perceive(
                {"source_kind": "camera"}
            )
        )

        observation = perception_observation(result)

        self.assertEqual(observation["status"], "unavailable")
        self.assertFalse(observation["present"])
        self.assertIn("camera", observation["status_reason"])

    def test_a_plain_object_still_gets_the_observation_back(self) -> None:
        from novacontrol.perception import attach_observation

        result = run(
            PerceptionEngine(vision=VisionManager(provider=NullVisionProvider())).perceive(
                {"source_kind": "camera"}
            )
        )

        observation = attach_observation(object(), result)

        self.assertEqual(observation["kind"], "perception.scene")


class PrivacyAndHonestyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))
        cls.engine = PerceptionEngine(
            vision=VisionManager(provider=NullVisionProvider(), ocr=ScriptedOcr(SCRIPTED_WORDS)),
            ocr=ScriptedOcr(SCRIPTED_WORDS),
        )
        run(cls.engine.perceive({"source": cls.images.shapes, "allow_segmentation": True}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_the_wire_shape_holds_no_pixels_masks_or_paths_from_other_frames(self) -> None:
        result = run(self.engine.perceive({"source": self.images.blank}))
        dumped = json.dumps(result.to_dict())

        self.assertNotIn("base64", dumped)
        self.assertNotIn('"mask"', dumped)
        self.assertNotIn("image_bytes", dumped)
        # The caller's own source may travel (it is their request); nothing else may.
        self.assertNotIn(self.images.shapes, dumped)

    def test_the_layer_never_stores_or_executes(self) -> None:
        status = self.engine.status()
        flags = overview()

        self.assertFalse(status["action_execution"])
        self.assertFalse(status["raw_frames_stored"])
        for flag in (
            "cuda_required",
            "automatic_model_loading",
            "automatic_model_downloads",
            "stores_raw_frames",
            "stores_masks",
            "stores_hidden_reasoning",
            "action_execution",
            "predicts_future_state",
        ):
            self.assertIs(flags[flag], False, f"{flag} must be declared False")

    def test_no_module_in_the_package_shells_out_or_touches_the_network(self) -> None:
        package = Path(__file__).resolve().parents[1] / "src" / "novacontrol" / "perception"
        offenders: list[str] = []
        for path in package.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for needle in ("subprocess", "os.system", "os.popen", "requests.", "httpx.", "socket."):
                if needle in source:
                    offenders.append(f"{path.name}: {needle}")

        self.assertEqual(offenders, [],
            "perception observes; it neither shells out nor reaches the network")

    def test_capability_classification_uses_only_the_agreed_vocabulary(self) -> None:
        allowed = {state.value for state in CapabilityState}

        self.assertEqual(allowed, {
            "implemented",
            "partially_implemented",
            "provider_dependent",
            "mock",
            "unavailable",
            "future",
        })
        for definition in CAPABILITY_TABLE:
            self.assertIn(definition.state.value, allowed)
            self.assertTrue(definition.description)
        self.assertEqual(
            {row.name.value: row.state.value for row in CAPABILITY_TABLE},
            {
                "ocr": "implemented",
                "detection": "partially_implemented",
                "segmentation": "partially_implemented",
                "tracking": "implemented",
                "relationships": "implemented",
                "temporal": "implemented",
                "abstraction": "implemented",
                "vlm": "provider_dependent",
            },
        )

    def test_deferred_phases_are_named_not_implied(self) -> None:
        listed = " ".join(DEFERRED_PHASES)

        for phase in ("Phase 22", "Phase 23", "Phase 24", "Phase 25", "Phase 26"):
            self.assertIn(phase, listed)
        self.assertEqual(PERCEPTION_SCHEMA_VERSION, "phase21.1")


class OverviewContractTests(unittest.TestCase):
    def test_overview_is_the_machine_readable_posture(self) -> None:
        data = overview()

        self.assertEqual(data["phase"], "phase21")
        self.assertEqual(data["frame_sources"], ["image", "sequence", "screen", "camera", "test"])
        self.assertEqual(data["providers"]["camera"], "unavailable (no camera backend ships)")
        self.assertIn("provider_dependent", str(data["providers"]["vlm"]))
        self.assertEqual(len(data["capability_details"]), len(CAPABILITY_TABLE))

    def test_status_and_overview_agree_on_the_honesty_flags(self) -> None:
        engine = PerceptionEngine()
        status = engine.status()

        self.assertFalse(status["cuda_required"])
        self.assertFalse(status["automatic_model_loading"])
        self.assertFalse(status["action_execution"])
        self.assertEqual(status["providers"]["vlm"]["available"], False)


# ── the HTTP surface ───────────────────────────────────────────────────────

try:  # httpx is optional; the API tests skip cleanly when it is absent
    from fastapi.testclient import TestClient  # noqa: F401  (import probe)

    _HAS_TEST_CLIENT = True
except Exception:  # noqa: BLE001 - any import failure means no HTTP client
    _HAS_TEST_CLIENT = False


@unittest.skipUnless(_HAS_TEST_CLIENT, "fastapi.testclient requires httpx")
class PerceptionApiTests(unittest.TestCase):
    """The three routes, through the REAL create_app() factory and TestClient."""

    @classmethod
    def setUpClass(cls) -> None:
        """ONE application for the class: these routes are read-only and stateless."""
        from unittest import mock

        from fastapi.testclient import TestClient

        from novacontrol.api.app import create_app
        from novacontrol.application import NovaControlApplication
        from novacontrol.browser import NoopBrowserRunner
        from novacontrol.desktop import NoopDesktopRunner

        cls._tmp = TemporaryDirectory()
        cls.images = _ImageSet(Path(cls._tmp.name))
        cls._data = TemporaryDirectory()
        cls._nova: Any = None

        def isolated(**kwargs: Any) -> Any:
            kwargs["data_dir"] = cls._data.name
            app = NovaControlApplication(**kwargs)
            cls._nova = app
            return app

        cls._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=isolated),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in cls._patchers:
            patcher.start()
        cls.client = TestClient(create_app())
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        for patcher in reversed(cls._patchers):
            patcher.stop()
        cls._data.cleanup()
        cls._tmp.cleanup()

    def test_status_route_reports_wiring_and_no_content(self) -> None:
        response = self.client.get("/perception/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("capabilities", body)
        self.assertIn("providers", body)
        self.assertFalse(body["action_execution"])
        self.assertNotIn(self.images.shapes, json.dumps(body))

    def test_capabilities_route_publishes_the_classification(self) -> None:
        response = self.client.get("/perception/capabilities")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], PERCEPTION_SCHEMA_VERSION)
        self.assertEqual(body["count"], len(CAPABILITY_TABLE))
        states = {row["name"]: row["state"] for row in body["capabilities"]}
        self.assertEqual(states["ocr"], "implemented")
        self.assertEqual(states["vlm"], "provider_dependent")
        self.assertTrue(body["deferred"])

    def test_perceive_route_returns_a_structured_scene(self) -> None:
        response = self.client.post(
            "/perception", json={"source": self.images.shapes,
                "question": "what objects are visible"}
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], {"success", "partial"})
        self.assertTrue(body["scene"]["objects"])
        self.assertNotIn("base64", json.dumps(body))

    def test_perceive_route_requires_a_source(self) -> None:
        response = self.client.post("/perception", json={})

        self.assertEqual(response.status_code, 422)
        self.assertIn("source", response.json()["detail"])

    def test_perceive_route_reports_an_unreadable_frame_as_an_outcome(self) -> None:
        response = self.client.post("/perception", json={"source": self.images.missing})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertTrue(body["status_reason"])

    def test_perceive_route_answers_a_camera_request_honestly(self) -> None:
        response = self.client.post("/perception", json={"source_kind": "camera"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertIn("camera", body["status_reason"])

    def test_the_three_routes_are_in_the_surface_and_owned(self) -> None:
        from novacontrol.api.models import ApiSurface
        from novacontrol.api.route_consumers import NON_RENDER_ROLES, ROUTE_CONSUMERS

        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        for route in (("GET", "/perception/status"),
            ("GET", "/perception/capabilities"), ("POST", "/perception")):
            self.assertIn(route, routes)
            self.assertIn(ROUTE_CONSUMERS[route],
                NON_RENDER_ROLES, "reading only: no panel owns it")


if __name__ == "__main__":
    unittest.main()
