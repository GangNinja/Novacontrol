"""NF-17 — objective and progress understanding: model, interpretation, service.

Every reading here is built from the REAL NF-16 state records with no OCR, no
window and no model behind them, so the interpretation logic is exercised on its
own. The offline fixture evaluation lives in
``scripts/novaforge_nf17_objective_eval.py``; nothing in this file claims a
commercial game's objective was understood.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase

from novacontrol.novaforge.objective_service import ObjectiveService
from novacontrol.novaforge.objectives import (
    MAX_DESCRIPTION,
    ObjectiveCategory,
    ObjectiveDependency,
    ObjectiveEventKind,
    ObjectiveProfile,
    ObjectiveProfileRegistry,
    ObjectiveRule,
    ObjectiveStatus,
    ProgressKind,
    ProgressRecord,
    UnderstoodObjective,
    default_objective_registry,
    interpret,
    progress_fraction,
)
from novacontrol.novaforge.state import (
    GameProfile,
    GameState,
    HudKind,
    HudReading,
    HudStatus,
    ObservationRef,
    ProfileMatch,
    builtin_profiles,
)
from novacontrol.novaforge.state_service import GameStateService

MEASURED_AT = "2026-10-25T10:00:00+00:00"


def mission_profile() -> GameProfile:
    """The fixture perception profile NF-17's rules are keyed by, by id."""
    return next(item for item in builtin_profiles() if item.profile_id == "nc-forge-mission@1")


def observation(*, session: str = "s1", seq: int = 1, at: str = MEASURED_AT) -> ObservationRef:
    return ObservationRef(
        observation_id=f"{session}:{seq:04d}",
        session_id=session,
        game="NC FORGE MISSION",
        seq=seq,
        at=at,
        width=1280,
        height=720,
        backend="window_surface",
    )


def mission_state(
    *,
    session: str = "s1",
    seq: int = 1,
    at: str = MEASURED_AT,
    objective: str | None = "Collect the audit tokens",
    objective_status: HudStatus = HudStatus.RECOGNIZED,
    objective_reason: str = "",
    counter: float | None = 1.0,
    counter_status: HudStatus = HudStatus.RECOGNIZED,
    counter_reason: str = "",
    notification: str = "",
    notification_status: HudStatus = HudStatus.ABSENT,
    screen: tuple[str, ...] = (),
    matched: bool = True,
) -> GameState:
    """One NF-16 state for the mission fixture, without any perception behind it."""
    hud: list[HudReading] = []
    if objective is not None:
        hud.append(
            HudReading(
                element_id="objective",
                kind=HudKind.PROMPT,
                label="Objective line",
                status=objective_status,
                text=objective if objective_status is HudStatus.RECOGNIZED else "",
                reason=objective_reason,
            )
        )
    if counter is not None:
        hud.append(
            HudReading(
                element_id="progress",
                kind=HudKind.NUMBER,
                label="Progress counter",
                status=counter_status,
                text="" if counter is None else f"{counter:g}",
                value=counter if counter_status is HudStatus.RECOGNIZED else None,
                reason=counter_reason,
            )
        )
    hud.append(
        HudReading(
            element_id="notification",
            kind=HudKind.NOTIFICATION,
            label="Outcome banner",
            status=notification_status,
            text=notification if notification_status is HudStatus.RECOGNIZED else "",
        )
    )
    return GameState(
        game="NC FORGE MISSION",
        observation=observation(session=session, seq=seq, at=at),
        sequence=seq,
        timestamp=at,
        hud=tuple(hud),
        text=tuple({"text": line, "line": index} for index, line in enumerate(screen)),
        profile=(
            ProfileMatch(
                profile=mission_profile(),
                reason_code="profile_matched",
                reason="the fixture title matched",
            )
            if matched
            else ProfileMatch(reason_code="no_profile_for_title", reason="no title matched")
        ),
    )


def objective_named(status: ObjectiveStatus, result: Any) -> Any:
    """The one current objective of a reading, asserting there is exactly one."""
    current = [item for item in result.objectives if item.status is not ObjectiveStatus.SUPERSEDED]
    if status is not None:
        matches = [item for item in current if item.status is status]
        if not matches:  # pragma: no cover - clearer failure than an IndexError
            raise AssertionError(f"no {status} objective among {[i.status for i in current]}")
        return matches[0]
    if len(current) != 1:  # pragma: no cover - a fixture that stopped being one objective
        raise AssertionError(f"expected one objective, got {[i.status for i in current]}")
    return current[0]


class ObjectiveModelTests(unittest.TestCase):
    """Invalid values are refused when the record is built, not discovered later."""

    def test_an_objective_refuses_an_unknown_status_and_a_superseded_without_a_successor(
        self,
    ) -> None:
        base = {
            "objective_id": "o1",
            "description": "Collect the tokens",
            "session_id": "s1",
        }
        self.assertEqual(UnderstoodObjective(**base).status, ObjectiveStatus.DISCOVERED)
        with self.assertRaises(ValueError):
            UnderstoodObjective(**base, status="believed")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            UnderstoodObjective(**base, status=ObjectiveStatus.SUPERSEDED)
        with self.assertRaises(ValueError):
            UnderstoodObjective(**{**base, "session_id": ""})
        with self.assertRaises(ValueError):
            UnderstoodObjective(**{**base, "objective_id": "  "})
        with self.assertRaises(ValueError):
            UnderstoodObjective(**base, revision=0)
        with self.assertRaises(ValueError):
            UnderstoodObjective(**base, confidence=1.5)
        with self.assertRaises(ValueError):
            UnderstoodObjective(**{**base, "description": "x" * (MAX_DESCRIPTION + 1)})
        # Confidence is a real absence, never an invented midpoint.
        self.assertIsNone(UnderstoodObjective(**base).confidence)

    def test_progress_refuses_an_impossible_fraction_and_a_negative_reset(self) -> None:
        with self.assertRaises(ValueError):
            ProgressRecord(kind=ProgressKind.NUMERIC, fraction=1.2)
        with self.assertRaises(ValueError):
            ProgressRecord(kind=ProgressKind.NUMERIC, resets=-1)
        with self.assertRaises(ValueError):
            ProgressRecord(kind="plenty")  # type: ignore[arg-type]
        record = ProgressRecord(kind=ProgressKind.STAGE, text="stage 2")
        self.assertFalse(record.numeric)
        self.assertIsNone(record.value)

    def test_a_fraction_exists_only_when_all_three_figures_are_known(self) -> None:
        self.assertEqual(progress_fraction(2.0, 0.0, 4.0), 0.5)
        self.assertIsNone(progress_fraction(None, 0.0, 4.0))
        self.assertIsNone(progress_fraction(2.0, None, 4.0))
        self.assertIsNone(progress_fraction(2.0, 0.0, None))
        # A target at or behind the start has no span to divide by: no percentage.
        self.assertIsNone(progress_fraction(2.0, 2.0, 2.0))
        self.assertIsNone(progress_fraction(5.0, 0.0, 4.0))

    def test_a_dependency_is_declared_only(self) -> None:
        self.assertEqual(ObjectiveDependency(requires="a").source, "declared")
        with self.assertRaises(ValueError):
            ObjectiveDependency(requires="a", source="inferred")
        with self.assertRaises(ValueError):
            ObjectiveDependency(requires="")

    def test_a_profile_reports_missing_self_and_cyclic_dependencies(self) -> None:
        def rule(rule_id: str, *depends: str) -> ObjectiveRule:
            return ObjectiveRule(
                rule_id=rule_id,
                description=rule_id,
                objective_field="objective",
                dependencies=tuple(depends),
            )

        def profile(*rules: ObjectiveRule) -> ObjectiveProfile:
            return ObjectiveProfile(
                profile_id="p@1",
                game="g",
                version="1",
                provenance="test",
                rules=tuple(rules),
            )

        self.assertEqual(profile(rule("a"), rule("b", "a")).problems(), [])
        self.assertEqual(
            len(profile(rule("a", "b")).problems()), 1, profile(rule("a", "b")).problems()
        )
        self.assertIn("does not declare", profile(rule("a", "b")).problems()[0])
        self.assertIn("depends on itself", profile(rule("a", "a")).problems()[0])
        self.assertIn("cycle", profile(rule("a", "b"), rule("b", "a")).problems()[0])
        self.assertIn("share a rule_id", profile(rule("a"), rule("a")).problems()[0])
        self.assertIn(
            "neither an objective field nor a counter field",
            str(_raises(ValueError, lambda: ObjectiveRule(rule_id="x", description="x"))),
        )

    def test_a_broken_profile_is_a_rejection_not_a_crash(self) -> None:
        registry = ObjectiveProfileRegistry()
        self.assertTrue(registry.register({"profile_id": "broken"}))
        self.assertEqual(registry.ids, ())
        reason = registry.register(
            ObjectiveProfile(
                profile_id="p@1",
                game="g",
                version="1",
                provenance="test",
                rules=(ObjectiveRule(rule_id="a", description="a", objective_field="objective", dependencies=("b",)),),
            )
        )
        self.assertIn("does not declare", reason)
        rows = registry.rows()
        self.assertEqual({row["profile_id"] for row in rows}, {"broken", "p@1"})
        self.assertTrue(all(row["available"] is False for row in rows if row["profile_id"] != "nc-forge-mission@1"))

    def test_a_duplicate_profile_id_is_refused_and_the_first_is_kept(self) -> None:
        registry = default_objective_registry()
        first = registry.get("nc-forge-mission@1")
        self.assertIn(
            "already registered",
            registry.register(
                ObjectiveProfile(
                    profile_id="nc-forge-mission@1",
                    game="g",
                    version="1",
                    provenance="test",
                )
            ),
        )
        self.assertIs(registry.get("nc-forge-mission@1"), first)

    def test_the_shipped_registry_declares_its_limitations(self) -> None:
        registry = default_objective_registry()
        profile = registry.get("nc-forge-mission@1")
        self.assertIsNotNone(profile)
        self.assertIn("fixture", profile.provenance)
        self.assertTrue(any("fixture" in item for item in profile.limitations))
        self.assertEqual(registry.rejected, {})


def _raises(exc_type: type[BaseException], call: Any) -> BaseException:
    try:
        call()
    except exc_type as exc:
        return exc
    except Exception as exc:  # pragma: no cover - wrong exception type
        raise AssertionError(f"raised {type(exc).__name__}, not {exc_type.__name__}") from exc
    raise AssertionError("nothing was raised")  # pragma: no cover


class DiscoveryTests(unittest.TestCase):
    """What counts as an objective, and what never does."""

    def test_a_profile_objective_is_read_from_its_declared_field(self) -> None:
        result = interpret(mission_state(), now=_fixed())
        objective = objective_named(None, result)
        self.assertEqual(objective.description, "Collect the audit tokens")
        self.assertEqual(objective.category, ObjectiveCategory.MISSION)
        self.assertTrue(objective.explicit)
        self.assertEqual(objective.source, "nc-forge-mission@1")
        self.assertEqual(result.profile_id, "nc-forge-mission@1")
        self.assertEqual(objective.session_id, "s1")
        self.assertEqual(objective.revision, 1)
        self.assertIsNone(objective.confidence)
        self.assertEqual(
            [item.kind.value for item in objective.evidence][0], "objective_text"
        )
        self.assertEqual(objective.evidence[0].observation_id, "s1:0001")
        self.assertEqual(objective.evidence[0].at, MEASURED_AT)

    def test_generic_text_that_does_not_name_itself_is_not_an_objective(self) -> None:
        state = mission_state(
            matched=False,
            objective=None,
            counter=None,
            screen=("Press E to interact", "You are carrying 3 tokens", "Disclaimer: mission statements"),
        )
        result = interpret(state, now=_fixed())
        self.assertEqual(result.objectives, ())
        self.assertTrue(any("named itself" in reason for reason in result.reasons))
        self.assertTrue(any("No perception profile matched" in reason for reason in result.reasons))

    def test_multiple_self_naming_lines_become_separate_objectives(self) -> None:
        state = mission_state(
            matched=False,
            objective=None,
            counter=None,
            screen=("Objective: Reach the roof", "Mission: Escort the convoy"),
        )
        result = interpret(state, now=_fixed())
        self.assertEqual(len(result.objectives), 2)
        self.assertEqual(
            {item.description for item in result.objectives},
            {"Reach the roof", "Escort the convoy"},
        )
        self.assertTrue(all(item.explicit for item in result.objectives))
        self.assertTrue(all(item.category is ObjectiveCategory.MISSION for item in result.objectives))
        self.assertEqual(
            len({item.objective_id for item in result.objectives}), 2, "ids must be distinct"
        )

    def test_an_unreadable_objective_region_is_unknown_not_invented(self) -> None:
        result = interpret(
            mission_state(
                objective_status=HudStatus.UNREADABLE,
                objective_reason="contrast too low",
            ),
            now=_fixed(),
        )
        objective = objective_named(ObjectiveStatus.UNKNOWN, result)
        self.assertIn("could not be read", objective.reason)
        self.assertIn("contrast too low", objective.reason)
        self.assertFalse(objective.explicit)

    def test_an_empty_objective_region_is_not_a_task(self) -> None:
        result = interpret(
            mission_state(objective_status=HudStatus.ABSENT, counter=2.0), now=_fixed()
        )
        # A declared, empty objective region holds no objective, whatever the
        # counter says: a moving counter is progress evidence, not a task.
        self.assertEqual(result.objectives, ())
        self.assertTrue(any("nothing was read" in reason for reason in result.reasons))


class ProgressTests(unittest.TestCase):
    """Progress is measured where a counter exists and never invented elsewhere."""

    def test_counter_progress_carries_a_start_and_a_fraction(self) -> None:
        first = interpret(mission_state(seq=1, counter=1.0), now=_fixed())
        objective = objective_named(ObjectiveStatus.IN_PROGRESS, first)
        self.assertEqual(objective.progress.kind, ProgressKind.NUMERIC)
        self.assertEqual(objective.progress.value, 1.0)
        self.assertEqual(objective.progress.target, 3.0)
        self.assertEqual(objective.progress.start, 1.0)
        self.assertEqual(objective.progress.fraction, 0.0)

        second = interpret(mission_state(seq=2, counter=2.0), previous=first, now=_fixed())
        moved = objective_named(ObjectiveStatus.IN_PROGRESS, second)
        self.assertEqual(moved.progress.value, 2.0)
        self.assertEqual(moved.progress.start, 1.0, "the start must not drift with the value")
        self.assertEqual(moved.progress.fraction, 0.5)
        self.assertEqual(moved.first_seen, objective.first_seen)
        progress_events = [item for item in second.events if item.kind is ObjectiveEventKind.PROGRESS]
        self.assertEqual(len(progress_events), 1)
        self.assertIn("moved from 1 to 2", progress_events[0].reason)

    def test_a_repeated_counter_is_not_a_new_progress_step(self) -> None:
        first = interpret(mission_state(seq=1, counter=2.0), now=_fixed())
        again = interpret(mission_state(seq=2, counter=2.0), previous=first, now=_fixed())
        self.assertEqual([item for item in again.events if item.kind is ObjectiveEventKind.PROGRESS], [])
        self.assertIn("has not changed", objective_named(ObjectiveStatus.IN_PROGRESS, again).reason)

    def test_a_decreasing_counter_is_reported_as_a_reset(self) -> None:
        start = interpret(mission_state(seq=1, counter=2.0), now=_fixed())
        dropped = interpret(mission_state(seq=2, counter=1.0), previous=start, now=_fixed())
        objective = objective_named(ObjectiveStatus.IN_PROGRESS, dropped)
        self.assertEqual(objective.progress.resets, 1)
        event = [item for item in dropped.events if item.kind is ObjectiveEventKind.PROGRESS][0]
        self.assertIn("decreased", event.reason)
        self.assertIn("reset", event.reason)

    def test_an_unreadable_counter_claims_no_progress(self) -> None:
        result = interpret(
            mission_state(counter_status=HudStatus.AMBIGUOUS, counter_reason="two values read"),
            now=_fixed(),
        )
        objective = objective_named(None, result)
        self.assertIsNone(objective.progress)
        self.assertIn("no progress is claimed", objective.reason)
        self.assertIn("two values read", objective.reason)
        self.assertNotEqual(objective.status, ObjectiveStatus.COMPLETED)

    def test_an_absent_counter_is_a_missing_measurement_not_a_zero(self) -> None:
        result = interpret(
            mission_state(counter_status=HudStatus.ABSENT), now=_fixed()
        )
        objective = objective_named(None, result)
        self.assertIsNone(objective.progress)
        self.assertEqual(objective.status, ObjectiveStatus.DISCOVERED)


class OutcomeTests(unittest.TestCase):
    """Only evidence that says so closes an objective."""

    def test_a_counter_at_its_target_completes_with_that_evidence(self) -> None:
        first = interpret(mission_state(seq=1, counter=2.0), now=_fixed())
        done = interpret(mission_state(seq=2, counter=3.0), previous=first, now=_fixed())
        objective = objective_named(ObjectiveStatus.COMPLETED, done)
        self.assertEqual(objective.progress.fraction, 1.0)
        kinds = {item.kind.value for item in objective.evidence}
        self.assertIn("counter_target", kinds)
        self.assertIn("3 of 3", objective.reason)
        completed = [item for item in done.events if item.kind is ObjectiveEventKind.COMPLETED]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].before, "in_progress")

    def test_completion_text_completes_with_its_evidence(self) -> None:
        result = interpret(
            mission_state(notification="MISSION COMPLETE", notification_status=HudStatus.RECOGNIZED),
            now=_fixed(),
        )
        objective = objective_named(ObjectiveStatus.COMPLETED, result)
        self.assertIn("mission complete", objective.reason.casefold())
        self.assertEqual(
            [item.kind.value for item in objective.evidence].count("completion_text"), 1
        )

    def test_failure_text_fails_the_objective(self) -> None:
        result = interpret(
            mission_state(notification="Mission failed", notification_status=HudStatus.RECOGNIZED),
            now=_fixed(),
        )
        objective = objective_named(ObjectiveStatus.FAILED, result)
        self.assertIn("failed", objective.reason.casefold())
        self.assertEqual(
            [item.kind for item in result.events], [ObjectiveEventKind.FAILED]
        )

    def test_conflicting_completion_and_failure_claims_no_verdict(self) -> None:
        result = interpret(
            mission_state(
                notification="Mission complete / Mission failed",
                notification_status=HudStatus.RECOGNIZED,
            ),
            now=_fixed(),
        )
        objective = objective_named(None, result)
        self.assertNotIn(objective.status, {ObjectiveStatus.COMPLETED, ObjectiveStatus.FAILED})
        self.assertIn("conflict", objective.reason)
        self.assertEqual(len(result.diagnostics.conflicts), 1)
        self.assertIn("no verdict", result.diagnostics.conflicts[0])

    def test_a_vanished_objective_is_not_verifiable_not_completed(self) -> None:
        first = interpret(mission_state(seq=1, counter=2.0), now=_fixed())
        gone = interpret(mission_state(seq=2, objective_status=HudStatus.ABSENT), previous=first, now=_fixed())
        objective = objective_named(ObjectiveStatus.NOT_VERIFIABLE, gone)
        self.assertIn("that is not completion", objective.reason)
        self.assertNotEqual(objective.status, ObjectiveStatus.COMPLETED)
        self.assertEqual(
            [item.kind for item in gone.events], [ObjectiveEventKind.UNVERIFIABLE]
        )

    def test_a_terminal_verdict_is_corrected_when_the_objective_reappears(self) -> None:
        done = interpret(
            mission_state(seq=1, notification="Mission complete", notification_status=HudStatus.RECOGNIZED),
            now=_fixed(),
        )
        objective_named(ObjectiveStatus.COMPLETED, done)
        back = interpret(mission_state(seq=2, counter=2.0), previous=done, now=_fixed())
        corrected = objective_named(ObjectiveStatus.ACTIVE, back)
        self.assertIn("corrected from completed", corrected.reason)
        self.assertEqual([item.kind for item in back.events], [ObjectiveEventKind.CORRECTED])

    def test_a_terminal_verdict_is_kept_when_nothing_contradicts_it(self) -> None:
        done = interpret(
            mission_state(seq=1, notification="Mission complete", notification_status=HudStatus.RECOGNIZED),
            now=_fixed(),
        )
        later = interpret(
            mission_state(
                seq=2,
                notification="Mission complete",
                notification_status=HudStatus.RECOGNIZED,
                counter=3.0,
            ),
            previous=done,
            now=_fixed(),
        )
        objective = objective_named(ObjectiveStatus.COMPLETED, later)
        self.assertEqual(objective.revision, 1)
        self.assertEqual([item.kind for item in later.events], [])

    def test_a_changed_objective_in_the_same_slot_supersedes_the_old_one(self) -> None:
        first = interpret(mission_state(seq=1, objective="Collect the audit tokens"), now=_fixed())
        old = objective_named(None, first)
        changed = interpret(
            mission_state(seq=2, objective="Escort the convoy"), previous=first, now=_fixed()
        )
        current = objective_named(None, changed)
        self.assertEqual(current.description, "Escort the convoy")
        superseded = [item for item in changed.objectives if item.status is ObjectiveStatus.SUPERSEDED]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0].objective_id, old.objective_id)
        self.assertEqual(superseded[0].superseded_by, current.objective_id)
        self.assertEqual(
            [item.kind for item in changed.events],
            [ObjectiveEventKind.DISCOVERED, ObjectiveEventKind.SUPERSEDED],
        )

    def test_a_session_change_supersedes_rather_than_merges(self) -> None:
        first = interpret(mission_state(session="s1", seq=1), now=_fixed())
        second = interpret(mission_state(session="s2", seq=1), previous=first, now=_fixed())
        self.assertEqual(second.session_id, "s2")
        self.assertTrue(all(item.session_id == "s2" for item in second.objectives))
        superseded = [item for item in second.events if item.kind is ObjectiveEventKind.SUPERSEDED]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0].session_id, "s1")
        self.assertIn("session changed", superseded[0].reason)

    def test_an_objective_with_no_declared_condition_says_it_cannot_be_verified(self) -> None:
        result = interpret(
            mission_state(matched=False, objective=None, counter=None, screen=("Objective: Reach the roof",)),
            now=_fixed(),
        )
        self.assertTrue(any("cannot be verified" in reason for reason in result.reasons))


class DependencyTests(unittest.TestCase):
    """A prerequisite is represented only where a rule declares one."""

    def _profile(self) -> ObjectiveProfile:
        return ObjectiveProfile(
            profile_id="nc-forge-mission@1",
            game="NC FORGE MISSION (fixture scenario)",
            version="1",
            provenance="test",
            rules=(
                ObjectiveRule(
                    rule_id="prereq",
                    description="Open the gate",
                    objective_field="objective",
                    completion_texts=("mission complete",),
                ),
                ObjectiveRule(
                    rule_id="main",
                    description="Escort the convoy",
                    objective_field="notification",
                    dependencies=("prereq",),
                ),
            ),
        )

    def _registry(self) -> ObjectiveProfileRegistry:
        return ObjectiveProfileRegistry((self._profile(),))

    def test_a_declared_dependency_blocks_until_it_is_completed(self) -> None:
        registry = self._registry()
        blocked = interpret(
            mission_state(notification="Escort", notification_status=HudStatus.RECOGNIZED, counter=None),
            registry=registry,
            now=_fixed(),
        )
        main = next(item for item in blocked.objectives if "main" in item.objective_id)
        self.assertEqual(main.status, ObjectiveStatus.BLOCKED)
        self.assertEqual(main.dependencies[0].requires, "prereq")
        self.assertFalse(main.dependencies[0].satisfied)
        self.assertIn(ObjectiveEventKind.BLOCKED, [item.kind for item in blocked.events])

        lifted = interpret(
            mission_state(
                seq=2,
                objective="Mission complete",
                notification="Escort",
                notification_status=HudStatus.RECOGNIZED,
                counter=None,
            ),
            previous=blocked,
            registry=registry,
            now=_fixed(),
        )
        prereq = next(item for item in lifted.objectives if "prereq" in item.objective_id)
        self.assertEqual(prereq.status, ObjectiveStatus.COMPLETED)
        unblocked = next(item for item in lifted.objectives if "main" in item.objective_id)
        self.assertEqual(unblocked.status, ObjectiveStatus.ACTIVE)
        self.assertTrue(unblocked.dependencies[0].satisfied)
        self.assertIn(
            ObjectiveEventKind.ACTIVATED, [item.kind for item in lifted.events]
        )

    def test_a_dependency_that_produces_no_objective_is_reported(self) -> None:
        registry = self._registry()
        result = interpret(
            mission_state(
                objective=None,
                notification="Escort",
                notification_status=HudStatus.RECOGNIZED,
                counter=None,
            ),
            registry=registry,
            now=_fixed(),
        )
        self.assertEqual(len(result.diagnostics.dependency_issues), 1)
        self.assertIn("produced no objective", result.diagnostics.dependency_issues[0])


class ObjectiveServiceTests(unittest.IsolatedAsyncioTestCase):
    """The service decides WHICH states may update the picture, and nothing else."""

    def _service(self, **kwargs: Any) -> ObjectiveService:
        return ObjectiveService(clock=_fixed, **kwargs)

    async def test_a_state_is_interpreted_once_and_a_duplicate_is_not_reapplied(self) -> None:
        service = self._service()
        state = mission_state(seq=1, counter=2.0)
        first = await service.observe(state)
        self.assertIsNotNone(first)
        self.assertEqual(first.objectives[0].status, ObjectiveStatus.IN_PROGRESS)
        events_after_first = len(service.status()["events"])

        again = await service.observe(mission_state(seq=1, counter=2.0))
        self.assertIs(again, first, "a duplicate must return the same picture")
        status = service.status()
        self.assertEqual(status["counts"]["duplicates"], 1)
        self.assertEqual(status["counts"]["received"], 1)
        self.assertEqual(len(status["events"]), events_after_first)
        self.assertEqual(status["state"]["objectives"][0]["progress"]["value"], 2.0)

    async def test_an_out_of_order_state_is_ignored_with_its_reason(self) -> None:
        service = self._service()
        await service.observe(mission_state(seq=2, counter=2.0))
        await service.observe(
            mission_state(seq=1, counter=1.0)
        )
        status = service.status()
        self.assertEqual(status["counts"]["ignored"], 1)
        self.assertEqual(status["counts"]["received"], 1)
        self.assertEqual(status["last_error"]["reason_code"], "observation_out_of_order")
        self.assertEqual(status["state"]["objectives"][0]["progress"]["value"], 2.0)

    async def test_a_state_without_identity_is_refused(self) -> None:
        service = self._service()
        state = mission_state()
        state = GameState(
            game=state.game,
            observation=ObservationRef(observation_id="o1", session_id=""),
            hud=state.hud,
            profile=state.profile,
        )
        self.assertIsNone(await service.observe(state))
        self.assertEqual(service.status()["last_error"]["reason_code"], "invalid_observation")
        self.assertIsNone(service.status()["state"])

    async def test_an_object_that_is_not_a_state_is_a_recorded_refusal(self) -> None:
        service = self._service()
        self.assertIsNone(await service.observe(object()))  # type: ignore[arg-type]
        status = service.status()
        self.assertEqual(status["counts"]["failed"], 1)
        self.assertEqual(status["last_error"]["reason_code"], "invalid_observation")
        self.assertIsNone(status["state"])

    async def test_a_broken_interpretation_is_recorded_not_raised(self) -> None:
        service = self._service()
        with mock.patch(
            "novacontrol.novaforge.objective_service.interpret",
            side_effect=RuntimeError("interpreter exploded"),
        ):
            self.assertIsNone(await service.observe(mission_state()))
        status = service.status()
        self.assertEqual(status["counts"]["failed"], 1)
        self.assertIn("interpreter exploded", status["last_error"]["reason"])
        self.assertEqual(status["last_error"]["reason_code"], "interpretation_failed")

    async def test_a_change_is_published_once_without_raw_screen_text(self) -> None:
        published: list[tuple[str, dict[str, Any]]] = []

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            published.append((event_type, payload))

        service = self._service(publish=publish)
        await service.observe(mission_state(seq=1, counter=2.0))
        self.assertEqual(len(published), 1)
        event_type, payload = published[0]
        self.assertEqual(event_type, "novaforge.objectives.updated")
        self.assertEqual(payload["observation_id"], "s1:0001")
        self.assertEqual(payload["session_id"], "s1")
        self.assertNotIn("text", payload)
        self.assertEqual(payload["objectives"][0]["progress"], 2.0)

    async def test_an_unrelated_reading_publishes_nothing(self) -> None:
        published: list[str] = []

        async def publish(event_type: str, _payload: dict[str, Any]) -> None:
            published.append(event_type)

        service = self._service(publish=publish)
        await service.observe(mission_state(counter=2.0))
        await service.observe(mission_state(seq=2, counter=2.0))
        self.assertEqual(published, ["novaforge.objectives.updated"])

    async def test_a_raising_publisher_cannot_fail_the_interpretation(self) -> None:
        async def publish(_event_type: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("bus down")

        service = self._service(publish=publish)
        result = await service.observe(mission_state(counter=2.0))
        self.assertIsNotNone(result)
        self.assertTrue(service.status()["tested"])

    async def test_shutdown_stops_accepting_states(self) -> None:
        service = self._service()
        await service.observe(mission_state(counter=2.0))
        await service.shutdown()
        self.assertIsNone(await service.observe(mission_state(seq=2, counter=3.0)))
        status = service.status()
        self.assertTrue(status["stopped"])
        self.assertEqual(status["state"]["objectives"][0]["progress"]["value"], 2.0)

    async def test_history_is_bounded_and_reset_keeps_it(self) -> None:
        service = self._service(max_history=2)
        for seq, value in ((1, 1.0), (2, 2.0), (3, 3.0)):
            await service.observe(mission_state(seq=seq, counter=value))
        status = service.status()
        self.assertEqual(len(status["history"]), 2)
        self.assertEqual(status["counts"]["history"], 2)
        self.assertIn("max", status["measured"]["interpretation_ms"])

        reset = service.reset()
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["history_kept"], 2)
        after = service.status()
        self.assertIsNone(after["state"])
        self.assertEqual(len(after["events"]), len(status["events"]))

    async def test_elapsed_interpretation_time_is_measured(self) -> None:
        service = self._service()
        await service.observe(mission_state(counter=2.0))
        measured = service.status()["measured"]["interpretation_ms"]
        self.assertIsInstance(measured["last"], float)
        self.assertGreaterEqual(measured["last"], 0.0)
        self.assertIsNone(ObjectiveService().status()["measured"]["interpretation_ms"]["last"])

    def test_the_status_publishes_no_confidence_it_cannot_measure(self) -> None:
        limitations = ObjectiveService().status()["limitations"]
        self.assertTrue(any("No confidence" in item for item in limitations))
        self.assertTrue(any("quest log" in item for item in limitations))


class DictStore:
    """The two-method store protocol, in memory, owned by the test."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def read(self, namespace: str) -> dict[str, Any]:
        return dict(self.data.get(namespace) or {})

    def write(self, namespace: str, payload: dict[str, Any]) -> None:
        self.data[namespace] = dict(payload)


class ObjectivePersistenceTests(unittest.IsolatedAsyncioTestCase):
    """What the service writes is what a restart reads back — and only that."""

    async def test_history_events_and_counts_survive_a_restart(self) -> None:
        store = DictStore()
        first = ObjectiveService(store=store, clock=_fixed)
        await first.observe(mission_state(seq=1, counter=2.0))
        await first.observe(mission_state(seq=1, counter=2.0))  # a duplicate
        self.assertEqual(first.status()["counts"]["duplicates"], 1)

        restarted = ObjectiveService(store=store, clock=_fixed)
        status = restarted.status()
        self.assertTrue(status["restored"])
        # The current picture is deliberately NOT restored: a stale objective
        # must never look current after a restart.
        self.assertFalse(status["tested"])
        self.assertIsNone(status["state"])
        self.assertEqual(status["counts"]["received"], 1)
        self.assertEqual(status["counts"]["duplicates"], 1)
        self.assertEqual(len(status["history"]), 1)
        self.assertEqual(len(status["events"]), 1)
        self.assertEqual(status["history"][0]["objectives"][0]["status"], "in_progress")

    async def test_a_corrupt_store_is_empty_not_a_crash(self) -> None:
        class Broken:
            def read(self, _namespace: str) -> Any:
                raise OSError("unreadable")

            def write(self, _namespace: str, _payload: Any) -> None:
                raise OSError("unwritable")

        service = ObjectiveService(store=Broken(), clock=_fixed)
        status = service.status()
        self.assertFalse(status["restored"])
        self.assertIsNone(status["state"])
        # A broken store must not stop a reading either.
        self.assertIsNotNone(await service.observe(mission_state(counter=2.0)))


class StateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """NF-17 consumes NF-16 states through the one hook, and cannot break them."""

    async def _analyze(self, on_state: Any) -> Any:
        from test_novaforge_state import FakeObserver, TICK_TEXT, fake_result, scene

        with tempfile.TemporaryDirectory() as folder:
            frame = Path(folder) / "frame.png"
            frame.write_bytes(b"\x89PNG\r\n\x1a\nfake")

            async def perceive(_frame: Any) -> Any:
                return fake_result(scene(text=TICK_TEXT))

            async def publish(_event_type: str, _payload: dict[str, Any]) -> None:
                return None

            service = GameStateService(
                observer=FakeObserver(frame=frame),
                perceive=perceive,
                publish=publish,
                on_state=on_state,
            )
            return await service.analyze()

    async def test_a_successful_reading_reaches_the_objective_service(self) -> None:
        seen: list[GameState] = []
        result = await self._analyze(seen.append)
        self.assertTrue(result.analyzed)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].observation.observation_id, "obs-1:0001")

    async def test_a_raising_consumer_does_not_fail_the_reading(self) -> None:
        def explode(_state: GameState) -> None:
            raise RuntimeError("objective consumer bug")

        result = await self._analyze(explode)
        self.assertTrue(result.analyzed)
        self.assertFalse(result.reason_code)

    async def test_an_async_consumer_is_awaited(self) -> None:
        seen: list[str] = []

        async def consumer(state: GameState) -> None:
            await asyncio.sleep(0)
            seen.append(state.observation.observation_id)

        result = await self._analyze(consumer)
        self.assertTrue(result.analyzed)
        self.assertEqual(seen, ["obs-1:0001"])


class ObjectiveApiTests(_IsolatedApiTestCase):
    """The HTTP surface, with the objective service holding a deterministic state."""

    def _isolated_application(self, **kwargs: Any) -> Any:
        app = super()._isolated_application(**kwargs)
        app.novaforge.objective_state = ObjectiveService(clock=_fixed)
        return app

    def test_status_starts_untested_and_measures_nothing(self) -> None:
        body = self._client.get("/novaforge/objectives").json()
        self.assertTrue(body["available"])
        self.assertFalse(body["tested"])
        self.assertIsNone(body["state"])
        self.assertEqual(body["counts"]["received"], 0)
        self.assertIsNone(body["measured"]["interpretation_ms"]["last"])
        self.assertIsNone(body["last_error"])

    def test_the_profiles_route_lists_the_fixture_rules_and_rejections(self) -> None:
        body = self._client.get("/novaforge/objectives/profiles").json()
        ids = {row["profile_id"] for row in body["profiles"]}
        self.assertIn("nc-forge-mission@1", ids)
        self.assertIn("nc-forge-mission@1", body["defaults"])
        self.assertEqual(body["rejected"], {})

    def test_reset_reports_what_it_kept(self) -> None:
        body = self._client.post("/novaforge/objectives/reset", json={}).json()
        self.assertTrue(body["reset"])
        self.assertEqual(body["events_kept"], 0)
        self.assertIsNone(self._client.get("/novaforge/objectives").json()["state"])

    def test_the_served_page_contains_the_objectives_card(self) -> None:
        response = self._client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="forgeObjectivesList"', response.text)
        self.assertIn('id="forgeObjectivesReadButton"', response.text)
        self.assertIn("no-store", response.headers.get("cache-control", ""))

    def test_the_routes_exist_in_the_declared_surface(self) -> None:
        from novacontrol.api.models import ApiSurface

        paths = {(route.method, route.path) for route in ApiSurface.default().routes}
        for method, path in [
            ("GET", "/novaforge/objectives"),
            ("POST", "/novaforge/objectives/reset"),
            ("GET", "/novaforge/objectives/profiles"),
        ]:
            self.assertIn((method, path), paths)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ObjectivePanelLogicTests(unittest.TestCase):
    """The panel's objective rows are pure functions, executed in Node with no DOM."""

    NEEDLE = """
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const progress = { kind: 'numeric', text: '2', value: 2, target: 3, start: 1, fraction: 0.5,
  resets: 1, reason: 'read from the declared counter', at: '2026-10-25T09:59:00+00:00' };
const objective = { description: 'Collect the audit tokens', status: 'in_progress',
  category: 'mission', explicit: true, progress: progress, source_text: 'Collect the audit tokens',
  source: 'nc-forge-mission@1', last_updated: '2026-10-25T10:00:00+00:00',
  dependencies: [{ requires: 'open_gate', satisfied: false }],
  reason: 'counter moved from 1 to 2',
  evidence: [{ kind: 'objective_text', summary: 'objective text' },
             { kind: 'progress', summary: 'counter read as 2' }] };
const out = {
  progress: [L.objectiveProgressText(progress), L.objectiveProgressText(null),
    L.objectiveProgressText({ kind: 'none', reason: 'the counter was not read' }),
    L.objectiveProgressText({ kind: 'stage', text: 'stage 2' })],
  row: L.objectiveRow(objective),
  inferredRow: L.objectiveRow(Object.assign({}, objective, { explicit: false, progress: null })),
  statuses: L.objectiveStatusSummary([{ status: 'in_progress' }, { status: 'completed' },
    { status: 'in_progress' }, { status: 'not_verifiable' }]),
  emptyStatuses: L.objectiveStatusSummary([]),
  hints: {
    untested: L.objectiveHint(null),
    error: L.objectiveHint({ state: null, last_error: { reason_code: 'invalid_observation', reason: 'no identity' } }),
    conflict: L.objectiveHint({ state: { objectives: [objective], events: [],
      diagnostics: { conflicts: ['o1: completion and failure conflict'], unattributed: [], dependency_issues: [] },
      reasons: [] } }),
    none: L.objectiveHint({ state: { objectives: [], events: [],
      diagnostics: { conflicts: [], unattributed: [], dependency_issues: [] },
      reasons: ['No line named itself as an objective'] } }),
    current: L.objectiveHint({ state: { objectives: [objective, Object.assign({}, objective,
      { status: 'superseded' })], events: [],
      diagnostics: { conflicts: [], unattributed: [], dependency_issues: [] }, reasons: [] } }),
  },
  facts: L.objectiveFacts({ state: { game: 'NC FORGE MISSION', session_id: 's1',
      observation_id: 's1:0001', at: new Date().toISOString(),
      profile_id: 'nc-forge-mission@1', events: [], diagnostics: {},
      objectives: [objective, Object.assign({}, objective, { status: 'superseded' })] },
    counts: { received: 2, ignored: 1, duplicates: 3 }, measured: { interpretation_ms: { last: 4.5, average: 3.0 } },
    restored: true, last_error: null }),
  noFacts: L.objectiveFacts(null),
  event: L.objectiveEventText({ kind: 'completed', before: 'in_progress', after: 'completed',
    objective_id: 'o1', reason: 'counter reached 3 of 3' }),
};
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(cls.NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(Path("src/novacontrol/web/static/js/novaforge.js"))],
            capture_output=True,
            timeout=60,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_a_progress_sentence_never_invents_a_number(self) -> None:
        progress = self.data["progress"]
        self.assertIn("2 / 3", progress[0])
        self.assertIn("50%", progress[0])
        self.assertIn("1 reset", progress[0])
        self.assertEqual(progress[1], "no progress evidence")
        self.assertIn("the counter was not read", progress[2])
        self.assertIn("stage 2", progress[3])

    def test_an_objective_row_states_status_progress_and_evidence(self) -> None:
        row = self.data["row"]
        self.assertIn("Collect the audit tokens", row)
        self.assertIn("in_progress", row)
        self.assertIn("explicit", row)
        self.assertIn("counter read as 2", row)
        self.assertIn("counter moved from 1 to 2", row)
        self.assertIn("source nc-forge-mission@1", row)
        self.assertIn("requires open_gate (unmet)", row)
        self.assertIn("progress last read", row)
        self.assertIn("inferred", self.data["inferredRow"])
        self.assertNotIn("explicit", self.data["inferredRow"])

    def test_statuses_are_counted_and_none_says_none(self) -> None:
        self.assertEqual(
            self.data["statuses"], "2 in_progress · 1 completed · 1 not_verifiable"
        )
        self.assertEqual(self.data["emptyStatuses"], "none")

    def test_the_hint_names_what_kept_a_reading_from_a_verdict(self) -> None:
        hints = self.data["hints"]
        self.assertIn("No objective has been interpreted", hints["untested"])
        self.assertIn("invalid_observation", hints["error"])
        self.assertIn("no identity", hints["error"])
        self.assertIn("completion and failure conflict", hints["conflict"])
        self.assertIn("No line named itself", hints["none"])
        self.assertIn("1 objective(s)", hints["current"])
        self.assertIn("completion needs explicit evidence", hints["current"])

    def test_facts_separate_a_measured_zero_from_never_measured(self) -> None:
        rows = {row[0]: row[1] for row in self.data["facts"]}
        self.assertEqual(rows["Game"], "NC FORGE MISSION")
        self.assertEqual(rows["Session"], "s1")
        self.assertIn("s1:0001", rows["Observation"])
        self.assertEqual(rows["Profile"], "nc-forge-mission@1")
        # The counts describe the CURRENT set; a superseded record is its own row.
        self.assertEqual(rows["Objectives"], "1")
        self.assertEqual(rows["Statuses"], "1 in_progress")
        self.assertEqual(rows["Superseded"], "1 kept in history")
        self.assertIn("2 read", rows["Readings"])
        self.assertIn("4.5 ms last", rows["Interpretation"])
        self.assertIn("history from an earlier run", rows["Restored"])
        self.assertEqual(rows["Last error"], "none")
        self.assertEqual({row[0]: row[1] for row in self.data["noFacts"]}["Restored"], "no")
        never = {row[0]: row[1] for row in self.data["noFacts"]}
        self.assertEqual(never["Session"], "none")
        self.assertEqual(never["Interpretation"], "not measured yet")

    def test_an_event_row_names_both_statuses_and_the_reason(self) -> None:
        text = self.data["event"]
        self.assertIn("completed", text)
        self.assertIn("in_progress → completed", text)
        self.assertIn("counter reached 3 of 3", text)


class ObjectivePanelStaticTests(unittest.TestCase):
    """The panel ships the controls and the honesty sentence the reading relies on."""

    def test_the_objectives_card_is_declared_with_its_control_ids(self) -> None:
        html = Path("src/novacontrol/web/static/index.html").read_text(encoding="utf-8")
        for marker in (
            'id="forgeObjectivesBadge"',
            'id="forgeObjectivesReadButton"',
            'id="forgeObjectivesRefreshButton"',
            'id="forgeObjectivesResetButton"',
            'id="forgeObjectivesHint"',
            'id="forgeObjectivesFacts"',
            'id="forgeObjectivesList"',
            'id="forgeObjectivesDiagnostics"',
            'id="forgeObjectivesHistory"',
        ):
            self.assertIn(marker, html)
        self.assertIn("A vanished objective is not completion", html)

    def test_the_panel_reads_objectives_through_the_real_routes(self) -> None:
        source = Path("src/novacontrol/web/static/js/novaforge.js").read_text(encoding="utf-8")
        self.assertIn('"/novaforge/objectives"', source)
        self.assertIn('"/novaforge/objectives/reset"', source)
        # Interpretation happens inside the state reading, so the card refreshes
        # through the same analyze call rather than a second capture path.
        self.assertIn("analyzeGameState()", source)

    def test_a_state_reading_repaints_the_objective_card(self) -> None:
        # Both cards describe the same reading; a state Analyze that left the
        # objective card on the previous frame would be two answers to one
        # question.
        source = Path("src/novacontrol/web/static/js/novaforge.js").read_text(encoding="utf-8")
        analyze = source.split("async function analyzeGameState", 1)[1].split(
            "async function resetGameState", 1
        )[0]
        self.assertIn("await loadGameState();", analyze)
        self.assertIn("await loadObjectives();", analyze)


def _fixed() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
