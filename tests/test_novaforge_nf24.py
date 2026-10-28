"""NF-24 — real-game state understanding: screens, transitions, and evidence.

Every case here is either a deterministic fixture or a disposable fake seam.
No game is launched and no pixels are read; the live-game half of the phase is
reported separately in ``docs/NOVAFORGE_NF24_REAL_GAME_STATE_UNDERSTANDING_REPORT.md``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from novacontrol.novaforge.knowledge import state_candidates
from novacontrol.novaforge.objectives import (
    ObjectiveCategory,
    ObjectiveProfile,
    ObjectiveProfileRegistry,
    ObjectiveRule,
    ObjectiveStatus,
    interpret,
)
from novacontrol.novaforge.scene import (
    MAX_TRACKED_CONTRADICTIONS,
    SceneAssessment,
    SceneKind,
    SceneRule,
    SceneTracker,
    assess_scene,
)
from novacontrol.novaforge.state import (
    SCHEMA_VERSION,
    FieldSpec,
    GameProfile,
    HudKind,
    HudReading,
    HudStatus,
    ObservationRef,
    ProfileRegistry,
    build_game_state,
)
from novacontrol.novaforge.state_service import GameStateService
from novacontrol.perception.models import BBox, OcrText, SceneRepresentation

PANEL_JS = Path("src/novacontrol/web/static/js/novaforge.js").resolve()

# ── fixtures ───────────────────────────────────────────────────────────────

SCENE_PROFILE_ID = "nf24-fixture@1"


def scene_profile(*, confidence_floor: float | None = None) -> GameProfile:
    """A fixture layout: one declared banner that names the screen."""
    return GameProfile(
        profile_id=SCENE_PROFILE_ID,
        game="NF24 FIXTURE",
        title_tokens=("nf24", "fixture"),
        version="1",
        provenance="declared for the NF-24 scene tests; not measured from a game",
        reference_width=1280,
        reference_height=720,
        fields=(
            FieldSpec(
                element_id="banner",
                kind=HudKind.STATUS,
                label="Outcome banner",
                box=BBox(x=40, y=600, width=600, height=80),
                min_chars=1,
            ),
            FieldSpec(
                element_id="objective",
                kind=HudKind.PROMPT,
                label="Objective line",
                box=BBox(x=40, y=40, width=800, height=60),
                min_chars=1,
            ),
        ),
        scene_rules=(
            SceneRule(
                SceneKind.VICTORY,
                "banner",
                ("mission complete",),
                min_confidence=confidence_floor,
            ),
            SceneRule(SceneKind.DEFEAT, "banner", ("mission failed",)),
        ),
    )


def objective_profile(rule_ids: tuple[str, ...] = ("collect",)) -> ObjectiveProfile:
    """Objective rules for the same fixture, without text-based completion.

    The point of these tests is the SCENE channel, so no rule declares
    ``completion_texts``: an objective can only be closed by the screen.
    """
    return ObjectiveProfile(
        profile_id=SCENE_PROFILE_ID,
        game="NF24 FIXTURE",
        version="1",
        provenance="declared for the NF-24 objective/scene tests",
        rules=tuple(
            ObjectiveRule(
                rule_id=rule_id,
                description=f"Fixture objective {rule_id}",
                category=ObjectiveCategory.MISSION,
                objective_field="objective",
            )
            for rule_id in rule_ids
        ),
    )


def scene(*texts: str) -> SceneRepresentation:
    """One perception scene whose text sits inside the declared banner region."""
    rows = tuple(
        OcrText(
            text=text,
            bbox=BBox(x=60, y=630, width=520, height=40),
            reading_order=index,
            source="fixture",
        )
        for index, text in enumerate(texts)
    )
    return SceneRepresentation(
        width=1280,
        height=720,
        text=rows,
        frame_id="frame-fixture",
        source_id="fixture",
    )


def observation(seq: int, *, session: str = "session-a") -> ObservationRef:
    at = (datetime(2026, 10, 27, 18, 0, 0, tzinfo=UTC) + timedelta(seconds=seq)).isoformat()
    return ObservationRef(
        observation_id=f"{session}:{seq:04d}",
        session_id=session,
        game="NF24 FIXTURE",
        seq=seq,
        at=at,
        width=1280,
        height=720,
        backend="printwindow",
    )


def build(
    seq: int,
    *texts: str,
    tracker: SceneTracker | None = None,
    previous: Any = None,
    registry: ProfileRegistry | None = None,
    session: str = "session-a",
) -> Any:
    registry = registry or ProfileRegistry((scene_profile(),))
    match = registry.resolve("NF24 FIXTURE", width=1280, height=720)
    return build_game_state(
        scene(*texts),
        observation(seq, session=session),
        sequence=seq,
        profile_match=match,
        previous=previous,
        ocr_available=True,
        detection_available=True,
        scene_tracker=tracker,
    )


def reading(
    element_id: str,
    text: str,
    *,
    kind: HudKind = HudKind.STATUS,
    confidence: float | None = None,
) -> HudReading:
    return HudReading(
        element_id=element_id,
        kind=kind,
        label=element_id,
        status=HudStatus.RECOGNIZED,
        text=text,
        confidence=confidence,
    )


# ── the rule vocabulary ────────────────────────────────────────────────────


class SceneRuleTests(unittest.TestCase):
    """A rule names a screen, a declared field and the words that identify it."""

    def test_a_rule_refuses_an_unknown_scene(self) -> None:
        with self.assertRaises(ValueError):
            SceneRule(SceneKind.UNKNOWN, "banner", ("x",))

    def test_a_rule_refuses_an_empty_field_or_no_patterns(self) -> None:
        with self.assertRaises(ValueError):
            SceneRule(SceneKind.MENU, "  ", ("new game",))
        with self.assertRaises(ValueError):
            SceneRule(SceneKind.MENU, "banner", ("   ",))

    def test_patterns_are_matched_case_insensitively_and_reported(self) -> None:
        rule = SceneRule(SceneKind.MENU, "banner", ("New Game",))
        self.assertEqual(rule.matches("Press NEW GAME to start"), "new game")
        self.assertEqual(rule.matches("continue"), "")

    def test_a_pattern_matches_whole_words_only(self) -> None:
        rule = SceneRule(SceneKind.VICTORY, "banner", ("win",))
        self.assertEqual(rule.matches("You WIN"), "win")
        self.assertEqual(rule.matches("Window controller help"), "")

    def test_a_rule_refuses_a_confidence_floor_outside_the_unit_interval(self) -> None:
        with self.assertRaises(ValueError):
            SceneRule(SceneKind.MENU, "banner", ("menu",), min_confidence=1.5)
        self.assertIsNone(SceneRule(SceneKind.MENU, "banner", ("menu",)).min_confidence)

    def test_a_profile_rule_must_name_a_field_it_declares(self) -> None:
        with self.assertRaises(ValueError) as caught:
            GameProfile(
                profile_id="bad@1",
                game="BAD",
                title_tokens=("bad",),
                version="1",
                provenance="test",
                reference_width=100,
                reference_height=100,
                fields=(
                    FieldSpec(
                        element_id="banner",
                        kind=HudKind.STATUS,
                        label="banner",
                        box=BBox(x=0, y=0, width=10, height=10),
                    ),
                ),
                scene_rules=(SceneRule(SceneKind.MENU, "elsewhere", ("menu",)),),
            )
        self.assertIn("not one of its fields", str(caught.exception))

    def test_a_profiles_scene_rules_travel_in_its_payload(self) -> None:
        payload = scene_profile().to_dict()
        self.assertEqual(
            payload["scene_rules"][0],
            {
                "scene": "victory",
                "field": "banner",
                "patterns": ["mission complete"],
                "min_confidence": None,
            },
        )
        self.assertEqual(payload["required_capabilities"], ["ocr"])

    def test_a_profile_refuses_a_capability_the_build_cannot_provide(self) -> None:
        # Requirement names are checked against the declared vocabulary, so a
        # typo cannot read as "this profile needs nothing".
        with self.assertRaises(ValueError) as caught:
            GameProfile(
                profile_id="bad@1",
                game="BAD",
                title_tokens=("bad",),
                version="1",
                provenance="test",
                reference_width=100,
                reference_height=100,
                required_capabilities=("telepathy",),
            )
        self.assertIn("unknown observation capability", str(caught.exception))

    def test_a_profile_may_declare_no_requirement_at_all(self) -> None:
        profile = GameProfile(
            profile_id="free@1",
            game="FREE",
            title_tokens=("free",),
            version="1",
            provenance="test",
            reference_width=100,
            reference_height=100,
            required_capabilities=(),
        )
        self.assertEqual(profile.required_capabilities, ())


# ── classification ─────────────────────────────────────────────────────────


class AssessSceneTests(unittest.TestCase):
    """A screen is named from evidence; ambiguity stays unknown."""

    def test_a_declared_rule_over_a_read_field_is_observed(self) -> None:
        row = assess_scene(
            hud=[reading("banner", "MISSION COMPLETE")],
            text=[{"text": "MISSION COMPLETE"}],
            profile=scene_profile(),
        )
        self.assertIs(row.kind, SceneKind.VICTORY)
        self.assertEqual(row.status, "observed")
        self.assertEqual(row.source, "profile_rule")
        self.assertIn("banner", row.evidence[0])
        self.assertIn("mission complete", row.evidence[0])
        # No provider confidence was supplied, so none is claimed.
        self.assertIsNone(row.confidence)

    def test_two_rules_firing_are_a_contradiction_not_a_choice(self) -> None:
        row = assess_scene(
            hud=[reading("banner", "Mission complete / Mission failed")],
            text=[],
            profile=scene_profile(),
        )
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertEqual(row.status, "unknown")
        self.assertEqual(row.alternatives, ("victory", "defeat"))
        self.assertTrue(row.contradictions)

    def test_a_generic_cue_is_inferred_and_quotes_itself(self) -> None:
        row = assess_scene(
            hud=[],
            text=[{"text": "Game Paused"}],
            profile=None,
        )
        self.assertIs(row.kind, SceneKind.PAUSE)
        self.assertEqual(row.status, "inferred")
        self.assertEqual(row.source, "text_cue")
        self.assertIn("paused", row.evidence[0])

    def test_the_absence_of_a_rule_and_a_cue_is_unknown_with_a_reason(self) -> None:
        row = assess_scene(hud=[], text=[{"text": "LEVEL 26"}], profile=scene_profile())
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertEqual(row.status, "unknown")
        self.assertIn("no declared scene rule fired", row.evidence[0])

    def test_unavailable_ocr_is_named_as_the_reason_no_screen_was_read(self) -> None:
        row = assess_scene(hud=[], text=[], profile=None, ocr_available=False)
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertIn("no OCR engine", row.evidence[0])

    def test_a_menu_cue_and_a_completion_cue_together_stay_unknown(self) -> None:
        # A menu item "NEW GAME" plus "MISSION COMPLETE" is two different screens
        # in one frame: neither is promoted.
        row = assess_scene(
            hud=[],
            text=[{"text": "NEW GAME"}, {"text": "MISSION COMPLETE"}],
            profile=None,
        )
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertTrue(row.contradictions)
        self.assertEqual(set(row.alternatives), {"menu", "victory"})

    def test_an_unknown_assessment_cannot_claim_to_be_observed(self) -> None:
        with self.assertRaises(ValueError):
            SceneAssessment(kind=SceneKind.UNKNOWN, status="observed")

    def test_a_cue_must_be_the_whole_words_it_names(self) -> None:
        # "game over" inside "game overlay" is not a death screen.
        row = assess_scene(
            hud=[], text=[{"text": "GAME OVERLAY ready"}], profile=None
        )
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertEqual(row.source, "none")

    def test_a_mission_instruction_does_not_name_a_defeat_screen(self) -> None:
        # "Defeat the boss" is an objective, not an outcome. A bare screen noun
        # is not a cue, so this stays unknown instead of failing the mission.
        row = assess_scene(
            hud=[], text=[{"text": "Defeat the boss"}], profile=None
        )
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertEqual(row.alternatives, ())

    def test_a_profile_requiring_ocr_is_refused_when_ocr_is_missing(self) -> None:
        # The reading below WOULD fire the profile's victory rule. The requirement
        # is a precondition, so the answer is a structured unknown, not victory.
        row = assess_scene(
            hud=[reading("banner", "MISSION COMPLETE")],
            text=[{"text": "MISSION COMPLETE"}],
            profile=scene_profile(),
            ocr_available=False,
            ocr_reason="the OCR engine exited",
        )
        self.assertIs(row.kind, SceneKind.UNKNOWN)
        self.assertEqual(row.source, "capability")
        self.assertIn("requires OCR", row.evidence[0])
        self.assertIn("the OCR engine exited", row.evidence[0])

    def test_a_confidence_floor_holds_a_rule_back_until_the_reading_is_measured(self) -> None:
        profile = scene_profile(confidence_floor=0.7)
        unmeasured = assess_scene(
            hud=[reading("banner", "MISSION COMPLETE")],
            text=[{"text": "MISSION COMPLETE"}],
            profile=profile,
        )
        self.assertIs(unmeasured.kind, SceneKind.UNKNOWN)
        self.assertTrue(any("no measured confidence" in item for item in unmeasured.evidence))
        # The same words also appear as a generic victory cue; a cue weaker than
        # the rejected field read is not allowed to decide the same claim.
        self.assertEqual(unmeasured.alternatives, ())
        weak = assess_scene(
            hud=[reading("banner", "MISSION COMPLETE", confidence=0.5)],
            text=[{"text": "MISSION COMPLETE"}],
            profile=profile,
        )
        self.assertIs(weak.kind, SceneKind.UNKNOWN)
        self.assertTrue(any("below the declared floor 0.70" in item for item in weak.evidence))
        strong = assess_scene(
            hud=[reading("banner", "MISSION COMPLETE", confidence=0.92)],
            text=[{"text": "MISSION COMPLETE"}],
            profile=profile,
        )
        self.assertIs(strong.kind, SceneKind.VICTORY)
        self.assertEqual(strong.status, "observed")
        self.assertEqual(strong.confidence, 0.92)


# ── temporal confirmation ─────────────────────────────────────────────────


class SceneTrackerTests(unittest.TestCase):
    """One frame is never a transition."""

    def tracker(self, confirmations: int = 2) -> SceneTracker:
        return SceneTracker(confirmations=confirmations)

    def victory(self) -> SceneAssessment:
        return SceneAssessment(kind=SceneKind.VICTORY, status="observed", source="profile_rule")

    def test_one_frame_is_pending_and_two_confirm(self) -> None:
        tracker = self.tracker()
        first = tracker.observe(self.victory(), "obs:1")
        self.assertEqual(first.state, "pending")
        self.assertEqual(first.confirmations, 1)
        self.assertEqual(first.required, 2)
        self.assertEqual(first.confirmed, "unknown")
        second = tracker.observe(self.victory(), "obs:2")
        self.assertEqual(second.state, "confirmed")
        self.assertEqual(second.confirmed, "victory")
        self.assertEqual(second.previous, "")
        self.assertIs(tracker.confirmed, SceneKind.VICTORY)

    def test_a_required_run_of_one_confirms_immediately(self) -> None:
        tracker = self.tracker(1)
        verdict = tracker.observe(self.victory(), "obs:1")
        self.assertEqual(verdict.state, "confirmed")
        self.assertIs(tracker.confirmed, SceneKind.VICTORY)

    def test_the_same_observation_read_twice_never_counts_twice(self) -> None:
        tracker = self.tracker()
        tracker.observe(self.victory(), "obs:1")
        again = tracker.observe(self.victory(), "obs:1")
        self.assertEqual(again.state, "duplicate")
        self.assertEqual(tracker.streak, 1)
        self.assertEqual(tracker.observe(self.victory(), "obs:2").state, "confirmed")

    def test_an_unreadable_frame_neither_confirms_nor_resets_the_run(self) -> None:
        tracker = self.tracker()
        tracker.observe(self.victory(), "obs:1")
        blank = tracker.observe(
            SceneAssessment(kind=SceneKind.UNKNOWN, status="unknown"), "obs:2"
        )
        self.assertEqual(blank.state, "none")
        self.assertEqual(tracker.streak, 1)
        self.assertEqual(tracker.observe(self.victory(), "obs:3").state, "confirmed")

    def test_a_candidate_that_flips_before_confirmation_is_a_contradiction(self) -> None:
        tracker = self.tracker()
        tracker.observe(self.victory(), "obs:1")
        defeat = SceneAssessment(kind=SceneKind.DEFEAT, status="observed")
        verdict = tracker.observe(defeat, "obs:2")
        self.assertEqual(verdict.state, "contradicted")
        self.assertEqual(tracker.streak, 1)
        self.assertIn("changed from 'victory' to 'defeat'", verdict.reason)

    def test_the_confirmed_scene_does_not_restart_a_run_every_frame(self) -> None:
        tracker = self.tracker()
        tracker.observe(self.victory(), "obs:1")
        tracker.observe(self.victory(), "obs:2")
        steady = tracker.observe(self.victory(), "obs:3")
        self.assertEqual(steady.state, "none")
        self.assertEqual(steady.confirmed, "victory")
        self.assertEqual(tracker.streak, 0)

    def test_a_persisted_run_is_restored_instead_of_starting_over(self) -> None:
        original = self.tracker()
        original.observe(self.victory(), "obs:1")
        restored = self.tracker()
        restored.restore(original.last)
        self.assertEqual(restored.streak, 1)
        self.assertEqual(restored.observe(self.victory(), "obs:2").state, "confirmed")
        resumed = self.tracker()
        resumed.restore(original.last)
        # The same observation replayed after a restart is still a duplicate.
        self.assertEqual(resumed.observe(self.victory(), "obs:1").state, "duplicate")

    def test_a_sequence_gap_restarts_the_run_instead_of_confirming_across_it(self) -> None:
        tracker = self.tracker()
        tracker.observe(self.victory(), "obs:1", sequence=1)
        after_gap = tracker.observe(self.victory(), "obs:5", sequence=5)
        # Two agreeing frames, but four observations were never seen: pending.
        self.assertEqual(after_gap.state, "pending")
        self.assertTrue(after_gap.interrupted)
        self.assertEqual(after_gap.confirmations, 1)
        self.assertIn("skipped from 1 to 5", after_gap.reason)
        self.assertIn("capture interruption", " ".join(tracker.contradictions))
        confirmed = tracker.observe(self.victory(), "obs:6", sequence=6)
        self.assertEqual(confirmed.state, "confirmed")
        self.assertFalse(confirmed.interrupted)

    def test_a_contiguous_run_needs_no_gap_handling(self) -> None:
        tracker = self.tracker()
        first = tracker.observe(self.victory(), "obs:1", sequence=1)
        second = tracker.observe(self.victory(), "obs:2", sequence=2)
        third = tracker.observe(self.victory(), "obs:3", sequence=3)
        for verdict in (first, second, third):
            self.assertFalse(verdict.interrupted)
        self.assertEqual(first.state, "pending")
        self.assertEqual(second.state, "confirmed")
        self.assertEqual(second.sequence, 2)
        self.assertEqual(third.state, "none")
        self.assertEqual(third.sequence, 3)

    def test_a_restored_run_notices_that_capture_resumed_elsewhere(self) -> None:
        original = self.tracker()
        original.observe(self.victory(), "obs:1", sequence=1)
        resumed = self.tracker()
        resumed.restore(original.last)
        self.assertEqual(resumed.last_sequence, 1)
        verdict = resumed.observe(self.victory(), "obs:9", sequence=9)
        self.assertEqual(verdict.state, "pending")
        self.assertTrue(verdict.interrupted)

    def test_the_contradiction_log_stays_bounded(self) -> None:
        tracker = self.tracker()
        for index in range(40):
            kind = SceneKind.VICTORY if index % 2 == 0 else SceneKind.DEFEAT
            tracker.observe(
                SceneAssessment(kind=kind, status="observed"),
                f"obs:{index}",
                sequence=index + 1,
            )
        self.assertLessEqual(len(tracker.contradictions), MAX_TRACKED_CONTRADICTIONS)
        self.assertEqual(len(tracker.contradictions), MAX_TRACKED_CONTRADICTIONS)
        # The most recent note is kept; the oldest are the ones dropped.
        self.assertIn("defeat", tracker.contradictions[-1])


# ── the state model ────────────────────────────────────────────────────────


class StateSceneTests(unittest.TestCase):
    """The reading carries the screen and the transition, and keeps its gaps."""

    def test_the_state_carries_a_scene_and_a_transition(self) -> None:
        state = build(1, "MISSION COMPLETE", tracker=SceneTracker())
        payload = state.to_dict()
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["scene"]["kind"], "victory")
        self.assertEqual(payload["scene"]["status"], "observed")
        self.assertEqual(payload["transition"]["state"], "pending")
        self.assertEqual(payload["transition"]["required"], 2)

    def test_without_a_tracker_the_reading_claims_no_transition(self) -> None:
        state = build(1, "MISSION COMPLETE")
        self.assertEqual(state.transition.state, "none")
        self.assertIn("No scene tracker", state.transition.reason)
        self.assertIs(state.scene.kind, SceneKind.VICTORY)

    def test_a_confirmed_change_appears_once_in_the_change_list(self) -> None:
        tracker = SceneTracker()
        first = build(1, "MISSION COMPLETE", tracker=tracker)
        second = build(2, "MISSION COMPLETE", tracker=tracker, previous=first)
        scene_changes = [item for item in second.changes if item.kind == "scene_changed"]
        self.assertEqual(len(scene_changes), 1)
        change = scene_changes[0]
        self.assertEqual((change.before, change.after), ("", "victory"))
        self.assertFalse(change.inferred)
        # A steady screen is not a change.
        third = build(3, "MISSION COMPLETE", tracker=tracker, previous=second)
        self.assertEqual([item for item in third.changes if item.kind == "scene_changed"], [])

    def test_a_capture_gap_cannot_confirm_a_transition_across_it(self) -> None:
        tracker = SceneTracker()
        first = build(1, "MISSION COMPLETE", tracker=tracker)
        self.assertEqual(first.transition.state, "pending")
        # Observation 5 is the next one captured; 2-4 never arrived.
        second = build(5, "MISSION COMPLETE", tracker=tracker, previous=first)
        self.assertEqual(second.transition.state, "pending")
        self.assertTrue(second.transition.interrupted)
        self.assertEqual(second.transition.sequence, 5)
        self.assertEqual(second.transition.confirmed, "unknown")
        self.assertEqual(
            [item for item in second.changes if item.kind == "scene_changed"], []
        )
        third = build(6, "MISSION COMPLETE", tracker=tracker, previous=second)
        self.assertEqual(third.transition.state, "confirmed")
        self.assertEqual(third.transition.confirmed, "victory")

    def test_the_transition_carries_its_observation_sequence(self) -> None:
        payload = build(4, "MISSION COMPLETE", tracker=SceneTracker()).to_dict()
        self.assertEqual(payload["transition"]["sequence"], 4)
        self.assertFalse(payload["transition"]["interrupted"])

    def test_a_generic_cue_change_is_marked_inferred(self) -> None:
        tracker = SceneTracker(confirmations=1)
        first = build(1, "walking", tracker=tracker)
        second = build(2, "Game Paused", tracker=tracker, previous=first)
        change = next(item for item in second.changes if item.kind == "scene_changed")
        self.assertTrue(change.inferred)
        self.assertEqual(change.after, "pause")

    def test_confidence_is_the_measured_mean_or_nothing(self) -> None:
        # The fixture scene reports no confidence: the state must not invent one.
        self.assertIsNone(build(1, "MISSION COMPLETE").confidence)
        measured = SceneRepresentation(
            width=1280,
            height=720,
            text=(
                OcrText(
                    text="MISSION COMPLETE",
                    bbox=BBox(x=60, y=630, width=520, height=40),
                    reading_order=0,
                    confidence=0.8,
                    source="fixture",
                ),
            ),
            confidence=0.8,
            frame_id="frame-fixture",
            source_id="fixture",
        )
        match = ProfileRegistry((scene_profile(),)).resolve(
            "NF24 FIXTURE", width=1280, height=720
        )
        state = build_game_state(
            measured,
            observation(1),
            sequence=1,
            profile_match=match,
            ocr_available=True,
            detection_available=True,
        )
        self.assertEqual(state.confidence, 0.8)
        self.assertEqual(state.uncertainty, 0.2)
        self.assertTrue(
            any("mean of the provider confidences" in item for item in state.limitations)
        )

    def test_an_unmeasured_confidence_says_so_in_the_limitations(self) -> None:
        state = build(1, "MISSION COMPLETE")
        self.assertTrue(
            any("No confidence is reported" in item for item in state.limitations)
        )


# ── the service: persistence, session scoping, events ─────────────────────


class _FakeObserver:
    """The NF-15 surface, with every figure a test controls."""

    def __init__(self, frame: Path) -> None:
        self._frame = frame
        self.seq = 1

    def status(self) -> dict[str, Any]:
        at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        return {
            "state": "observing",
            "session_id": "session-a",
            "game": "NF24 FIXTURE",
            "target": {
                "title": "NF24 FIXTURE",
                "handle": 11,
                "pid": 22,
                "start_time": "2026-10-27T18:00:00+00:00",
            },
            "last_success": {
                "observation_id": f"session-a:{self.seq:04d}",
                "seq": self.seq,
                "at": at,
                "width": 1280,
                "height": 720,
                "backend": "printwindow",
            },
        }

    def frame_path(self) -> Path:
        return self._frame


class _DictStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def read(self, namespace: str) -> dict[str, Any]:
        return dict(self.data.get(namespace) or {})

    def write(self, namespace: str, payload: dict[str, Any]) -> None:
        self.data[namespace] = dict(payload)


def _perception_result(target: SceneRepresentation) -> Any:
    return SimpleNamespace(
        status="success",
        scene=target,
        latency={"total": 3.0},
        capability=lambda name: SimpleNamespace(available=True, reason=""),
    )


class GameStateServiceSceneTests(unittest.IsolatedAsyncioTestCase):
    """The service owns the run across frames, sessions and restarts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frame = Path(self._tmp.name) / "frame.png"
        self.frame.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.store = _DictStore()

    def service(self, observer: _FakeObserver) -> GameStateService:
        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        async def perceive(_frame: Any) -> Any:
            return _perception_result(scene("MISSION COMPLETE"))

        service = GameStateService(
            observer=observer,
            perceive=perceive,
            publish=publish,
            store=self.store,
            registry=ProfileRegistry((scene_profile(),)),
        )
        self.addAsyncCleanup(service.shutdown)
        return service

    async def test_a_reading_needs_two_observations_before_it_is_a_transition(self) -> None:
        observer = _FakeObserver(self.frame)
        service = self.service(observer)
        first = await service.analyze()
        self.assertTrue(first.analyzed, first.reason)
        status = service.status()
        self.assertEqual(status["scene"]["confirmed"], "unknown")
        self.assertEqual(status["scene"]["candidate"], "victory")
        self.assertEqual(status["scene"]["confirmations"], 1)
        observer.seq = 2
        second = await service.analyze()
        self.assertTrue(second.analyzed, second.reason)
        status = service.status()
        self.assertEqual(status["scene"]["confirmed"], "victory")
        self.assertEqual(status["counts"]["scene_transitions"], 1)
        payload = dict(self.events[-1][1])
        self.assertEqual(payload["scene"], "victory")
        self.assertEqual(payload["transition"], "confirmed")
        self.assertNotIn("source", payload)

    async def test_a_restart_continues_the_run_instead_of_forgetting_it(self) -> None:
        observer = _FakeObserver(self.frame)
        service = self.service(observer)
        await service.analyze()
        await service.shutdown()

        observer.seq = 2
        restarted = self.service(observer)
        self.assertEqual(restarted.status()["scene"]["confirmations"], 1)
        outcome = await restarted.analyze()
        self.assertTrue(outcome.analyzed, outcome.reason)
        self.assertEqual(restarted.status()["scene"]["confirmed"], "victory")

    async def test_a_new_session_does_not_inherit_the_confirmed_screen(self) -> None:
        observer = _FakeObserver(self.frame)
        service = self.service(observer)
        await service.analyze()
        observer.seq = 2
        await service.analyze()
        self.assertEqual(service.status()["scene"]["confirmed"], "victory")

        class OtherSession(_FakeObserver):
            def status(self) -> dict[str, Any]:
                payload = super().status()
                payload["session_id"] = "session-b"
                payload["last_success"] = {
                    **payload["last_success"],
                    "observation_id": "session-b:0001",
                }
                return payload

        other = OtherSession(self.frame)
        other.seq = 1
        service.observer = other
        outcome = await service.analyze()
        self.assertTrue(outcome.analyzed, outcome.reason)
        self.assertEqual(service.status()["scene"]["confirmed"], "unknown")

    async def test_a_reset_forgets_the_scene_run_too(self) -> None:
        observer = _FakeObserver(self.frame)
        service = self.service(observer)
        await service.analyze()
        service.reset()
        self.assertEqual(service.status()["scene"]["candidate"], "unknown")
        self.assertEqual(service.status()["scene"]["confirmations"], 0)


# ── NF-17: objectives closed by a confirmed outcome screen ────────────────


class ObjectiveSceneEvidenceTests(unittest.TestCase):
    """A confirmed outcome screen is evidence; a stale one is not."""

    def registry(self) -> ObjectiveProfileRegistry:
        return ObjectiveProfileRegistry((objective_profile(),))

    def state(
        self,
        seq: int,
        texts: tuple[str, ...],
        tracker: SceneTracker,
        *,
        previous: Any = None,
        session: str = "session-a",
    ) -> Any:
        rows = tuple(
            OcrText(
                text=text,
                bbox=(
                    BBox(x=60, y=630, width=520, height=40)
                    if "MISSION" in text
                    else BBox(x=60, y=50, width=520, height=40)
                ),
                reading_order=index,
                source="fixture",
            )
            for index, text in enumerate(texts)
        )

        class _Scene(SceneRepresentation):
            pass

        target = SceneRepresentation(
            width=1280,
            height=720,
            text=rows,
            frame_id="frame-fixture",
            source_id="fixture",
        )
        match = ProfileRegistry((scene_profile(),)).resolve(
            "NF24 FIXTURE", width=1280, height=720
        )
        return build_game_state(
            target,
            observation(seq, session=session),
            sequence=seq,
            profile_match=match,
            previous=previous,
            ocr_available=True,
            detection_available=True,
            scene_tracker=tracker,
        )

    def test_the_only_current_objective_is_completed_by_a_confirmed_victory(self) -> None:
        tracker = SceneTracker()
        first = self.state(1, ("Objective: collect the audit tokens",), tracker)
        before = interpret(first, registry=self.registry())
        self.assertEqual(before.objectives[0].status, ObjectiveStatus.DISCOVERED)

        self.state(2, ("Objective: collect the audit tokens", "MISSION COMPLETE"), tracker)
        third = self.state(3, ("MISSION COMPLETE",), tracker)
        self.assertEqual(third.transition.state, "confirmed")

        after = interpret(third, before, registry=self.registry())
        objective = after.objectives[0]
        self.assertEqual(objective.status, ObjectiveStatus.COMPLETED)
        kinds = [item.kind.value for item in objective.evidence]
        self.assertIn("scene", kinds)
        summary = next(item.summary for item in objective.evidence if item.kind.value == "scene")
        self.assertIn("confirmed victory screen", summary)
        self.assertIn("observed", summary)
        events = [item.kind.value for item in after.events]
        self.assertIn("completed", events)

    def test_a_defeat_screen_fails_the_objective_instead_of_completing_it(self) -> None:
        tracker = SceneTracker()
        first = self.state(1, ("Objective: collect the audit tokens",), tracker)
        before = interpret(first, registry=self.registry())
        self.state(2, ("Objective: collect the audit tokens", "MISSION FAILED"), tracker)
        third = self.state(3, ("MISSION FAILED",), tracker)
        after = interpret(third, before, registry=self.registry())
        objective = after.objectives[0]
        self.assertEqual(objective.status, ObjectiveStatus.FAILED)
        self.assertIn("scene", [item.kind.value for item in objective.evidence])

    def test_two_current_objectives_are_never_closed_by_one_banner(self) -> None:
        tracker = SceneTracker()
        registry = ObjectiveProfileRegistry((objective_profile(("collect", "escort")),))
        first = self.state(
            1, ("Objective: collect the audit tokens", "Objective: escort the witness"), tracker
        )
        before = interpret(first, registry=registry)
        self.assertEqual(len(before.objectives), 2)
        self.state(2, ("Objective: collect the audit tokens", "MISSION COMPLETE"), tracker)
        third = self.state(3, ("MISSION COMPLETE",), tracker)
        after = interpret(third, before, registry=registry)
        self.assertEqual(
            {item.status for item in after.objectives},
            {ObjectiveStatus.NOT_VERIFIABLE},
        )
        self.assertTrue(
            any(
                "none was completed by it" in note
                for note in after.diagnostics.unattributed
            )
        )

    def test_two_displayed_objectives_are_never_closed_by_one_banner(self) -> None:
        # The same frame shows the banner AND both objective lines. The scene is
        # confirmed, but a run outcome cannot close two objectives by guesswork.
        tracker = SceneTracker()
        registry = ObjectiveProfileRegistry((objective_profile(("collect", "escort")),))
        first = self.state(
            1, ("Objective: collect the audit tokens", "Objective: escort the witness"), tracker
        )
        before = interpret(first, registry=registry)
        self.state(
            2,
            (
                "Objective: collect the audit tokens",
                "Objective: escort the witness",
                "MISSION COMPLETE",
            ),
            tracker,
        )
        confirmed = self.state(
            3,
            (
                "Objective: collect the audit tokens",
                "Objective: escort the witness",
                "MISSION COMPLETE",
            ),
            tracker,
        )
        self.assertEqual(confirmed.transition.state, "confirmed")
        after = interpret(confirmed, before, registry=registry)
        self.assertNotEqual(
            {item.status for item in after.objectives},
            {ObjectiveStatus.COMPLETED},
        )
        self.assertTrue(
            any(
                "none was closed by it" in note
                for note in after.diagnostics.unattributed
            )
        )

    def test_a_victory_screen_that_stays_up_is_not_fresh_evidence(self) -> None:
        tracker = SceneTracker()
        first = self.state(1, ("Objective: collect the audit tokens",), tracker)
        before = interpret(first, registry=self.registry())
        # Frame 2 confirms the victory screen, but the objective is still shown.
        self.state(2, ("Objective: collect the audit tokens", "MISSION COMPLETE"), tracker)
        confirmed = self.state(3, ("MISSION COMPLETE",), tracker)
        after = interpret(confirmed, before, registry=self.registry())
        self.assertEqual(after.objectives[0].status, ObjectiveStatus.COMPLETED)

        # A LATER frame on the same victory screen has no confirmed transition.
        steady = self.state(4, ("MISSION COMPLETE",), tracker, previous=confirmed)
        self.assertEqual(steady.transition.state, "none")
        later = interpret(steady, after, registry=self.registry())
        objective = later.objectives[0]
        self.assertEqual(objective.status, ObjectiveStatus.COMPLETED)
        # No new event and no re-dated evidence: the verdict stands on the frame
        # that confirmed it, and a banner that is still up adds nothing.
        self.assertNotIn("completed", [item.kind.value for item in later.events])
        scene_ref = next(item for item in objective.evidence if item.kind.value == "scene")
        self.assertEqual(scene_ref.observation_id, "session-a:0003")


# ── NF-18: the confirmed screen is knowledge; a guess is not ──────────────


class KnowledgeSceneCandidateTests(unittest.TestCase):
    """Only a confirmation is stored, and an inferred cue says so."""

    def test_a_confirmed_scene_becomes_a_situation_record(self) -> None:
        tracker = SceneTracker()
        build(1, "MISSION COMPLETE", tracker=tracker)
        state = build(2, "MISSION COMPLETE", tracker=tracker)
        scene_rows = [item for item in state_candidates(state) if item.key.startswith("scene:")]
        self.assertEqual(len(scene_rows), 1)
        candidate = scene_rows[0]
        self.assertEqual(candidate.key, "scene:victory")
        self.assertEqual(candidate.category.value, "situation")
        self.assertEqual(candidate.content["scene"], "victory")
        self.assertEqual(candidate.evidence[0].method.value, "observed")

    def test_an_unconfirmed_frame_stores_nothing(self) -> None:
        state = build(1, "MISSION COMPLETE", tracker=SceneTracker())
        self.assertEqual(
            [item for item in state_candidates(state) if item.key.startswith("scene:")], []
        )

    def test_an_inferred_screen_is_marked_inferred_in_the_evidence(self) -> None:
        tracker = SceneTracker(confirmations=1)
        state = build(1, "Game Paused", tracker=tracker)
        candidate = next(
            item for item in state_candidates(state) if item.key.startswith("scene:")
        )
        self.assertEqual(candidate.key, "scene:pause")
        self.assertEqual(candidate.evidence[0].method.value, "inferred")


# ── the panel sentence ────────────────────────────────────────────────────

SCENE_TEXT_NEEDLE = r"""
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
const cases = {
  none: L.stateSceneText(null),
  observed: L.stateSceneText({
    scene: { kind: 'victory', status: 'observed', source: 'profile_rule',
      confidence: null, alternatives: [], evidence: [] },
    transition: { state: 'confirmed', confirmed: 'victory', previous: '',
      confirmations: 0, required: 2, reason: 'confirmed after 2 observations' },
  }),
  pending: L.stateSceneText({
    scene: { kind: 'pause', status: 'inferred', source: 'text_cue', confidence: 0.5 },
    transition: { state: 'pending', confirmed: 'unknown', confirmations: 1,
      required: 2, reason: "'pause' seen 1 of 2 required time(s)" },
  }),
  contradiction: L.stateSceneText({
    scene: { kind: 'unknown', status: 'unknown', source: 'profile_rule',
      alternatives: ['victory', 'defeat'], contradictions: [] },
    transition: { state: 'contradicted', confirmed: 'unknown', confirmations: 1,
      required: 2, reason: 'the candidate changed', contradictions: ['flip'] },
  }),
  unknownReason: L.stateSceneText({
    scene: { kind: 'unknown', status: 'unknown', source: 'none',
      evidence: ['no declared scene rule fired and no unambiguous cue was read'],
      alternatives: [], contradictions: [] },
    transition: { state: 'none', confirmed: 'unknown', confirmations: 0,
      required: 2, reason: '' },
  }),
  interrupted: L.stateSceneText({
    scene: { kind: 'gameplay', status: 'observed', source: 'profile_rule',
      confidence: null, alternatives: [], evidence: [] },
    transition: { state: 'pending', confirmed: 'menu', confirmations: 1,
      required: 2, interrupted: true, sequence: 9,
      reason: 'gameplay seen 1 of 2 required time(s)' },
  }),
};
console.log(JSON.stringify(cases));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class StateSceneSentenceTests(unittest.TestCase):
    """The card says what was read, how, and whether a change is confirmed."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(SCENE_TEXT_NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(PANEL_JS)],
            capture_output=True,
            timeout=60,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        cls.data = json.loads(stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_an_unread_state_says_so(self) -> None:
        self.assertEqual(self.data["none"], "not read")

    def test_a_confirmed_screen_names_its_evidence_and_its_confirmation(self) -> None:
        text = self.data["observed"]
        self.assertIn("victory (observed via profile_rule)", text)
        self.assertIn("confirmed victory", text)

    def test_a_pending_candidate_shows_the_count_and_the_reason(self) -> None:
        text = self.data["pending"]
        self.assertIn("pause (inferred via text_cue)", text)
        self.assertIn("confidence 0.50", text)
        self.assertIn("pending 1/2", text)
        self.assertIn("required time", text)

    def test_an_ambiguous_screen_shows_its_alternatives_and_contradiction(self) -> None:
        text = self.data["contradiction"]
        self.assertIn("unknown (unknown via profile_rule)", text)
        self.assertIn("alternatives victory, defeat", text)
        self.assertIn("contradiction: flip", text)

    def test_an_unknown_screen_shows_the_reason_it_could_not_be_named(self) -> None:
        text = self.data["unknownReason"]
        self.assertIn("unknown (unknown) — no declared scene rule fired", text)
        self.assertIn("confirmed unknown", text)

    def test_a_capture_interruption_is_named_beside_the_pending_run(self) -> None:
        text = self.data["interrupted"]
        self.assertIn("pending 1/2", text)
        self.assertIn("run restarted after a capture interruption", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
