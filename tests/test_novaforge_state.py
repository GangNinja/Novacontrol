"""NF-16 — game state understanding: model, extraction, service and HTTP surface.

Every perception seam here is a fake scene built from the REAL perception
records (``SceneRepresentation``/``OcrText``/``DetectedObject``), so the reading
logic is exercised without OCR, a desktop or a model. The offline evaluation
against real captured frames lives in ``scripts/novaforge_nf16_state_eval.py``;
nothing in this file claims a game was understood.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase

from novacontrol.novaforge.state import (
    DEFAULT_MIN_CHARS,
    ENTITY_STATUSES,
    HUD_KINDS,
    HUD_STATUSES,
    PROFILE_REASONS,
    SCHEMA_VERSION,
    STATE_STATUSES,
    UNKNOWN,
    FieldSpec,
    GameProfile,
    GameState,
    HudKind,
    HudReading,
    HudStatus,
    ObservationRef,
    ProfileMatch,
    ProfileRegistry,
    StateStatus,
    build_game_state,
    builtin_profiles,
    default_registry,
    parse_number,
)
from novacontrol.novaforge.state_service import GameStateService
from novacontrol.perception.models import BBox, DetectedObject, OcrText, SceneRepresentation


def stamp(*, seconds_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def observation(**overrides: Any) -> ObservationRef:
    values: dict[str, Any] = {
        "observation_id": "obs-1:0001",
        "session_id": "obs-1",
        "game": "NC FORGE AUDIT TICK",
        "seq": 1,
        "at": stamp(),
        "width": 1832,
        "height": 718,
        "backend": "printwindow",
    }
    values.update(overrides)
    return ObservationRef(**values)


def scene(
    *,
    width: int = 1832,
    height: int = 718,
    text: tuple[tuple[str, int, int, int, int, int], ...] = (),
    regions: tuple[tuple[int, int, int, int], ...] = (),
) -> SceneRepresentation:
    """A scene from (text, line, x, y, w, h) words and (x, y, w, h) regions."""
    rows = tuple(
        OcrText(
            text=word,
            bbox=BBox(x=x, y=y, width=w, height=h),
            reading_order=line,
            source="ocr:fake",
        )
        for word, line, x, y, w, h in text
    )
    objects = tuple(
        DetectedObject(
            label="region",
            bbox=BBox(x=x, y=y, width=w, height=h),
            source="region:classical",
            metadata={"kind": "region"},
        )
        for x, y, w, h in regions
    )
    return SceneRepresentation(
        timestamp=stamp(), frame_id="f1", source_id="s1", width=width, height=height,
        objects=objects, text=rows,
    )


TICK_TEXT = (
    ("NC", 0, 75, 24, 30, 17),
    ("FORGE", 0, 114, 24, 70, 17),
    ("AUDIT", 0, 192, 24, 66, 17),
    ("TICK", 0, 266, 24, 60, 17),
    ("786", 1, 994, 182, 196, 86),
)


class StateVocabularyTests(unittest.TestCase):
    """The vocabularies are declarations a panel and a test both read."""

    def test_the_status_and_kind_tables_are_the_declared_ones(self) -> None:
        self.assertEqual(STATE_STATUSES, ("ok", "partial", "unavailable", "failed"))
        self.assertEqual(
            HUD_STATUSES, ("recognized", "absent", "ambiguous", "unreadable", "unsupported")
        )
        self.assertEqual(ENTITY_STATUSES, ("detected", "none", "unavailable"))
        self.assertIn("number", HUD_KINDS)
        self.assertIn("menu", HUD_KINDS)
        self.assertIn("profile_matched", PROFILE_REASONS)
        # NF-24 added ``scene``/``transition`` to the wire shape, which is why the
        # version moved: a consumer can now tell a payload that CAN name a screen
        # from one that never tried.
        self.assertEqual(SCHEMA_VERSION, "nf24.1")

    def test_a_number_is_parsed_only_when_the_text_states_it(self) -> None:
        self.assertEqual(parse_number("786"), (786.0, ""))
        self.assertEqual(parse_number("1,234"), (1234.0, ""))
        self.assertEqual(parse_number("87%"), (87.0, ""))
        self.assertEqual(parse_number(" +7 "), (7.0, ""))
        value, why = parse_number("LEVEL 26")
        self.assertIsNone(value)
        self.assertIn("not a number", why)
        self.assertIsNone(parse_number("")[0])


class StateModelTests(unittest.TestCase):
    """Invalid values are refused at construction, not discovered by a reader."""

    def test_a_field_spec_refuses_an_empty_box_and_a_bad_value_kind(self) -> None:
        with self.assertRaises(ValueError):
            FieldSpec(element_id="x", kind=HudKind.TEXT, label="X", box=BBox())
        with self.assertRaises(ValueError):
            FieldSpec(
                element_id="x", kind=HudKind.TEXT, label="X",
                box=BBox(x=0, y=0, width=4, height=4), value_kind="percent",
            )
        with self.assertRaises(ValueError):
            FieldSpec(
                element_id="", kind=HudKind.TEXT, label="X",
                box=BBox(x=0, y=0, width=4, height=4),
            )

    def test_a_profile_refuses_duplicate_ids_an_outside_field_and_no_reference(self) -> None:
        field = FieldSpec(
            element_id="a", kind=HudKind.TEXT, label="A", box=BBox(x=0, y=0, width=4, height=4)
        )
        with self.assertRaises(ValueError):
            GameProfile(
                profile_id="p", game="g", title_tokens=("g",), version="1", provenance="test",
                reference_width=0, reference_height=10,
            )
        with self.assertRaises(ValueError):
            GameProfile(
                profile_id="p", game="g", title_tokens=("g",), version="1", provenance="test",
                reference_width=10, reference_height=10, fields=(field, field),
            )
        with self.assertRaises(ValueError):
            GameProfile(
                profile_id="p", game="g", title_tokens=("g",), version="1", provenance="test",
                reference_width=10, reference_height=10,
                fields=(
                    FieldSpec(
                        element_id="b", kind=HudKind.TEXT, label="B",
                        box=BBox(x=8, y=8, width=9, height=9),
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            GameProfile(
                profile_id="p", game="g", title_tokens=("g",), version="",
                provenance="test", reference_width=10, reference_height=10,
            )

    def test_a_state_refuses_a_negative_sequence_and_an_impossible_coverage(self) -> None:
        base = observation()
        with self.assertRaises(ValueError):
            GameState(game="g", observation=base, sequence=-1)
        with self.assertRaises(ValueError):
            GameState(game="g", observation=base, quality={"coverage": 1.5})
        with self.assertRaises(ValueError):
            GameState(game="g", observation=base, schema_version="old")
        with self.assertRaises(ValueError):
            HudReading(
                element_id="x", kind=HudKind.TEXT, label="X",
                status=HudStatus.RECOGNIZED, confidence=3.0,
            )

    def test_an_observation_reference_needs_an_identifier(self) -> None:
        with self.assertRaises(ValueError):
            ObservationRef(observation_id="")
        with self.assertRaises(ValueError):
            ObservationRef(observation_id="a", seq=-1)


class ProfileRegistryTests(unittest.TestCase):
    """Unknown games inherit nothing, and a broken profile cannot take the pipeline down."""

    def test_the_title_tokens_decide_the_match_and_nothing_else(self) -> None:
        registry = default_registry()
        match = registry.resolve("NC FORGE AUDIT TICK - 786", width=1832, height=718)
        self.assertTrue(match.matched)
        self.assertEqual(match.reason_code, "profile_matched")
        self.assertEqual(match.profile.profile_id, "nc-forge-tick@1")
        missed = registry.resolve("Hitman 3", width=2880, height=1800)
        self.assertFalse(missed.matched)
        self.assertEqual(missed.reason_code, "no_profile_for_title")
        self.assertIn("generic", missed.reason)
        no_title = registry.resolve("")
        self.assertEqual(no_title.reason_code, "no_title")

    def test_the_tick_profile_matches_the_window_its_regions_were_measured_from(self) -> None:
        # The captured 1832x718 windows are titled "NC FORGE AUDIT"; the profile
        # must resolve for them, or its own provenance describes unreachable data.
        registry = default_registry()
        match = registry.resolve("NC FORGE AUDIT", width=1832, height=718)
        self.assertTrue(match.matched)
        self.assertEqual(match.profile.profile_id, "nc-forge-tick@1")
        partial = registry.resolve("NC FORGE", width=1832, height=718)
        self.assertFalse(partial.matched)

    def test_shipped_regions_never_overlap_each_other(self) -> None:
        # A word's centre belongs to one region. Two overlapping regions read the
        # same text twice (the harness title and its state suffix did).
        for profile in builtin_profiles():
            boxes = {spec.element_id: spec.box for spec in profile.fields}
            ids = sorted(boxes)
            for index, first in enumerate(ids):
                for second in ids[index + 1:]:
                    one, two = boxes[first], boxes[second]
                    overlaps = (
                        one.x < two.right
                        and two.x < one.right
                        and one.y < two.bottom
                        and two.y < one.bottom
                    )
                    self.assertFalse(
                        overlaps, f"{profile.profile_id}: {first} overlaps {second}"
                    )

    def test_a_declared_resolution_is_required_before_regions_are_used(self) -> None:
        registry = default_registry()
        wrong = registry.resolve("NC FORGE AUDIT TICK", width=1280, height=720)
        self.assertFalse(wrong.matched)
        self.assertEqual(wrong.reason_code, "resolution_not_declared")
        self.assertIn("1280x720", wrong.reason)

    def test_a_forced_profile_is_reported_as_forced_and_unknown_ids_are_refused(self) -> None:
        registry = default_registry()
        forced = registry.resolve("anything", profile_id="game-menu@1")
        self.assertTrue(forced.matched)
        self.assertTrue(forced.forced)
        self.assertEqual(forced.reason_code, "profile_forced")
        unknown = registry.resolve("anything", profile_id="nope@9")
        self.assertFalse(unknown.matched)
        self.assertEqual(unknown.reason_code, "profile_unknown")
        self.assertIn("nope@9", unknown.reason)

    def test_a_rejected_profile_is_recorded_and_never_raises(self) -> None:
        registry = ProfileRegistry()
        self.assertTrue(registry.register({"profile_id": "broken"}))
        self.assertIn("broken", registry.rejected)
        rows = registry.rows()
        self.assertEqual(rows[0]["profile_id"], "broken")
        self.assertFalse(rows[0]["available"])
        self.assertEqual(rows[0]["reason"].split(":")[0], "ValueError")
        self.assertEqual(registry.ids, ())
        self.assertFalse(registry.resolve("anything", profile_id="broken").matched)

    def test_a_duplicate_id_is_refused_without_replacing_the_first(self) -> None:
        registry = ProfileRegistry(builtin_profiles())
        first = registry.get("game-menu@1")
        # Selected by id, never by position: adding a profile must not silently
        # repoint a test at a different target.
        reason = registry.register(
            next(item for item in builtin_profiles() if item.profile_id == "game-menu@1")
        )
        self.assertIn("already registered", reason)
        self.assertIs(registry.get("game-menu@1"), first)


class ExtractionTests(unittest.TestCase):
    """Each declared field answers with one of five statuses, and only one is a fact."""

    def build(self, target: SceneRepresentation | None, **kwargs: Any) -> GameState:
        registry = default_registry()
        match = registry.resolve("NC FORGE AUDIT TICK", width=1832, height=718)
        return build_game_state(
            target,
            observation(),
            profile_match=match,
            ocr_available=kwargs.pop("ocr_available", True),
            detection_available=kwargs.pop("detection_available", True),
            **kwargs,
        )

    def test_a_read_field_is_a_fact_and_a_number_field_carries_its_value(self) -> None:
        state = self.build(scene(text=TICK_TEXT))
        self.assertEqual(state.status, StateStatus.OK)
        by_id = {row.element_id: row for row in state.hud}
        self.assertEqual(by_id["counter"].status, HudStatus.RECOGNIZED)
        self.assertEqual(by_id["counter"].value, 786.0)
        self.assertEqual(by_id["counter"].text, "786")
        self.assertEqual(by_id["header"].text, "NC FORGE AUDIT TICK")
        # The reading names the reader that produced it, not a fixed "ocr" tag.
        self.assertEqual(by_id["counter"].source, "ocr:fake")
        self.assertEqual(by_id["header"].source, "ocr:fake")
        self.assertEqual(state.quality["declared"], 2)
        self.assertEqual(state.quality["recognized"], 2)
        self.assertEqual(state.quality["coverage"], 1.0)
        self.assertTrue(state.quality["heuristic"])
        self.assertIsNone(state.confidence)

    def test_a_value_inside_a_labelled_line_is_read_from_the_word_box(self) -> None:
        # The reader reports "TICK 786" as one line whose union box starts left of
        # the counter region: the line's centre is outside it, the word's is not.
        merged = scene(text=(("TICK 786", 1, 640, 182, 550, 86),))
        words = tuple(
            DetectedObject(
                label=word,
                bbox=BBox(x=x, y=y, width=w, height=h),
                source="ocr:fake",
                metadata={"kind": "text", "line": 1},
            )
            for word, x, y, w, h in (("TICK", 640, 182, 272, 86), ("786", 994, 182, 196, 86))
        )
        from dataclasses import replace

        with_words = replace(merged, objects=merged.objects + words)

        line_only = self.build(merged)
        self.assertEqual(
            {row.element_id: row for row in line_only.hud}["counter"].status,
            HudStatus.ABSENT,
        )
        read = {row.element_id: row for row in self.build(with_words).hud}["counter"]
        self.assertEqual(read.status, HudStatus.RECOGNIZED)
        self.assertEqual(read.value, 786.0)
        self.assertEqual(read.text, "786")

    def test_an_empty_region_is_absent_not_unreadable(self) -> None:
        state = self.build(scene(text=TICK_TEXT[:4]))
        by_id = {row.element_id: row for row in state.hud}
        self.assertEqual(by_id["counter"].status, HudStatus.ABSENT)
        self.assertEqual(state.status, StateStatus.PARTIAL)
        self.assertIn("counter", {row.field_name for row in state.unknown})
        self.assertIn("not proof", by_id["counter"].reason)

    def test_a_missing_ocr_engine_makes_every_field_unreadable(self) -> None:
        state = self.build(scene(text=TICK_TEXT), ocr_available=False, ocr_reason="no OCR here")
        for row in state.hud:
            self.assertEqual(row.status, HudStatus.UNREADABLE)
        self.assertEqual(state.status, StateStatus.PARTIAL)
        self.assertIn("OCR is unavailable", state.reasons[0])

    def test_too_short_text_is_ambiguous_and_an_unparsed_number_is_ambiguous(self) -> None:
        short = self.build(scene(text=(TICK_TEXT[0], ("7", 1, 994, 182, 20, 40))))
        counter = {row.element_id: row for row in short.hud}["counter"]
        self.assertEqual(counter.status, HudStatus.AMBIGUOUS)
        self.assertIn("too short", counter.reason)
        worded = self.build(scene(text=(("LEVEL 26", 1, 980, 180, 240, 90),)))
        counter = {row.element_id: row for row in worded.hud}["counter"]
        self.assertEqual(counter.status, HudStatus.AMBIGUOUS)
        self.assertIsNone(counter.value)
        self.assertIn("not a number", counter.reason)

    def test_a_declared_kind_this_build_cannot_measure_is_unsupported(self) -> None:
        profile = GameProfile(
            profile_id="bars@1", game="bars", title_tokens=("bars",), version="1",
            provenance="test", reference_width=100, reference_height=100,
            fields=(
                FieldSpec(
                    element_id="health", kind=HudKind.HEALTH, label="Health",
                    box=BBox(x=0, y=0, width=40, height=10),
                ),
            ),
        )
        registry = ProfileRegistry((profile,))
        state = build_game_state(
            scene(width=100, height=100),
            observation(width=100, height=100),
            profile_match=registry.resolve("bars", width=100, height=100),
            ocr_available=True,
            detection_available=True,
        )
        self.assertEqual(state.hud[0].status, HudStatus.UNSUPPORTED)
        self.assertTrue(any("health" in item for item in state.unsupported))

    def test_a_frame_with_no_measured_size_cannot_place_regions(self) -> None:
        registry = default_registry()
        match = registry.resolve("NC FORGE AUDIT TICK")  # no width/height
        broken = scene()
        broken = SceneRepresentation(
            timestamp=broken.timestamp, frame_id="f", source_id="s",
            width=None, height=None, objects=broken.objects, text=broken.text,
        )
        state = build_game_state(
            broken,
            observation(width=None, height=None),
            profile_match=match,
            ocr_available=True,
            detection_available=True,
        )
        for row in state.hud:
            self.assertEqual(row.status, HudStatus.UNREADABLE)
        self.assertIn("no measured frame size", state.hud[0].reason)

    def test_no_profile_means_generic_text_and_no_presumed_hud(self) -> None:
        registry = default_registry()
        match = registry.resolve("Hitman 3", width=2880, height=1800)
        state = build_game_state(
            scene(width=2880, height=1800, text=(("GAME", 0, 10, 10, 60, 20),)),
            observation(width=2880, height=1800),
            profile_match=match,
            ocr_available=True,
            detection_available=True,
        )
        self.assertEqual(state.hud, ())
        self.assertEqual(len(state.text), 1)
        self.assertEqual(state.status, StateStatus.PARTIAL)
        self.assertFalse(state.profile.matched)
        self.assertTrue(any("no profile matched" in item.lower() for item in state.unsupported))
        self.assertIn("No perception profile", state.reasons[0])
        self.assertIsNone(state.interface.present)

    def test_a_missing_scene_is_unavailable_rather_than_an_empty_screen(self) -> None:
        state = build_game_state(
            None,
            observation(),
            profile_match=ProfileMatch(),
            ocr_available=None,
            detection_available=None,
        )
        self.assertEqual(state.status, StateStatus.UNAVAILABLE)
        self.assertEqual(state.text, ())
        self.assertEqual(state.entity_status.value, "unavailable")

    def test_entities_are_geometry_with_an_unknown_kind(self) -> None:
        state = self.build(scene(regions=((100, 400, 60, 60),)))
        self.assertEqual(state.entity_status.value, "detected")
        entity = state.entities[0]
        self.assertEqual(entity.kind, UNKNOWN)
        self.assertFalse(entity.classified)
        self.assertEqual(entity.observation_id, "obs-1:0001")
        self.assertIn("semantic detector", entity.reason)

    def test_no_detection_is_distinguished_from_an_unavailable_detector(self) -> None:
        none = self.build(scene())
        self.assertEqual(none.entity_status.value, "none")
        self.assertIn("found no region", none.entity_reason)
        unavailable = self.build(scene(), detection_available=False, detection_reason="no detector")
        self.assertEqual(unavailable.entity_status.value, "unavailable")
        self.assertIn("no detector", unavailable.entity_reason)

    def test_a_region_inside_a_declared_hud_area_is_marked_as_overlapping(self) -> None:
        state = self.build(
            scene(regions=((960, 170, 240, 120),)),
        )
        self.assertTrue(state.entities[0].overlaps_hud)

    def test_the_interface_summary_is_derived_and_names_its_evidence(self) -> None:
        profile = next(
            item for item in builtin_profiles() if item.profile_id == "game-menu@1"
        )
        registry = ProfileRegistry((profile,))
        state = build_game_state(
            scene(
                width=2880,
                height=1800,
                text=(
                    ("GAME", 0, 1180, 816, 82, 17),
                    ("MENU", 0, 1268, 816, 83, 17),
                    ("SINGLE", 1, 1180, 850, 58, 13),
                    ("PLAYER", 1, 1243, 850, 64, 12),
                ),
            ),
            observation(width=2880, height=1800),
            profile_match=registry.resolve("GAME MENU", width=2880, height=1800),
            ocr_available=True,
            detection_available=True,
        )
        self.assertEqual(state.interface.kind, "menu")
        self.assertTrue(state.interface.present)
        self.assertTrue(state.interface.derived)
        # The labels are the TEXT the OCR read inside the declared region, not a
        # label the state invented for it.
        self.assertIn("SINGLE PLAYER", state.interface.labels)
        self.assertIn("declared interface element", state.interface.evidence)


class ChangeTrackingTests(unittest.TestCase):
    """Changes are observed diffs of two readings of one session — nothing inferred."""

    def state_for(
        self,
        text: tuple[tuple[str, int, int, int, int, int], ...],
        *,
        seq: int = 1,
        session: str = "obs-1",
        previous: GameState | None = None,
        regions: tuple[tuple[int, int, int, int], ...] = (),
    ) -> GameState:
        registry = default_registry()
        match = registry.resolve("NC FORGE AUDIT TICK", width=1832, height=718)
        return build_game_state(
            scene(text=text, regions=regions),
            observation(seq=seq, session_id=session),
            profile_match=match,
            previous=previous,
            ocr_available=True,
            detection_available=True,
        )

    def test_a_changed_counter_is_one_observed_value_change(self) -> None:
        first = self.state_for(TICK_TEXT)
        second = self.state_for(TICK_TEXT[:-1] + (("804", 1, 994, 182, 196, 86),), seq=2, previous=first)
        self.assertTrue(second.tracking.compared)
        self.assertEqual(second.tracking.dropped_observations, 0)
        kinds = [item.kind for item in second.changes]
        self.assertEqual(kinds, ["hud_value_changed"])
        change = second.changes[0]
        self.assertEqual((change.before, change.after), ("786", "804"))
        self.assertEqual(change.field_name, "counter")
        self.assertFalse(change.inferred)

    def test_a_status_move_is_reported_with_both_ends(self) -> None:
        first = self.state_for(TICK_TEXT)
        gone_text = (TICK_TEXT[0], TICK_TEXT[1], TICK_TEXT[2], TICK_TEXT[3])
        second = self.state_for(gone_text, seq=2, previous=first)
        change = next(item for item in second.changes if item.kind == "hud_status_changed")
        self.assertEqual(change.field_name, "counter")
        self.assertEqual((change.before, change.after), ("recognized", "absent"))

    def test_a_field_absent_from_the_previous_reading_is_reported_as_appearing(self) -> None:
        # A profile version that adds a field is the reachable case: the ids
        # match, so the readings ARE compared, and the new field appeared.
        complete = self.state_for(TICK_TEXT)
        from dataclasses import replace

        older = replace(complete, hud=tuple(row for row in complete.hud if row.element_id == "header"))
        newer = self.state_for(TICK_TEXT, seq=2, previous=older)
        appeared = [item for item in newer.changes if item.kind == "hud_appeared"]
        self.assertEqual([item.field_name for item in appeared], ["counter"])

    def test_a_dropped_observation_is_counted_not_hidden(self) -> None:
        first = self.state_for(TICK_TEXT, seq=1)
        third = self.state_for(TICK_TEXT, seq=4, previous=first)
        self.assertEqual(third.tracking.dropped_observations, 2)

    def test_another_session_is_never_compared(self) -> None:
        first = self.state_for(TICK_TEXT, seq=1, session="obs-1")
        other = self.state_for(TICK_TEXT, seq=2, session="obs-2", previous=first)
        self.assertFalse(other.tracking.compared)
        self.assertEqual(other.changes, ())
        self.assertIn("different session", other.tracking.reason)

    def test_an_entity_count_change_is_a_region_count_not_an_object_identity(self) -> None:
        first = self.state_for(TICK_TEXT, seq=1, regions=((100, 400, 40, 40),))
        second = self.state_for(TICK_TEXT, seq=2, previous=first,
                                regions=((100, 400, 40, 40), (300, 500, 30, 30)))
        change = next(item for item in second.changes if item.kind == "entity_count_changed")
        self.assertEqual((change.before, change.after), ("1", "2"))
        self.assertIn("not of game objects", change.detail)

    def test_a_profile_change_stops_the_comparison_instead_of_guessing(self) -> None:
        first = self.state_for(TICK_TEXT)
        registry = default_registry()
        other_match = registry.resolve("GAME MENU", profile_id="game-menu@1")
        second = build_game_state(
            scene(width=2880, height=1800, text=TICK_TEXT),
            observation(seq=2, width=2880, height=1800),
            profile_match=other_match,
            previous=first,
            ocr_available=True,
            detection_available=True,
        )
        self.assertFalse(second.tracking.compared)
        self.assertIn("profile changed", second.tracking.reason)


class FakeObserver:
    """The NF-15 surface, standing in for a real observer in the service tests."""

    def __init__(
        self,
        *,
        session_id: str = "obs-1",
        seq: int = 1,
        at: str | None = None,
        width: int = 1832,
        height: int = 718,
        title: str = "NC FORGE AUDIT TICK - 786",
        frame: Path | None = None,
        tested: bool = True,
        interval: float | None = None,
        capture_ms: float | None = None,
    ) -> None:
        self.session_id = session_id
        self.seq = seq
        self.at = at or stamp()
        self.width = width
        self.height = height
        self.title = title
        self._frame = frame
        self.tested = tested
        #: The real observer reports these, and the freshness window is judged
        #: against them; ``None`` keeps the pre-cadence behaviour (the floor).
        self.interval = interval
        self.capture_ms = capture_ms
        self.observation_id = f"{session_id}:{seq:04d}"
        #: Target identity. A test that changes one of these mid-read is
        #: describing a DIFFERENT window, which is what supersedes a reading.
        self.handle = 17185
        self.pid = 5150
        self.start_time = "2026-10-25T09:00:00+00:00"

    def status(self) -> dict[str, Any]:
        success = (
            {
                "observation_id": self.observation_id,
                "seq": self.seq,
                "at": self.at,
                "width": self.width,
                "height": self.height,
                "backend": "printwindow",
                "capture_ms": self.capture_ms,
            }
            if self.tested
            else None
        )
        interval = (
            {"requested_seconds": self.interval, "applied_seconds": self.interval}
            if self.interval is not None
            else {}
        )
        return {
            "state": "observing",
            "session_id": self.session_id,
            "interval": interval,
            "game": "NC FORGE AUDIT TICK",
            "target": {
                "title": self.title,
                "handle": self.handle,
                "pid": self.pid,
                "start_time": self.start_time,
            },
            "last_success": success,
        }

    def frame_path(self) -> Path | None:
        return self._frame


def fake_result(target: SceneRepresentation, *, ocr: bool = True, detection: bool = True) -> Any:
    """A perception result shaped like the engine's, with no engine behind it."""
    return SimpleNamespace(
        status="success",
        scene=target,
        latency={"total": 4.0},
        capability=lambda name: SimpleNamespace(
            available=ocr if name == "ocr" else detection,
            reason="" if (ocr if name == "ocr" else detection) else f"{name} unavailable",
        ),
    )


class DictStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def read(self, namespace: str) -> dict[str, Any]:
        return dict(self.data.get(namespace) or {})

    def write(self, namespace: str, payload: dict[str, Any]) -> None:
        self.data[namespace] = dict(payload)


class GameStateServiceTests(unittest.IsolatedAsyncioTestCase):
    """The service refuses what it cannot attribute, and never blocks the loop."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frame = Path(self._tmp.name) / "frame.png"
        self.frame.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.events: list[tuple[str, dict[str, Any]]] = []

    def service(self, observer: FakeObserver, **kwargs: Any) -> GameStateService:
        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        seam = kwargs.pop("perceive", None)
        if seam is None:
            async def seam(_frame: Any) -> Any:
                return fake_result(scene(text=TICK_TEXT))

        service = GameStateService(
            observer=observer,
            perceive=seam,
            publish=publish,
            **kwargs,
        )
        self.addAsyncCleanup(service.shutdown)
        return service

    async def test_no_validated_frame_is_a_refusal_with_a_code(self) -> None:
        service = self.service(FakeObserver(tested=False))
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "no_validated_frame")
        self.assertIsNone(service.status()["state"])

    async def test_a_stale_frame_is_a_record_not_a_state(self) -> None:
        service = self.service(FakeObserver(at=stamp(seconds_ago=30)))
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "observation_stale")
        self.assertIn("record, not a current state", outcome.reason)

    async def test_a_frame_as_old_as_the_observers_own_cadence_is_read(self) -> None:
        """A 5 s interval plus 1.4 s capture makes the newest frame ~6.4 s old.

        The fixed 5 s window refused that frame as stale, so the state reader
        could never succeed on a target it could not capture more often than
        that — the refusal was of the only frame that existed.
        """
        observer = FakeObserver(
            at=stamp(seconds_ago=6.4),
            frame=self.frame,
            interval=5.0,
            capture_ms=1400.0,
        )
        outcome = await self.service(observer).analyze()
        self.assertTrue(outcome.analyzed, outcome.reason)

    async def test_a_frame_far_older_than_the_cadence_is_still_stale(self) -> None:
        observer = FakeObserver(
            at=stamp(seconds_ago=20.0),
            frame=self.frame,
            interval=5.0,
            capture_ms=1400.0,
        )
        outcome = await self.service(observer).analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "observation_stale")
        self.assertIn("7.4s", outcome.reason)

    async def test_an_unreadable_timestamp_is_refused_rather_than_assumed_fresh(self) -> None:
        service = self.service(FakeObserver(at="not-a-date"))
        outcome = await service.analyze()
        self.assertEqual(outcome.reason_code, "observation_time_unknown")

    async def test_a_missing_frame_file_is_a_refusal(self) -> None:
        service = self.service(FakeObserver(frame=None))
        outcome = await service.analyze()
        self.assertEqual(outcome.reason_code, "frame_unavailable")

    async def test_a_reading_records_one_state_and_publishes_the_event(self) -> None:
        service = self.service(FakeObserver(frame=self.frame))
        outcome = await service.analyze()
        self.assertTrue(outcome.analyzed, outcome.reason)
        assert outcome.state is not None
        self.assertEqual(outcome.state.status, StateStatus.OK)
        self.assertEqual(outcome.state.profile.profile.profile_id, "nc-forge-tick@1")
        self.assertEqual(outcome.state.observation.observation_id, "obs-1:0001")
        status = service.status()
        self.assertTrue(status["tested"])
        self.assertEqual(status["counts"]["analyzed"], 1)
        self.assertIsNotNone(status["measured"]["processing_ms"]["last"])
        self.assertEqual(len(status["history"]), 1)
        names = [name for name, _ in self.events]
        self.assertIn("novaforge.state.analyzed", names)
        payload = dict(self.events[-1][1])
        self.assertNotIn("source", payload, "a reserved envelope key must not be shadowed")
        self.assertEqual(payload["profile_reason"], "profile_matched")
        self.assertEqual(payload["fields_recognized"], 2)
        for measured in ("processing_ms", "model_load_ms"):
            self.assertIn(measured, outcome.state.to_dict()["latency"])

    async def test_a_reading_is_discarded_when_the_target_changes_under_it(self) -> None:
        # The invariant that supersedes a reading is a DIFFERENT window, not a
        # newer frame of the same one: attributing a state to a target it was not
        # read from would aim an action at the wrong window.
        observer = FakeObserver(frame=self.frame)

        async def perceive(_frame: Any) -> Any:
            observer.title = "A DIFFERENT WINDOW"
            observer.handle = 99999
            return fake_result(scene(text=TICK_TEXT))

        service = self.service(observer, perceive=perceive)
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "observation_superseded")
        self.assertEqual(service.status()["counts"]["superseded"], 1)
        self.assertIsNone(service.status()["state"])
        self.assertEqual(service.status()["counts"]["superseded"], 1)

    async def test_a_newer_frame_of_the_same_target_keeps_a_fresh_reading(self) -> None:
        # A continuously sampling observer always has a newer frame by the time
        # perception finishes; discarding every such reading made the state
        # unreadable whenever observation was running.
        observer = FakeObserver(frame=self.frame)

        async def perceive(_frame: Any) -> Any:
            observer.seq = 2
            observer.observation_id = "obs-1:0002"
            observer.at = stamp()
            return fake_result(scene(text=TICK_TEXT))

        service = self.service(observer, perceive=perceive)
        outcome = await service.analyze()
        self.assertTrue(outcome.analyzed, outcome.reason)
        assert outcome.state is not None
        # The reading says which frame it described instead of claiming the newest.
        self.assertEqual(outcome.state.observation.observation_id, "obs-1:0001")
        self.assertTrue(
            any(
                "A newer frame arrived" in line
                for line in outcome.state.to_dict()["limitations"]
            ),
            outcome.state.to_dict()["limitations"],
        )

    async def test_a_second_analysis_is_refused_while_one_is_running(self) -> None:
        async def slow(_frame: Any) -> Any:
            await asyncio.sleep(0.15)
            return fake_result(scene(text=TICK_TEXT))

        service = self.service(FakeObserver(frame=self.frame), perceive=slow)
        first = asyncio.create_task(service.analyze())
        await asyncio.sleep(0.03)
        second = await service.analyze()
        self.assertFalse(second.analyzed)
        self.assertEqual(second.reason_code, "analysis_in_progress")
        self.assertTrue((await first).analyzed)

    async def test_a_timeout_is_reported_and_the_worker_is_not_overlapped(self) -> None:
        calls = 0

        async def slower(_frame: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.4)
            return fake_result(scene(text=TICK_TEXT))

        service = self.service(
            FakeObserver(frame=self.frame), perceive=slower, analyze_timeout=0.05
        )
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "analysis_timeout")
        again = await service.analyze()
        self.assertEqual(again.reason_code, "analysis_in_progress")
        await asyncio.sleep(0.5)
        recovered = await service.analyze()
        self.assertTrue(recovered.analyzed, recovered.reason)

    async def test_cancelling_the_caller_leaves_the_service_usable(self) -> None:
        async def slow(_frame: Any) -> Any:
            await asyncio.sleep(0.05)
            return fake_result(scene(text=TICK_TEXT))

        service = self.service(FakeObserver(frame=self.frame), perceive=slow)
        task = asyncio.create_task(service.analyze())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The shielded worker finishes on its own; once it has, a fresh reading
        # is accepted instead of the service staying wedged on a cancelled one.
        await asyncio.sleep(0.2)
        again = await service.analyze()
        self.assertTrue(again.analyzed, again.reason)

    async def test_a_perception_exception_is_a_result_not_a_crash(self) -> None:
        async def broken(_frame: Any) -> Any:
            raise RuntimeError("the OCR subprocess died")

        service = self.service(FakeObserver(frame=self.frame), perceive=broken)
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "analysis_failed")
        self.assertIn("the OCR subprocess died", outcome.reason)
        status = service.status()
        self.assertEqual(status["counts"]["failed"], 1)
        self.assertEqual(status["last_error"]["reason_code"], "analysis_failed")
        self.assertIn("novaforge.state.failed", [name for name, _ in self.events])

    async def test_a_new_session_starts_its_own_sequence_and_comparison(self) -> None:
        observer = FakeObserver(frame=self.frame)
        service = self.service(observer)
        first = await service.analyze()
        assert first.state is not None
        self.assertEqual(first.state.sequence, 1)
        observer.session_id = "obs-2"
        observer.seq = 1
        observer.observation_id = "obs-2:0001"
        second = await service.analyze()
        assert second.state is not None
        self.assertEqual(second.state.sequence, 1)
        self.assertFalse(second.state.tracking.compared)

    async def test_the_history_is_bounded_and_the_store_restores_previous(self) -> None:
        store = DictStore()
        observer = FakeObserver(frame=self.frame)
        service = self.service(observer, store=store, max_states=2)
        for index in range(4):
            observer.seq = index + 1
            observer.observation_id = f"obs-1:{index + 1:04d}"
            outcome = await service.analyze()
            self.assertTrue(outcome.analyzed, outcome.reason)
        status = service.status()
        self.assertEqual(len(status["history"]), 2)
        self.assertIn("novaforge_state", store.data)
        restored = GameStateService(
            observer=observer,
            perceive=None,
            store=store,
        )
        self.assertTrue(restored.status()["previous"]["restored"])
        self.assertFalse(restored.status()["tested"])

    async def test_reset_clears_the_baseline_and_the_restored_previous(self) -> None:
        store = DictStore()
        observer = FakeObserver(frame=self.frame)
        service = self.service(observer, store=store)
        await service.analyze()
        before = service.status()["history"]
        restored = GameStateService(observer=observer, perceive=None, store=store)
        self.assertIsNotNone(restored.status()["previous"])
        body = restored.reset()
        self.assertTrue(body["reset"])
        after = restored.status()
        self.assertIsNone(after["state"])
        self.assertIsNone(after["previous"])
        self.assertEqual(len(after["history"]), len(before))

    async def test_a_refusal_is_recorded_without_counting_as_a_failure(self) -> None:
        observer = FakeObserver(frame=self.frame)
        service = self.service(observer)
        observer.at = stamp(seconds_ago=120)
        refused = await service.analyze()
        self.assertEqual(refused.reason_code, "observation_stale")
        status = service.status()
        self.assertEqual(status["counts"]["failed"], 0)
        self.assertEqual(status["last_refusal"]["reason_code"], "observation_stale")
        self.assertIn("120", status["last_refusal"]["reason"])
        self.assertIsNone(status["last_error"])
        observer.at = stamp()
        accepted = await service.analyze()
        self.assertTrue(accepted.analyzed, accepted.reason)
        self.assertIsNone(service.status()["last_refusal"])

    async def test_shutdown_refuses_further_readings_and_is_repeatable(self) -> None:
        service = self.service(FakeObserver(frame=self.frame))
        await service.analyze()
        await service.shutdown()
        await service.shutdown()
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "stopped")

    async def test_the_profile_table_includes_defaults_and_rejections(self) -> None:
        service = self.service(FakeObserver(frame=self.frame))
        table = service.profiles()
        self.assertIn("nc-forge-tick@1", table["defaults"])
        self.assertTrue(table["profiles"])
        self.assertEqual(table["rejected"], {})

    async def test_a_broken_observer_is_an_error_not_an_exception(self) -> None:
        class Broken:
            def status(self) -> dict[str, Any]:
                raise RuntimeError("observer is gone")

            def frame_path(self) -> Path | None:
                return None

        service = GameStateService(observer=Broken())
        self.addAsyncCleanup(service.shutdown)
        outcome = await service.analyze()
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "observer_unavailable")
        self.assertIn("observer is gone", outcome.reason)


class StateApiTests(_IsolatedApiTestCase):
    """The HTTP surface, with the reading service replaced by a deterministic one."""

    def _isolated_application(self, **kwargs: Any) -> Any:
        app = super()._isolated_application(**kwargs)
        self.frame = Path(self._tmp.name) / "api-frame.png"
        self.frame.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.observer = FakeObserver(frame=self.frame)

        async def perceive(_frame: Any) -> Any:
            return fake_result(scene(text=TICK_TEXT))

        async def publish(_event_type: str, _payload: dict[str, Any]) -> None:
            return None

        app.novaforge.state = GameStateService(
            observer=self.observer, perceive=perceive, publish=publish
        )
        return app

    def test_status_starts_untested(self) -> None:
        body = self._client.get("/novaforge/state").json()
        self.assertFalse(body["tested"])
        self.assertIsNone(body["state"])
        self.assertIsNone(body["measured"]["processing_ms"]["last"])
        self.assertEqual(body["counts"]["analyzed"], 0)

    def test_analyze_returns_a_state_and_reset_clears_the_baseline(self) -> None:
        response = self._client.post("/novaforge/state/analyze", json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["analyzed"], body)
        state = body["state"]
        self.assertEqual(state["schema_version"], SCHEMA_VERSION)
        self.assertEqual(state["observation"]["observation_id"], "obs-1:0001")
        self.assertEqual(state["profile"]["profile_id"], "nc-forge-tick@1")
        self.assertFalse(state["confidence"] is not None)
        reset = self._client.post("/novaforge/state/reset", json={}).json()
        self.assertTrue(reset["reset"])
        self.assertIsNone(self._client.get("/novaforge/state").json()["state"])

    def test_a_pinned_profile_id_is_honoured(self) -> None:
        body = self._client.post(
            "/novaforge/state/analyze", json={"profile_id": "game-menu@1"}
        ).json()
        self.assertTrue(body["analyzed"], body)
        self.assertEqual(body["state"]["profile"]["profile_id"], "game-menu@1")
        self.assertEqual(body["state"]["profile"]["reason_code"], "profile_forced")

    def test_a_refusal_is_a_200_with_a_reason_code_the_status_keeps(self) -> None:
        self.observer.tested = False
        body = self._client.post("/novaforge/state/analyze", json={}).json()
        self.assertFalse(body["analyzed"])
        self.assertEqual(body["reason_code"], "no_validated_frame")
        self.assertIsNone(body["state"])
        status = self._client.get("/novaforge/state").json()
        self.assertEqual(status["last_refusal"]["reason_code"], "no_validated_frame")
        self.assertEqual(status["counts"]["failed"], 0)
        self.assertIsNone(status["last_error"])

    def test_the_profiles_route_lists_the_shipped_table(self) -> None:
        body = self._client.get("/novaforge/state/profiles").json()
        ids = {row["profile_id"] for row in body["profiles"]}
        self.assertIn("nc-forge-tick@1", ids)
        self.assertIn("game-menu@1", body["defaults"])

    def test_the_routes_exist_in_the_declared_surface(self) -> None:
        from novacontrol.api.models import ApiSurface

        paths = {(route.method, route.path) for route in ApiSurface.default().routes}
        for method, path in [
            ("GET", "/novaforge/state"),
            ("POST", "/novaforge/state/analyze"),
            ("POST", "/novaforge/state/reset"),
            ("GET", "/novaforge/state/profiles"),
        ]:
            self.assertIn((method, path), paths)


class StatePanelContractTests(unittest.TestCase):
    """The card exists, starts untested, and states what a reading is not."""

    def test_the_card_markup_exists_and_starts_hidden_of_facts(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "src" / "novacontrol" / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8", errors="replace")
        for node_id in (
            "forgeStateTitle",
            "forgeStateBadge",
            "forgeStateProfile",
            "forgeStateAnalyzeButton",
            "forgeStateRefreshButton",
            "forgeStateResetButton",
            "forgeStateHint",
            "forgeStateFacts",
            "forgeStateHud",
            "forgeStateEntities",
            "forgeStateChanges",
            "forgeStateUnknown",
            "forgeStateHistory",
        ):
            self.assertIn(f'id="{node_id}"', html, node_id)
        self.assertIn("untested", html)
        self.assertIn("not an understanding of the game", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
