"""Phase 25 — embodied sessions and controlled environments: contracts and workflows.

Two halves, both required.

The first half exercises the embodied contracts directly: what an objective must
say to be verifiable at all, where an observation's facts come from and how much
they may be leaned on, what the owned grid-world adapter really answers, how a
tactical choice is made from a reading, what the ONE input door refuses, and what
the session registry does across a restart. These tests pin the RULES — a
condition nobody can observe is refused before anything runs, an unlabeled region
never becomes a named object, a stale reading never authorizes an action, and a
refused input is reported as a rejection rather than as a malfunction.

The second half drives the REAL application over HTTP (``create_app()`` against an
isolated data directory), which is the interface a user or a CLI reaches:
``POST /embodied/sessions`` to create, ``POST /embodied/sessions/{id}/run`` to
drive the loop, ``GET /embodied/sessions/{id}`` to read the record back. That is
the only thing that proves the routes, the application methods, the Phase 24 goal
owner, the world model's fresh-state gate, the input door and the persistence
agree with each other — an internal call could pass while the route 422s.

Every workflow that claims COMPLETED also proves the success condition was
OBSERVED holding; every ending that is not a completion says which budget or
which environment did it and is asserted NOT to be completion.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:  # pragma: no cover - import guard mirrors the rest of the suite
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
except Exception:  # pragma: no cover - httpx/fastapi missing
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    NovaControlApplication = None  # type: ignore[assignment]

from novacontrol.browser import NoopBrowserRunner
from novacontrol.core.config import EmbodiedSettings
from novacontrol.core.events import EVENT_PAYLOAD_FIELDS, EventType
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.embodied import (
    DEFERRED_PHASES,
    EVENT_TYPES,
    LIFECYCLE,
    overview,
)
from novacontrol.embodied.capabilities import (
    CAPABILITY_TABLE,
    capability_rows,
    register_embodied_capabilities,
)
from novacontrol.embodied.environment import EnvironmentRefused, EnvironmentRegistry
from novacontrol.embodied.manager import (
    ENVIRONMENT_INPUT,
    ENVIRONMENT_SCOPE,
    HARD_MAX_ACTIONS,
    HARD_MAX_SESSIONS,
    STORE_KEY,
    EmbodiedManager,
    EmbodiedSettingsView,
)
from novacontrol.embodied.models import (
    ActionStatus,
    EmbodiedActionResult,
    EnvironmentAction,
    EnvironmentStatus,
    Evaluation,
    Objective,
    Observation,
    ObservationBasis,
    ObservationFreshness,
    SessionStatus,
    validate_objective,
)
from novacontrol.embodied.perception import (
    effect_observed,
    facts_from_observation,
    same_value,
)
from novacontrol.embodied.simulation import (
    FAULT_NAMES,
    Faults,
    adapter_for,
    faults_for,
    scenarios,
)
from novacontrol.embodied.tactical import (
    TacticalMemory,
    choose,
    read_collected,
    read_position,
    read_target,
    tasks_for,
)
from novacontrol.intelligence.intent import CapabilityAvailability, CapabilityRegistry
from novacontrol.perception.models import CapabilityState

# --------------------------------------------------------------------------- #
# Small doubles
# --------------------------------------------------------------------------- #


class _MemoryStore:
    """The two methods the embodied manager uses from the JSON state store."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.fail_writes = False

    def read(self, name: str) -> dict[str, Any]:
        payload = self.values.get(name)
        return dict(payload) if isinstance(payload, dict) else {}

    def write(self, name: str, payload: dict[str, Any]) -> None:
        if self.fail_writes:
            raise OSError("the store is unavailable")
        self.values[name] = dict(payload)


class _Step:
    """A planned step, structurally — the door reads four attributes of it."""

    def __init__(self, **parameters: Any) -> None:
        self.id = "step-environment-input"
        self.parameters = dict(parameters)
        self.effect = "local_write"
        self.description = "send one environment input"


def _objective(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request": "collect the target",
        "success_conditions": ["env.target.collected=true"],
        "max_actions": 20,
    }
    body.update(overrides)
    return body


def _manager(**overrides: Any) -> EmbodiedManager:
    """A manager with NO planner wired and no interval between inputs."""
    settings = EmbodiedSettingsView(action_interval_ms=0, **overrides)
    return EmbodiedManager(settings=settings)


def _all_blocked() -> TacticalMemory:
    """A memory that knows every way out of a cell is blocked."""
    memory = TacticalMemory()
    for direction in ("east", "north", "south", "west"):
        memory.remember_blocked((0, 1), direction)
    return memory


def _observation(**structured: Any) -> Observation:
    return Observation(
        session_id="sess-test",
        environment_id="gridworld",
        basis=ObservationBasis.STRUCTURED,
        freshness=ObservationFreshness.FRESH,
        structured=dict(structured) or {"agent.x": 0, "agent.y": 0, "target.collected": False},
    )


# --------------------------------------------------------------------------- #
# The contracts
# --------------------------------------------------------------------------- #


class ObjectiveContractTests(unittest.TestCase):
    """An objective that cannot be verified is refused before anything runs."""

    def test_an_objective_without_a_request_is_refused(self) -> None:
        valid, why = validate_objective(Objective(success_conditions=("env.x=1",)))
        self.assertFalse(valid)
        self.assertIn("request", why)

    def test_an_objective_without_success_conditions_is_refused(self) -> None:
        valid, why = validate_objective(Objective(request="get better at the game"))
        self.assertFalse(valid)
        self.assertIn("OBSERVED", why, "the refusal says what would make it verifiable")

    def test_a_condition_that_is_not_a_key_value_check_is_refused(self) -> None:
        valid, why = validate_objective(Objective(request="win", success_conditions=("did well",)))
        self.assertFalse(valid)
        self.assertIn("key=value", why)

    def test_a_condition_missing_its_value_is_refused(self) -> None:
        valid, _ = validate_objective(Objective(request="win", success_conditions=("env.x=",)))
        self.assertFalse(valid)

    def test_a_measurable_objective_passes_and_round_trips(self) -> None:
        objective = Objective.from_mapping(
            _objective(constraints=("no destructive input",), max_seconds=30.5)
        )
        valid, why = validate_objective(objective)
        self.assertTrue(valid, why)
        restored = Objective.from_mapping(objective.to_dict())
        self.assertEqual(restored.success_conditions, objective.success_conditions)
        self.assertEqual(restored.request, objective.request)
        self.assertEqual(restored.constraints, objective.constraints)
        self.assertAlmostEqual(restored.max_seconds, 30.5)

    def test_the_terminal_statuses_are_exactly_the_ending_ones(self) -> None:
        endings = {
            SessionStatus.COMPLETED,
            SessionStatus.BUDGET_EXHAUSTED,
            SessionStatus.ENVIRONMENT_FAILED,
            SessionStatus.AGENT_ERROR,
            SessionStatus.DENIED,
            SessionStatus.STUCK,
            SessionStatus.CANCELLED,
        }
        for status in SessionStatus:
            with self.subTest(status=status):
                from novacontrol.embodied.models import SessionState

                state = SessionState(status=status)
                self.assertEqual(state.terminal, status in endings)
        # INTERRUPTED is cancelled BY A RESTART: not terminal (it can be resumed)
        # but not resumable until it has been revalidated either.
        from novacontrol.embodied.models import SessionState

        interrupted = SessionState(status=SessionStatus.INTERRUPTED)
        self.assertFalse(interrupted.terminal)


class EvaluationHonestyTests(unittest.TestCase):
    """A figure nothing measured is None, never a zero."""

    def test_rates_are_none_before_anything_was_measured(self) -> None:
        evaluation = Evaluation()
        self.assertIsNone(evaluation.objective_completion_rate)
        self.assertIsNone(evaluation.actions_per_success)
        self.assertIsNone(evaluation.recovery_success_rate)
        self.assertIsNone(evaluation.seconds)
        self.assertIsNone(evaluation.progress)

    def test_a_terminal_evaluation_reports_a_rate_not_an_absence(self) -> None:
        met = Evaluation(terminal="completed", verified_completion=True, actions_attempted=4)
        self.assertEqual(met.objective_completion_rate, 1.0)
        self.assertEqual(met.actions_per_success, 4.0)
        unmet = Evaluation(terminal="budget_exhausted", actions_attempted=2)
        self.assertEqual(unmet.objective_completion_rate, 0.0)
        self.assertIsNone(unmet.actions_per_success, "nothing succeeded, so there is no cost")


class ObservationProvenanceTests(unittest.TestCase):
    """Only a fresh structured reading may authorize an action."""

    def test_only_a_fresh_reading_is_usable_for_a_decision(self) -> None:
        fresh = _observation()
        self.assertTrue(fresh.usable_for_decision)
        for freshness in (ObservationFreshness.STALE, ObservationFreshness.FAILED):
            with self.subTest(freshness=freshness):
                stale = Observation(basis=ObservationBasis.STRUCTURED, freshness=freshness)
                self.assertFalse(
                    stale.usable_for_decision,
                    "a reading of the past must not authorize a state-dependent action",
                )

    def test_structured_state_becomes_observed_facts(self) -> None:
        facts = facts_from_observation(_observation(**{"agent.x": 2, "target.collected": True}))
        self.assertEqual(facts.observed["env.agent.x"], 2)
        self.assertEqual(facts.observed["env.target.collected"], True)
        self.assertNotIn("env.agent.x", facts.uncertain)

    def test_an_unlabeled_region_is_uncertain_and_never_named(self) -> None:
        from novacontrol.embodied.models import EntityRead

        observation = Observation(
            basis=ObservationBasis.VISUAL,
            freshness=ObservationFreshness.FRESH,
            entities=(
                EntityRead(
                    entity_id="region-1",
                    label="",
                    kind="region",
                    position=(1.0, 2.0),
                    confidence=0.4,
                    basis=ObservationBasis.VISUAL,
                ),
            ),
        )
        facts = facts_from_observation(observation)
        self.assertEqual(facts.observed, {}, "a region is not the environment's own state")
        self.assertEqual(
            facts.uncertain["env.region.region-1.present"],
            True,
            "the region IS reported, just never as a measurement",
        )
        self.assertEqual(facts.uncertain["env.region.region-1.x"], 1.0)
        self.assertNotIn(
            "env.region.region-1.confidence",
            facts.uncertain,
            "a name-less region carries no confidence claim either",
        )
        self.assertEqual(facts.claimed_labels, ())
        self.assertTrue(
            any("visual reading" in note for note in facts.limitations),
            "the limitation is stated rather than left for a reader to infer",
        )

    def test_a_visual_reading_naming_an_enemy_is_kept_unconfirmed(self) -> None:
        from novacontrol.embodied.models import EntityRead

        observation = Observation(
            basis=ObservationBasis.VISUAL,
            freshness=ObservationFreshness.FRESH,
            entities=(
                EntityRead(
                    entity_id="region-9",
                    label="enemy",
                    kind="object",
                    position=(0.0, 0.0),
                    confidence=0.2,
                    basis=ObservationBasis.VISUAL,
                ),
            ),
        )
        facts = facts_from_observation(observation)
        self.assertEqual(facts.claimed_labels, ("enemy",))
        self.assertNotIn(
            "env.enemy.present",
            facts.observed,
            "nothing here may authorize an action on a guess about what a pixel is",
        )
        self.assertTrue(facts.uncertain)
        self.assertTrue(
            any("below the 0.50 bar" in note for note in facts.limitations),
            "a low-confidence claim says so",
        )

    def test_a_stale_reading_says_so(self) -> None:
        stale = Observation(
            basis=ObservationBasis.STRUCTURED,
            freshness=ObservationFreshness.STALE,
            structured={"agent.x": 0},
        )
        facts = facts_from_observation(stale)
        self.assertTrue(any("stale" in note for note in facts.limitations))


class EffectVerificationTests(unittest.TestCase):
    """`True`, `False` and `None` are three different answers, and None is never a pass."""

    def test_an_action_that_declared_nothing_cannot_be_verified(self) -> None:
        verified, why = effect_observed((), _observation())
        self.assertIsNone(verified)
        self.assertIn("no observable effect", why)

    def test_no_observation_means_the_check_could_not_be_made(self) -> None:
        verified, why = effect_observed(("env.agent.x=1",), None)
        self.assertIsNone(verified)
        self.assertIn("no observation", why)

    def test_a_fresh_structured_reading_confirms_the_declared_fact(self) -> None:
        verified, why = effect_observed(
            ("env.agent.x=1", "env.agent.y=1"), _observation(**{"agent.x": 1, "agent.y": 1})
        )
        self.assertEqual(verified, True)
        self.assertIn("env.agent.x=1", why)

    def test_a_different_value_is_a_made_check_that_failed(self) -> None:
        verified, why = effect_observed(("env.agent.x=1",), _observation(**{"agent.x": 0}))
        self.assertEqual(verified, False)
        self.assertIn("expected", why)

    def test_a_structured_reading_that_omits_the_fact_is_a_failure(self) -> None:
        # The environment CAN report this fact and did not: the check was made.
        verified, why = effect_observed(("env.tick=9",), _observation(**{"agent.x": 0}))
        self.assertEqual(verified, False)
        self.assertIn("not reported", why)

    def test_a_reading_that_cannot_report_the_fact_is_unknown_not_a_pass(self) -> None:
        from novacontrol.embodied.models import EntityRead

        visual = Observation(
            basis=ObservationBasis.VISUAL,
            freshness=ObservationFreshness.FRESH,
            entities=(EntityRead(label="region", kind="region", basis=ObservationBasis.VISUAL),),
        )
        verified, why = effect_observed(("env.agent.x=1",), visual)
        self.assertIsNone(verified, "nothing could report it, so nothing may claim it")
        self.assertIn("not reported", why)

    def test_the_prefixed_and_bare_spellings_of_a_fact_are_the_same_fact(self) -> None:
        self.assertTrue(same_value(1, "1"))
        self.assertTrue(same_value(True, "true"))
        self.assertFalse(same_value(False, "true"))
        verified, _ = effect_observed(("env.agent.x=1",), _observation(**{"env.agent.x": 1}))
        self.assertEqual(verified, True)


class EnvironmentRegistryTests(unittest.TestCase):
    """An environment nobody allowed is refused BY NAME, not by accident."""

    def test_an_allowlisted_environment_is_usable_and_an_unknown_one_is_not(self) -> None:
        registry = EnvironmentRegistry(allowed=("gridworld",))
        registry.register(adapter_for("open-east"))
        self.assertTrue(registry.is_allowed("gridworld"))
        self.assertIsNotNone(registry.get("gridworld"))
        self.assertIsNone(registry.get("minecraft"))

    def test_requiring_a_disallowed_environment_raises_rather_than_returning_none(self) -> None:
        registry = EnvironmentRegistry(allowed=())
        registry.register(adapter_for("open-east"))
        with self.assertRaises(EnvironmentRefused):
            registry.require("gridworld")

    def test_a_scenario_is_a_configuration_of_an_allowed_family(self) -> None:
        registry = EnvironmentRegistry(allowed=("gridworld",))
        registry.register(adapter_for("open-east"))
        self.assertTrue(registry.is_allowed("gridworld:detour"))
        self.assertEqual(registry.family_of("gridworld:detour"), "gridworld")

    def test_unregistering_removes_it_from_the_allowlist_view(self) -> None:
        registry = EnvironmentRegistry(allowed=("gridworld",))
        registry.register(adapter_for("open-east"))
        self.assertTrue(registry.unregister("gridworld"))
        self.assertIsNone(registry.get("gridworld"))


class SimulationTests(unittest.TestCase):
    """The owned grid world is data, and it misbehaves only where it says it will."""

    def test_every_named_scenario_is_complete_data(self) -> None:
        table = scenarios()
        for name, config in table.items():
            with self.subTest(scenario=name):
                self.assertIn("width", config)
                self.assertIn("height", config)
                self.assertIn("start", config)
                self.assertIn("target", config)
        self.assertIn("detour", table)
        self.assertIn("fogged", table)

    def test_a_faulted_scenario_names_its_own_faults(self) -> None:
        table = scenarios()
        self.assertEqual(faults_for(table["drifting"]).drift, 1)
        self.assertEqual(faults_for(table["refusing"]).reject, 2)
        self.assertEqual(faults_for(table["frozen"]).freeze, 1)
        self.assertEqual(faults_for(table["timing-out"]).timeout, 1)
        for name in ("open-east", "detour", "fogged"):
            with self.subTest(scenario=name):
                self.assertEqual(
                    faults_for(table[name]).to_dict(),
                    Faults().to_dict(),
                    "a clean scenario must not silently misbehave",
                )

    def test_explicit_faults_add_to_a_scenario_never_take_away_from_it(self) -> None:
        merged = faults_for(scenarios()["drifting"], Faults(reject=1))
        self.assertEqual(merged.drift, 1, "the scenario's own fault stays")
        self.assertEqual(merged.reject, 1, "the caller asked for MORE trouble")
        self.assertEqual(faults_for({"faults": {}}).to_dict(), Faults().to_dict())
        for name in FAULT_NAMES:
            self.assertIn(name, Faults().to_dict())

    def test_connecting_and_observing_reports_the_environments_own_state(self) -> None:
        adapter = adapter_for("open-east")
        report = adapter.connect()
        self.assertTrue(report.connected)
        self.assertFalse(report.mock)
        self.assertEqual(adapter.status(), EnvironmentStatus.OPERATIONAL)
        observation = adapter.observe(session_id="sess-test")
        self.assertEqual(observation.basis, ObservationBasis.STRUCTURED)
        self.assertEqual(observation.freshness, ObservationFreshness.FRESH)
        self.assertEqual(observation.structured["agent.x"], 0)
        self.assertEqual(observation.structured["target.collected"], False)
        adapter.shutdown()

    def test_a_move_is_bounded_and_only_named_directions_are_accepted(self) -> None:
        adapter = adapter_for("open-east")
        adapter.connect()
        ok = adapter.send_input(
            EnvironmentAction(session_id="s", input_name="move", parameters={"direction": "east"})
        )
        self.assertEqual(ok.status, ActionStatus.COMPLETED)
        self.assertEqual(ok.observed.structured["agent.x"], 1)
        bad = adapter.send_input(
            EnvironmentAction(
                session_id="s", input_name="move", parameters={"direction": "sideways"}
            )
        )
        self.assertEqual(
            bad.status, ActionStatus.REJECTED, "an unknown direction is refused, not guessed"
        )
        self.assertIsNone(bad.observed, "a refusal reports no reading of its own")
        self.assertEqual(adapter.observe(session_id="s").structured["agent.x"], 1, "nothing moved")
        adapter.shutdown()

    def test_the_reported_duration_is_measured_not_echoed(self) -> None:
        # The result's duration is a MEASUREMENT of what happened, never the
        # number the caller handed in: echoing the request back would make the
        # report a copy of the ask (and the pin would pass with or without a
        # clamp — which is how this test read before). The bound on a REQUEST
        # lives at the manager's single input door, pinned beside it.
        adapter = adapter_for("open-east")
        adapter.connect()
        result = adapter.send_input(
            EnvironmentAction(
                session_id="s",
                input_name="move",
                parameters={"direction": "east"},
                duration_ms=10_000_000,
            )
        )
        self.assertEqual(result.status, ActionStatus.COMPLETED)
        self.assertLess(result.duration_ms, 1000, "the reported duration must be the elapsed time")
        self.assertNotEqual(
            result.duration_ms, 10_000_000, "the requested duration is not echoed as a measurement"
        )
        adapter.shutdown()

    def test_a_drifting_world_lands_somewhere_it_was_not_told_to(self) -> None:
        adapter = adapter_for("drifting")
        adapter.connect()
        result = adapter.send_input(
            EnvironmentAction(session_id="s", input_name="move", parameters={"direction": "east"})
        )
        self.assertEqual(result.status, ActionStatus.COMPLETED)
        self.assertNotEqual(
            (result.observed.structured["agent.x"], result.observed.structured["agent.y"]),
            (1, 0),
            "the point of the drift fault is that the declared effect is not what happened",
        )
        adapter.shutdown()

    def test_a_frozen_world_answers_stale_rather_than_lying(self) -> None:
        adapter = adapter_for("frozen")
        adapter.connect()
        first = adapter.observe(session_id="s")
        self.assertEqual(first.freshness, ObservationFreshness.STALE)
        self.assertEqual(
            first.basis, ObservationBasis.UNKNOWN, "it does not dress it up as a reading"
        )
        self.assertEqual(first.structured, {}, "it reports no state it cannot see")
        self.assertIn("freeze", first.reason)
        self.assertFalse(first.usable_for_decision)
        self.assertEqual(adapter.observe(session_id="s").freshness, ObservationFreshness.STALE)
        adapter.shutdown()

    def test_a_fogged_world_reports_regions_and_no_identity(self) -> None:
        adapter = adapter_for("fogged")
        adapter.connect()
        observation = adapter.observe(session_id="s")
        self.assertEqual(observation.structured, {}, "no state was claimed")
        self.assertTrue(observation.entities, "the regions it can see are reported")
        self.assertTrue(
            all(not entity.named for entity in observation.entities),
            "nothing may be named from pixels alone",
        )
        adapter.shutdown()

    def test_releasing_is_always_legal_and_reports_what_it_did(self) -> None:
        adapter = adapter_for("open-east")
        adapter.connect()
        report = adapter.release_inputs()
        self.assertTrue(report.released)
        adapter.shutdown()

    def test_an_unknown_scenario_is_a_named_error(self) -> None:
        with self.assertRaises(KeyError):
            adapter_for("no-such-scenario")


class TacticalChoiceTests(unittest.TestCase):
    """The choice is derived from the reading, and a blocked way is remembered."""

    def _reading(self, **structured: Any) -> Observation:
        base = {"agent.x": 0, "agent.y": 1, "target.x": 3, "target.y": 1, "target.collected": False}
        base.update(structured)
        return _observation(**base)

    def test_the_agent_moves_toward_the_target_it_was_told_about(self) -> None:
        choice = choose(self._reading())
        self.assertEqual(choice.kind, "move")
        self.assertEqual(choice.direction, "east")
        self.assertIn("distance", choice.reason)

    def test_standing_on_the_target_collects_it(self) -> None:
        choice = choose(self._reading(**{"agent.x": 3}), want="collect")
        self.assertEqual(choice.kind, "interact")
        self.assertEqual(choice.target, (3, 1))

    def test_a_collected_target_is_not_collected_twice(self) -> None:
        choice = choose(self._reading(**{"agent.x": 3, "target.collected": True}), want="collect")
        self.assertEqual(choice.kind, "none")
        self.assertIn("already collected", choice.reason)

    def test_a_remembered_blockage_changes_the_way_around(self) -> None:
        memory = TacticalMemory()
        memory.remember_blocked((0, 1), "east")
        choice = choose(self._reading(), memory=memory)
        self.assertEqual(choice.kind, "move")
        self.assertNotEqual(choice.direction, "east")
        self.assertEqual(choice.recovery_from, "east")

    def test_every_way_being_blocked_is_its_own_answer(self) -> None:
        memory = TacticalMemory()
        for direction in ("east", "north", "south", "west"):
            memory.remember_blocked((0, 1), direction)
        choice = choose(self._reading(), memory=memory)
        self.assertEqual(choice.kind, "none")
        self.assertIn("stuck", choice.reason)

    def test_a_reading_without_structured_state_cannot_be_acted_on(self) -> None:
        reading = Observation(basis=ObservationBasis.VISUAL, freshness=ObservationFreshness.FRESH)
        choice = choose(reading)
        self.assertEqual(choice.kind, "none")
        self.assertIn("cannot say where", choice.reason)
        self.assertIsNone(read_position(reading))

    def test_a_stale_reading_cannot_be_acted_on_either(self) -> None:
        reading = Observation(basis=ObservationBasis.UNKNOWN, freshness=ObservationFreshness.STALE)
        choice = choose(reading)
        self.assertEqual(choice.kind, "none")
        self.assertIn("stale", choice.reason)

    def test_readers_never_invent_a_value(self) -> None:
        reading = self._reading()
        self.assertEqual(read_position(reading), (0, 1))
        self.assertEqual(read_target(reading), (3, 1))
        self.assertEqual(read_collected(reading), False)
        empty = Observation(basis=ObservationBasis.STRUCTURED, freshness=ObservationFreshness.FRESH)
        self.assertIsNone(read_position(empty), "a reading that does not say is not a guess")
        self.assertIsNone(read_target(empty))
        self.assertEqual(
            read_collected(empty),
            False,
            "an unreported condition is never 'done' — the missing fact fails safe",
        )

    def test_the_move_task_declares_what_success_looks_like(self) -> None:
        choice = choose(self._reading())
        tasks = tasks_for(
            choice, session_id="s", environment_id="gridworld", position=choice.position
        )
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.action, ENVIRONMENT_INPUT)
        self.assertEqual(task.parameters["input"], "move")
        self.assertEqual(task.parameters["session_id"], "s")
        self.assertEqual(task.parameters["direction"], "east")
        self.assertIn("env.agent.x=1", str(task.parameters["expected"]))
        self.assertTrue(task.verification, "a step nobody can verify is a bug")
        self.assertTrue(task.preconditions, "the precondition is the fact the environment reported")

    def test_the_collect_task_is_idempotent_and_verifiable(self) -> None:
        choice = choose(self._reading(**{"agent.x": 3}), want="collect")
        tasks = tasks_for(
            choice, session_id="s", environment_id="gridworld", position=choice.position
        )
        task = tasks[0]
        self.assertEqual(task.parameters["input"], "interact")
        self.assertIn("Idempotent", task.description)
        self.assertEqual(task.parameters["expected"], "env.target.collected=true")

    def test_no_choice_produces_no_task(self) -> None:
        choice = choose(self._reading(), memory=_all_blocked())
        self.assertEqual(tasks_for(choice, session_id="s", environment_id="gridworld"), [])


class InputDoorTests(unittest.TestCase):
    """Every input passes through ONE door, and the door refuses in named ways."""

    def test_no_planner_means_the_session_refuses_rather_than_looping_privately(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        result = asyncio.run(manager.run_session(state.session_id))
        self.assertEqual(result.status, SessionStatus.AGENT_ERROR)
        self.assertIn("no Phase 24 goal planner is wired", result.reason)
        self.assertEqual(result.actions_taken, 0, "nothing was sent anywhere")

    def test_an_unknown_session_is_refused_before_anything_is_sent(self) -> None:
        manager = _manager()
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(manager.execute_step(_Step(session_id="nope", input="move")))
        self.assertIn("does not exist", str(caught.exception))

    def test_a_step_naming_no_input_is_refused(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(manager.execute_step(_Step(session_id=state.session_id)))
        self.assertIn("no environment input", str(caught.exception))

    def test_a_paused_session_sends_nothing(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        manager.pause(state.session_id)
        with self.assertRaises(PermissionError) as caught:
            asyncio.run(
                manager.execute_step(
                    _Step(session_id=state.session_id, input="move", direction="east")
                )
            )
        self.assertIn("paused", str(caught.exception))

    def test_a_cancelled_session_sends_nothing(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        manager.cancel(state.session_id)
        with self.assertRaises(PermissionError) as caught:
            asyncio.run(
                manager.execute_step(
                    _Step(session_id=state.session_id, input="move", direction="east")
                )
            )
        self.assertIn("cancelled", str(caught.exception))

    def test_a_session_that_was_never_revalidated_sends_nothing(self) -> None:
        store = _MemoryStore()
        first = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        state = first.create_session(objective=_objective())
        second = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        second.restore()
        restored = second.get(state.session_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertTrue(restored.needs_revalidation)
        with self.assertRaises(PermissionError) as caught:
            asyncio.run(
                second.execute_step(
                    _Step(session_id=state.session_id, input="move", direction="east")
                )
            )
        self.assertIn("revalidated", str(caught.exception))

    def test_an_environment_that_is_not_operational_is_refused(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        adapter = manager.registry().require(state.environment_id)
        adapter.shutdown()
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(
                manager.execute_step(
                    _Step(session_id=state.session_id, input="move", direction="east")
                )
            )
        self.assertIn("not operational", str(caught.exception))

    def test_a_rejected_input_is_raised_and_never_silently_accepted(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        adapter = manager.registry().require(state.environment_id)
        adapter.connect()
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(
                manager.execute_step(
                    _Step(session_id=state.session_id, input="move", direction="sideways")
                )
            )
        self.assertIn("rejected", str(caught.exception))
        self.assertEqual(manager.get(state.session_id).actions_taken, 1, "the attempt is recorded")

    def test_the_duration_handed_to_the_environment_is_clamped(self) -> None:
        # The ceiling is enforced where the input LEAVES: an objective (or a
        # step) asking for a ten-hour input is capped at the configured ceiling,
        # and the session record keeps what was actually sent.
        manager = _manager(max_input_ms=250)
        state = manager.create_session(objective=_objective())
        adapter = manager.registry().require(state.environment_id)
        adapter.connect()
        asyncio.run(
            manager.execute_step(
                _Step(
                    session_id=state.session_id,
                    input="move",
                    direction="east",
                    duration_ms=10_000_000,
                )
            )
        )
        sent = manager.get(state.session_id).last_action
        self.assertIsNotNone(sent, "the input that was sent is recorded on the session")
        assert sent is not None
        self.assertLessEqual(
            sent.duration_ms, 250, "the ceiling the session's settings declared is enforced"
        )
        self.assertEqual(manager.settings.max_input_ms, 250)

    def test_every_sent_input_lands_on_the_shared_audit_trail_exactly_once(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        adapter = manager.registry().require(state.environment_id)
        adapter.connect()
        asyncio.run(
            manager.execute_step(_Step(session_id=state.session_id, input="move", direction="east"))
        )
        asyncio.run(
            manager.execute_step(
                _Step(session_id=state.session_id, input="move", direction="south")
            )
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(
                manager.execute_step(
                    _Step(session_id=state.session_id, input="move", direction="sideways")
                )
            )
        entries = asyncio.run(manager.audit_log.read())
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            [entry.status for entry in entries], ["completed", "completed", "rejected"]
        )
        self.assertEqual(entries[0].output["input"], "move")
        self.assertEqual(entries[0].output["session_id"], state.session_id)
        self.assertEqual(
            entries[0].output["risk"],
            "",
            "no permission layer is wired here, and no risk is invented for it",
        )
        self.assertEqual(entries[0].output["released"], True, "the input was released")
        self.assertTrue(
            all(isinstance(entry.action_id, str) and entry.action_id for entry in entries),
            "each row names the action it is about",
        )


class ManagerRuleTests(unittest.TestCase):
    """The registry is the authority on a session, and it persists."""

    def test_an_unmeasurable_objective_never_becomes_a_session(self) -> None:
        manager = _manager()
        with self.assertRaises(ValueError):
            manager.create_session(objective={"request": "get better"})
        self.assertEqual(manager.sessions(), ())

    def test_an_unknown_environment_is_a_named_refusal(self) -> None:
        manager = _manager()
        with self.assertRaises(ValueError) as caught:
            manager.create_session(objective=_objective(), environment_id="minecraft")
        self.assertIn("minecraft", str(caught.exception))

    def test_an_environment_that_was_not_allowed_is_refused_by_name(self) -> None:
        manager = EmbodiedManager(
            settings=EmbodiedSettingsView(environments=(), action_interval_ms=0),
            register_default_adapters=False,
        )
        manager.register_adapter(adapter_for("open-east"))
        with self.assertRaises(EnvironmentRefused) as caught:
            manager.create_session(objective=_objective())
        self.assertIn("allowlist", str(caught.exception))

    def test_a_scenario_is_its_own_environment_and_the_budget_is_clamped(self) -> None:
        manager = _manager(max_actions=6)
        state = manager.create_session(
            objective=_objective(max_actions=1000, max_seconds=10_000.0), scenario="detour"
        )
        self.assertEqual(state.environment_id, "gridworld:detour")
        self.assertEqual(state.objective.max_actions, 6, "a session cannot raise its own ceiling")
        self.assertLessEqual(state.objective.max_seconds, manager.settings.max_seconds)
        self.assertIsNotNone(manager.environment(state.environment_id))

    def test_the_live_session_ceiling_is_enforced(self) -> None:
        manager = _manager(max_sessions=1)
        manager.create_session(objective=_objective())
        with self.assertRaises(ValueError) as caught:
            manager.create_session(objective=_objective())
        self.assertIn("1 live session", str(caught.exception))

    def test_pausing_resuming_and_cancelling_all_land_in_the_registry(self) -> None:
        # A transition the registry never heard about made a pause cosmetic: the
        # input door and the live-session ceiling both read the registry.
        manager = _manager()
        state = manager.create_session(objective=_objective())
        paused = manager.pause(state.session_id)
        self.assertEqual(paused.status, SessionStatus.PAUSED)
        self.assertEqual(manager.get(state.session_id).status, SessionStatus.PAUSED)
        self.assertEqual(manager.summary()["live"], 1, "paused is not finished")
        resumed = manager.resume(state.session_id)
        self.assertEqual(resumed.status, SessionStatus.CREATED)
        self.assertTrue(resumed.needs_revalidation, "a pause may have outlived the reading")
        cancelled = manager.cancel(state.session_id)
        self.assertEqual(cancelled.status, SessionStatus.CANCELLED)
        self.assertEqual(manager.get(state.session_id).status, SessionStatus.CANCELLED)
        self.assertEqual(manager.summary()["live"], 0, "the slot is free again")

    def test_the_loop_clearing_needs_revalidation_reaches_the_registry(self) -> None:
        # The revalidation the loop performs clears ``needs_revalidation`` in the
        # ONLY place the door reads, or a resumed session could never act again.
        manager = _manager()
        state = manager.create_session(objective=_objective())
        manager.pause(state.session_id)
        resumed = manager.resume(state.session_id)
        self.assertTrue(resumed.needs_revalidation)
        agent = manager._agent(state.session_id)
        revalidated, reason = agent.revalidate()
        self.assertTrue(revalidated, reason)
        self.assertFalse(
            manager.get(state.session_id).needs_revalidation,
            "the door must see the session as revalidated",
        )

    def test_a_second_run_of_a_terminal_session_is_refused_but_the_record_stays(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        manager.cancel(state.session_id)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(manager.run_session(state.session_id))
        self.assertIn("cancelled", str(caught.exception))
        self.assertIsNotNone(manager.get(state.session_id), "cancelling is not deleting")

    def test_running_a_paused_session_is_refused(self) -> None:
        manager = _manager()
        state = manager.create_session(objective=_objective())
        manager.pause(state.session_id)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(manager.run_session(state.session_id))
        self.assertIn("paused", str(caught.exception))

    def test_a_disabled_build_refuses_to_run_at_all(self) -> None:
        manager = _manager(enabled=False)
        state = manager.create_session(objective=_objective())
        with self.assertRaises(ValueError) as caught:
            asyncio.run(manager.run_session(state.session_id))
        self.assertIn("disabled", str(caught.exception))

    def test_restore_marks_an_unfinished_session_interrupted_and_skips_a_finished_one(self) -> None:
        store = _MemoryStore()
        settings = EmbodiedSettingsView(action_interval_ms=0, max_sessions=2)
        first = EmbodiedManager(settings=settings, store=store)
        live = first.create_session(objective=_objective())
        done = first.create_session(objective=_objective())
        first.cancel(done.session_id)
        second = EmbodiedManager(settings=settings, store=store)
        report = second.restore()
        self.assertEqual(report["restored"], 1)
        self.assertEqual(report["interrupted"], [live.session_id])
        self.assertGreaterEqual(report["skipped"], 1, "a finished session is history, not work")
        restored = second.view(live.session_id)
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["needs_revalidation"], True)
        self.assertIsNone(second.get(done.session_id), "a cancelled session is not restored")

    def test_a_session_survives_a_round_trip_with_its_objective_and_trajectory(self) -> None:
        store = _MemoryStore()
        first = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        state = first.create_session(objective=_objective(max_actions=7), scenario="detour")
        first.pause(state.session_id, "operator stepped away")
        second = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        second.restore()
        restored = second.get(state.session_id)
        assert restored is not None
        self.assertEqual(restored.objective.max_actions, 7)
        self.assertEqual(restored.environment_id, "gridworld:detour")
        self.assertEqual(restored.scenario, "detour")
        self.assertEqual(restored.status, SessionStatus.INTERRUPTED)
        self.assertIn("restart", restored.reason)

    def test_a_restored_scenario_session_is_rebuilt_not_lost_with_the_process(self) -> None:
        """A restart keeps the world: a configured session must come back drivable.

        A record stores the CONFIGURED environment (`gridworld:detour`) while a new
        process has only minted the default one, so without rebuilding it a restored
        session could never be revalidated: running it was refused with "environment
        ... is no longer registered", which reports a session as RESTORED that can
        never run. The rebuild is deterministic — the record's own key names the
        scenario and the faults — so the adapter and the agent both exist again.
        (The completion itself is pinned over HTTP in `WorkflowLPersistenceTests`,
        where the Phase 24 planner this manager does not have is wired.)
        """
        store = _MemoryStore()
        settings = EmbodiedSettingsView(action_interval_ms=0)
        first = EmbodiedManager(settings=settings, store=store)
        state = first.create_session(objective=_objective(), scenario="detour")
        second = EmbodiedManager(settings=settings, store=store)
        second.restore()
        self.assertIsNotNone(
            second.environment("gridworld:detour"),
            "the configured environment is rebuilt, not lost with the process",
        )
        self.assertIsNotNone(
            second._agent(state.session_id),
            "the loop is not refused for a world that is actually there",
        )

    def test_a_configuration_this_build_cannot_rebuild_is_reported_not_invented(self) -> None:
        """An unbuildable world is stated on the record, never faked into being.

        The record is readable and cancellable either way, and running it is refused
        with the reason — never silently pointed at some other world.
        """
        store = _MemoryStore()
        first = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        state = first.create_session(objective=_objective(), scenario="detour")
        payload = store.read(STORE_KEY)
        record = payload["sessions"][state.session_id]
        record["environment_id"] = "minecraft:drifting"
        record["scenario"] = "drifting"
        second = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        report = second.restore()
        self.assertEqual(report["restored"], 1, "it is restored as a readable record")
        restored = second.get(state.session_id)
        assert restored is not None
        self.assertIsNone(second.environment("minecraft:drifting"))
        self.assertIn("cannot", restored.reason)
        self.assertEqual(restored.status, SessionStatus.INTERRUPTED)
        with self.assertRaises(ValueError) as caught:
            asyncio.run(second.run_session(state.session_id))
        self.assertIn("no longer registered", str(caught.exception))
        self.assertEqual(
            second.cancel(state.session_id).status,
            SessionStatus.CANCELLED,
            "an operator can still cancel the session they cannot run",
        )

    def test_a_stored_evaluation_is_read_back_not_replaced_with_an_empty_one(self) -> None:
        """What the store writes, restore reads — a write-only field is data loss.

        The evaluation is written on every save and only becomes non-default when
        a session ends, and an ended session is never restored as work — so this
        pins the READ side against a stored record directly, which is the case
        that would silently lose the record's own scoreboard if that policy ever
        changed.
        """
        store = _MemoryStore()
        settings = EmbodiedSettingsView(action_interval_ms=0)
        first = EmbodiedManager(settings=settings, store=store)
        state = first.create_session(objective=_objective(), scenario="detour")
        first.pause(state.session_id, "keep it live across the restart")
        record = store.read(STORE_KEY)["sessions"][state.session_id]
        record["evaluation"] = {
            "session_id": state.session_id,
            "objective_met": False,
            "verified_completion": False,
            "terminal": "",
            "actions_attempted": 5,
            "actions_completed": 4,
            "recoveries": 1,
            "recoveries_succeeded": 1,
            "stale_observations": 2,
            "seconds": 12.5,
            "progress": 3.0,
        }
        second = EmbodiedManager(settings=settings, store=store)
        second.restore()
        restored = second.get(state.session_id)
        assert restored is not None
        evaluation = restored.evaluation
        self.assertEqual(evaluation.actions_attempted, 5)
        self.assertEqual(evaluation.actions_completed, 4)
        self.assertEqual(evaluation.recoveries, 1)
        self.assertEqual(evaluation.recoveries_succeeded, 1)
        self.assertEqual(evaluation.stale_observations, 2)
        self.assertEqual(evaluation.seconds, 12.5)
        self.assertEqual(evaluation.progress, 3.0)
        self.assertEqual(evaluation.actions_rejected, 0, "an absent count is zero, not unknown")
        self.assertIsNone(
            evaluation.observation_freshness_ms,
            "a figure nothing measured stays None rather than becoming 0.0",
        )
        self.assertIsNone(
            evaluation.objective_completion_rate,
            "a rate is derived from the counts, never read back from the store",
        )

    def test_a_store_that_fails_to_write_does_not_break_a_session(self) -> None:
        store = _MemoryStore()
        store.fail_writes = True
        manager = EmbodiedManager(settings=EmbodiedSettingsView(action_interval_ms=0), store=store)
        state = manager.create_session(objective=_objective())
        self.assertIsNotNone(manager.get(state.session_id), "persistence never breaks the work")
        self.assertFalse(manager.save(state.session_id))

    def test_the_status_surface_names_the_scope_the_ceilings_and_the_wiring(self) -> None:
        manager = _manager()
        status = manager.status()
        self.assertEqual(status["scope"], ENVIRONMENT_SCOPE)
        self.assertEqual(status["allowed"], ["gridworld"])
        self.assertIn("open-east", status["scenarios"])
        self.assertEqual(status["limits"]["max_actions"], manager.settings.max_actions)
        self.assertFalse(status["wired"]["planner"], "no planner is wired in this test")
        self.assertIn("audit_log", status["wired"])
        text = json.dumps(status)
        for content_key in ("last_observation", "verdict", "last_action", "last_result"):
            self.assertNotIn(content_key, text, "a status surface publishes no session content")


class CapabilityTableTests(unittest.TestCase):
    """What the phase claims, with the absences as loud as the presences."""

    def test_the_table_registers_on_the_one_registry_with_live_availability(self) -> None:
        manager = _manager()
        registry = CapabilityRegistry()
        registered = register_embodied_capabilities(registry, manager)
        self.assertEqual(registered, len(CAPABILITY_TABLE))
        row = registry.get("embodied.gridworld_environment")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.availability, CapabilityAvailability.AVAILABLE)

    def test_a_capability_that_ships_nowhere_is_reported_unavailable_with_a_reason(self) -> None:
        rows = {row.name: row for row in capability_rows(_manager())}
        for capability_id in (
            "embodied.screen_environment",
            "embodied.batching",
            "embodied.learning_from_sessions",
            "embodied.generalization",
        ):
            with self.subTest(capability_id=capability_id):
                self.assertEqual(rows[capability_id].available, False)
                self.assertTrue(rows[capability_id].reason)
        screen = rows["embodied.screen_environment"]
        self.assertIn("no screen/game adapter ships", screen.reason)

    def test_a_provider_dependent_capability_says_what_is_missing(self) -> None:
        rows = {row.name: row for row in capability_rows(_manager())}
        semantic = rows["embodied.semantic_perception"]
        self.assertEqual(semantic.available, False)
        self.assertIn("regions stay regions", semantic.reason)
        self.assertTrue(semantic.reason, "an unavailability always names its cause")

    def test_every_declared_capability_has_a_shipped_state(self) -> None:
        for definition in CAPABILITY_TABLE:
            with self.subTest(capability_id=definition.capability_id):
                self.assertIsInstance(definition.state, CapabilityState)
                self.assertTrue(definition.description)

    def test_an_unprobeable_requirement_is_unknown_and_a_probeable_one_is_a_plain_no(self) -> None:
        rows = {row.name: row for row in capability_rows(None)}
        # Nothing can be asked without a manager, and "cannot tell" is not "no".
        self.assertIsNone(rows["embodied.semantic_perception"].available)
        self.assertTrue(rows["embodied.semantic_perception"].reason)
        # A capability of the phase itself needs no provider: it is simply absent here.
        self.assertEqual(rows["embodied.gridworld_environment"].available, False)
        self.assertTrue(rows["embodied.gridworld_environment"].reason)


class ConfigTests(unittest.TestCase):
    """A configured ceiling may ask for less and can never ask for more."""

    def test_every_number_is_clamped_to_its_hard_ceiling(self) -> None:
        settings = EmbodiedSettings.from_mapping(
            {
                "max_sessions": HARD_MAX_SESSIONS * 100,
                "max_actions": HARD_MAX_ACTIONS * 100,
                "max_seconds": 10_000_000.0,
                "max_input_ms": 10_000_000,
            }
        )
        self.assertLessEqual(settings.max_sessions, HARD_MAX_SESSIONS)
        self.assertLessEqual(settings.max_actions, HARD_MAX_ACTIONS)
        self.assertLessEqual(settings.max_seconds, 1800.0)
        self.assertLessEqual(settings.max_input_ms, 2000)

    def test_disabling_sessions_and_allowing_nothing_are_both_expressible(self) -> None:
        disabled = EmbodiedSettings.from_mapping({"enabled": False})
        self.assertFalse(disabled.enabled)
        empty = EmbodiedSettings.from_mapping({"environments": []})
        self.assertEqual(empty.environments, (), "an empty allowlist means nothing may be driven")

    def test_the_settings_round_trip_through_a_mapping(self) -> None:
        settings = EmbodiedSettings.from_mapping({"environments": ["gridworld"], "max_replans": 5})
        restored = EmbodiedSettings.from_mapping(settings.to_mapping())
        self.assertEqual(restored.environments, ("gridworld",))
        self.assertEqual(restored.max_replans, 5)

    def test_the_manager_view_clamps_a_handed_in_mapping_too(self) -> None:
        view = EmbodiedSettingsView.from_mapping({"max_actions": HARD_MAX_ACTIONS * 10})
        self.assertLessEqual(view.max_actions, HARD_MAX_ACTIONS)


class OverviewTests(unittest.TestCase):
    """The phase states its own vocabulary, and the events it publishes are real."""

    def test_the_overview_states_the_cycle_and_the_deferrals(self) -> None:
        payload = overview()
        self.assertEqual(payload["phase"], "25")
        cycle = [str(step) for step in payload["cycle"]]  # type: ignore[union-attr]
        for step in ("observe", "select_action_through_planning", "verify_action_and_objective"):
            self.assertIn(step, cycle)
        deferred = [str(item) for item in payload["deferred"]]  # type: ignore[union-attr]
        self.assertEqual(len(deferred), len(DEFERRED_PHASES))
        self.assertTrue(any("Phase 26" in item for item in deferred))
        self.assertTrue(any("simulation only" in item for item in deferred))

    def test_the_lifecycle_tables_cover_every_status_the_code_can_set(self) -> None:
        self.assertEqual(set(LIFECYCLE["session"]), {status.value for status in SessionStatus})
        self.assertEqual(set(LIFECYCLE["action"]), {status.value for status in ActionStatus})
        self.assertEqual(
            set(LIFECYCLE["environment"]), {status.value for status in EnvironmentStatus}
        )

    def test_every_published_event_is_a_real_typed_event_with_a_payload_schema(self) -> None:
        declared = {member.value for member in EventType}
        for kind in EVENT_TYPES:
            with self.subTest(kind=kind):
                self.assertIn(kind, declared, "a published event must be in the vocabulary")
                self.assertIn(
                    EventType(kind),
                    EVENT_PAYLOAD_FIELDS,
                    "an event without a schema is one a handler can silently mis-read",
                )

    def test_the_overview_states_what_this_build_does_NOT_do(self) -> None:
        payload = overview()
        for flag in (
            "real_environment_shipped",
            "fabricates_object_identity",
            "acts_on_stale_state",
            "counts_unverified_as_success",
            "resends_timed_out_inputs",
            "retries_a_refusal",
            "completes_an_objective_without_a_fresh_reading",
            "sends_input_before_revalidation_after_restart",
            "drives_unallowlisted_environments",
            "stores_frames_or_prompts_in_the_trajectory",
        ):
            with self.subTest(flag=flag):
                self.assertEqual(payload[flag], False, f"{flag} must be declared, and false")
        self.assertEqual(payload["environments"], ["gridworld"])
        self.assertIn("phase24", payload["integration"])  # type: ignore[operator]


# --------------------------------------------------------------------------- #
# The workflows — the REAL application, over HTTP
# --------------------------------------------------------------------------- #


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class _EmbodiedWorkflowCase(unittest.TestCase):
    """One real application per test, over a temp data dir, with no device runners."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: Any = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> Any:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    # ── helpers ──────────────────────────────────────────────────────────
    def _create(self, objective: dict[str, Any] | None = None, **body: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"objective": objective or _objective()}
        payload.update(body)
        response = self._client.post("/embodied/sessions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _run(self, session_id: str, **body: Any) -> dict[str, Any]:
        response = self._client.post(f"/embodied/sessions/{session_id}/run", json=body or None)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _view(self, session_id: str) -> dict[str, Any]:
        response = self._client.get(f"/embodied/sessions/{session_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _embodied_events(self) -> list[Any]:
        """Every embodied event the REAL bus recorded, in order."""
        deadline = time.monotonic() + 2.0
        recent = self._nova.event_bus.recent(limit=400)
        while time.monotonic() < deadline:
            time.sleep(0.02)
            current = self._nova.event_bus.recent(limit=400)
            if len(current) == len(recent):
                break
            recent = current
        return [event for event in recent if str(event.type).startswith("embodied.")]


class WorkflowACollectTheTargetTests(_EmbodiedWorkflowCase):
    """A: the whole loop, end to end, and completion is only claimed with proof."""

    def test_a_session_observes_plans_acts_verifies_and_reports_completion(self) -> None:
        created = self._create()
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["actions_taken"], 0, "creating is not starting")

        run = self._run(created["session_id"])
        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertEqual(run["actions_taken"], 4, "three moves and one interact")
        self.assertEqual(run["released"], True, "nothing is left held")

        view = self._view(created["session_id"])
        self.assertEqual(view["verdict"]["met"], True)
        self.assertEqual(view["verdict"]["verified"], True)
        self.assertIn("env.target.collected=true", view["verdict"]["observed"])
        self.assertTrue(view["verdict"]["observation_id"])

        evaluation = view["evaluation"]
        self.assertEqual(evaluation["objective_completion_rate"], 1.0)
        self.assertEqual(evaluation["verified_completion"], True)
        self.assertEqual(evaluation["actions_per_success"], 4.0)
        self.assertGreaterEqual(evaluation["observations"], 1)
        self.assertGreaterEqual(evaluation["observations"], 1)
        self.assertIsNone(evaluation["recovery_success_rate"], "no recovery happened here")

    def test_the_trajectory_shows_the_whole_cycle_and_never_a_frame(self) -> None:
        run = self._run(self._create()["session_id"])
        phases = [entry["phase"] for entry in run["trajectory"]]
        for phase in ("connect", "observe", "decide", "act", "verify"):
            self.assertIn(phase, phases)
        self.assertEqual(phases[0], "connect")
        text = json.dumps(run)
        self.assertNotIn("data:image", text, "no frame body is published")
        self.assertNotIn("chain_of_thought", text)
        # INDEXES are dense and increasing: the trajectory is the session's story.
        indexes = [entry["index"] for entry in run["trajectory"]]
        self.assertEqual(indexes, list(range(len(indexes))))


class WorkflowBRefusalIsNotAMalfunctionTests(_EmbodiedWorkflowCase):
    """B: the environment refusing an input is reported as a refusal."""

    def test_a_refusing_controller_is_worked_around_and_counted_as_a_refusal(self) -> None:
        run = self._run(self._create(scenario="refusing")["session_id"])
        view = self._view(run["session_id"])
        self.assertEqual(view["status"], "completed", view["reason"])
        self.assertGreaterEqual(view["evaluation"]["actions_rejected"], 1)
        self.assertEqual(view["evaluation"]["actions_failed"], 0, "a refusal is not a malfunction")
        self.assertGreaterEqual(view["recoveries"], 1, "the retry path ran")
        statuses = [entry["status"] for entry in view["trajectory"]]
        self.assertIn("recover", statuses)


class WorkflowCDetourTests(_EmbodiedWorkflowCase):
    """C: a blocked way is remembered, and the objective still gets met."""

    def test_a_wall_makes_the_agent_route_around_it_and_recover(self) -> None:
        run = self._run(self._create(scenario="detour")["session_id"])
        self.assertEqual(run["status"], "completed", run["reason"])
        view = self._view(run["session_id"])
        self.assertEqual(view["verdict"]["met"], True)
        self.assertGreaterEqual(view["recoveries"], 1)
        self.assertGreaterEqual(view["evaluation"]["verification_failures"], 1)
        self.assertEqual(view["environment_id"], "gridworld:detour")


class WorkflowDRefusalsTests(_EmbodiedWorkflowCase):
    """D: what the API refuses, and with which code."""

    def test_an_unmeasurable_objective_is_refused_with_422(self) -> None:
        response = self._client.post(
            "/embodied/sessions", json={"objective": {"request": "get better"}}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("OBSERVED", str(response.json()["detail"]))

    def test_a_condition_that_cannot_be_observed_is_refused_with_422(self) -> None:
        response = self._client.post(
            "/embodied/sessions",
            json={"objective": {"request": "win", "success_conditions": ["did well"]}},
        )
        self.assertEqual(response.status_code, 422)

    def test_an_unknown_environment_is_refused_with_422(self) -> None:
        response = self._client.post(
            "/embodied/sessions", json={"objective": _objective(), "environment_id": "minecraft"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("minecraft", str(response.json()["detail"]))

    def test_an_unknown_session_is_404_including_its_run(self) -> None:
        self.assertEqual(self._client.get("/embodied/sessions/nope").status_code, 404)
        self.assertEqual(self._client.post("/embodied/sessions/nope/run").status_code, 404)
        self.assertEqual(self._client.post("/embodied/sessions/nope/pause").status_code, 404)

    def test_a_second_live_session_is_refused_by_the_ceiling(self) -> None:
        self._create()
        response = self._client.post("/embodied/sessions", json={"objective": _objective()})
        self.assertEqual(response.status_code, 422)
        self.assertIn("live session", str(response.json()["detail"]))

    def test_running_a_finished_session_again_is_refused(self) -> None:
        session_id = self._create()["session_id"]
        self._run(session_id)
        response = self._client.post(f"/embodied/sessions/{session_id}/run")
        self.assertEqual(response.status_code, 422)
        self.assertIn("already completed", str(response.json()["detail"]))
        self.assertEqual(self._client.get(f"/embodied/sessions/{session_id}").status_code, 200)


class WorkflowEPauseResumeCancelTests(_EmbodiedWorkflowCase):
    """E: pause, resume and cancel are real transitions, not labels."""

    def test_pausing_stops_inputs_resuming_revalidates_and_running_finishes(self) -> None:
        session_id = self._create()["session_id"]
        paused = self._client.post(f"/embodied/sessions/{session_id}/pause")
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")
        listed = self._client.get("/embodied/sessions").json()
        row = next(item for item in listed["sessions"] if item["session_id"] == session_id)
        self.assertEqual(row["status"], "paused", "the list reads the registry, not the response")

        refused = self._client.post(f"/embodied/sessions/{session_id}/run")
        self.assertEqual(refused.status_code, 422, "a paused session sends nothing")
        self.assertIn("paused", str(refused.json()["detail"]))

        resumed = self._client.post(f"/embodied/sessions/{session_id}/resume")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "created")
        self.assertEqual(resumed.json()["needs_revalidation"], True)

        run = self._run(session_id)
        self.assertEqual(run["status"], "completed", "the resumed session revalidates and finishes")
        self.assertEqual(run["needs_revalidation"], False)

    def test_cancelling_frees_the_slot_and_keeps_the_record_readable(self) -> None:
        first = self._create()["session_id"]
        cancelled = self._client.post(f"/embodied/sessions/{first}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self._client.get("/embodied/sessions").json()["summary"]["live"], 0)

        second = self._create()
        self.assertNotEqual(second["session_id"], first)
        record = self._view(first)
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["terminal"], True)


class WorkflowFBudgetHonestyTests(_EmbodiedWorkflowCase):
    """F: a budget running out is not a completion."""

    def test_a_tiny_budget_ends_as_exhausted_and_never_as_completed(self) -> None:
        session_id = self._create(objective=_objective(max_actions=2))["session_id"]
        run = self._run(session_id, max_actions=2)
        self.assertEqual(run["status"], "budget_exhausted", run["reason"])
        self.assertEqual(run["actions_taken"], 2)
        view = self._view(session_id)
        self.assertEqual(view["verdict"]["met"], False)
        self.assertEqual(view["evaluation"]["objective_completion_rate"], 0.0)
        self.assertIn("budget", view["reason"])
        self.assertEqual(view["finished_at"] != "", True)

    def test_a_run_cannot_be_raised_above_the_session_budget(self) -> None:
        session_id = self._create(objective=_objective(max_actions=2))["session_id"]
        run = self._run(session_id, max_actions=400)
        self.assertLessEqual(run["actions_taken"], 2, "the session's own ceiling wins")

    def test_the_budget_is_enforced_against_what_was_actually_sent(self) -> None:
        # The count comes from the input door, so a two-input budget must send
        # two — not three, and not report a number lower than what left.
        session_id = self._create(objective=_objective(max_actions=2))["session_id"]
        run = self._run(session_id, max_actions=2)
        entries = asyncio.run(self._nova.embodied.audit_log.read())
        self.assertEqual(run["actions_taken"], 2)
        self.assertEqual(len(entries), 2, "the audit trail is the proof of what was sent")
        self.assertEqual(run["evaluation"]["actions_attempted"], 2)
        self.assertEqual(run["evaluation"]["actions_completed"], 2)


class WorkflowGEnvironmentFailureTests(_EmbodiedWorkflowCase):
    """G: an environment that stops answering ends the session honestly."""

    def test_a_frozen_environment_is_reported_as_stale_and_stops_sending(self) -> None:
        session_id = self._create(scenario="frozen")["session_id"]
        run = self._run(session_id)
        self.assertEqual(run["status"], "budget_exhausted", run["reason"])
        self.assertEqual(run["actions_taken"], 0, "nothing was sent on a stale reading")
        view = self._view(session_id)
        self.assertEqual(view["verdict"]["met"], False)
        self.assertGreaterEqual(view["evaluation"]["stale_observations"], 1)
        self.assertGreaterEqual(view["recoveries"], 1)
        self.assertIn("stale", view["reason"])

    def test_a_fogged_environment_is_reported_as_stuck_rather_than_guessed_at(self) -> None:
        session_id = self._create(scenario="fogged")["session_id"]
        run = self._run(session_id)
        self.assertEqual(run["status"], "stuck", run["reason"])
        self.assertEqual(run["actions_taken"], 0)
        self.assertIn("structured state", run["reason"])
        observation = run["last_observation"]
        self.assertEqual(observation["structured"], {}, "no state was invented from pixels")
        self.assertTrue(
            all(not entity["named"] for entity in observation["entities"]),
            "a region stays a region",
        )

    def test_a_timed_out_input_is_re_observed_before_anything_else(self) -> None:
        session_id = self._create(scenario="timing-out")["session_id"]
        view = self._view(self._run(session_id)["session_id"])
        self.assertEqual(view["status"], "completed", view["reason"])
        self.assertGreaterEqual(view["evaluation"]["input_failures"], 1)
        summaries = " ".join(entry["summary"] for entry in view["trajectory"])
        self.assertIn("observed", summaries)


class WorkflowHDriftingTests(_EmbodiedWorkflowCase):
    """H: the environment disagreeing with the declared effect is noticed."""

    def test_a_drifting_environment_is_caught_by_verification_not_assumed_away(self) -> None:
        session_id = self._create(scenario="drifting")["session_id"]
        run = self._run(session_id)
        self.assertEqual(run["status"], "completed", run["reason"])
        view = self._view(session_id)
        self.assertEqual(view["verdict"]["met"], True)
        self.assertGreaterEqual(view["recoveries"], 1, "the unexpected landing forced a recovery")


class WorkflowIEventTests(_EmbodiedWorkflowCase):
    """I: the session announces itself on the ONE bus, with the declared payloads."""

    def test_a_run_publishes_its_lifecycle_events_with_their_required_fields(self) -> None:
        session_id = self._create()["session_id"]
        self._run(session_id)
        events = self._embodied_events()
        kinds = [str(event.type) for event in events]
        self.assertIn("embodied.session_started", kinds)
        self.assertIn("embodied.observation", kinds)
        self.assertIn("embodied.action_executed", kinds)
        self.assertTrue(
            "embodied.objective_verified" in kinds or "embodied.objective_failed" in kinds,
            f"the session's ending was announced: {sorted(set(kinds))}",
        )
        self.assertLess(kinds.index("embodied.session_started"), len(kinds) - 1)
        for event in events:
            with self.subTest(kind=str(event.type)):
                required = EVENT_PAYLOAD_FIELDS[EventType(str(event.type))]
                payload = dict(getattr(event, "payload", {}) or {})
                for field in required:
                    self.assertIn(field, payload, f"{event.type} must carry {field}")
                self.assertEqual(str(getattr(event, "correlation_id", "")), session_id)

    def test_a_refusal_on_the_bus_names_the_action_that_failed(self) -> None:
        session_id = self._create(scenario="refusing")["session_id"]
        self._run(session_id)
        failed = [
            event
            for event in self._embodied_events()
            if str(event.type) == "embodied.action_failed"
        ]
        self.assertTrue(failed, "a failed attempt is announced, not swallowed")
        for event in failed:
            self.assertEqual(dict(event.payload)["session_id"], session_id)


class WorkflowJStatusSurfacesTests(_EmbodiedWorkflowCase):
    """J: the operator surfaces answer, and they agree with each other."""

    def test_the_embodied_status_surface_reports_capabilities_and_ceilings(self) -> None:
        self._run(self._create()["session_id"])
        payload = self._client.get("/embodied/status").json()
        self.assertEqual(payload["allowed"], ["gridworld"])
        self.assertIn("open-east", payload["scenarios"])
        self.assertFalse(payload["enabled"] is None)
        self.assertEqual(payload["scope"], ENVIRONMENT_SCOPE)
        self.assertIn("max_actions", payload["limits"])
        self.assertTrue(payload["capabilities"], "the capability table travels with the status")
        self.assertEqual(payload["sessions"]["completed"], 1)
        environment = next(
            item for item in payload["environments"] if item["environment_id"] == "gridworld"
        )
        self.assertEqual(environment["allowed"], True)
        self.assertEqual(environment["status"], "operational")

    def test_the_application_status_carries_the_same_session_counts(self) -> None:
        session_id = self._create()["session_id"]
        self._run(session_id)
        app = self._client.get("/status").json()["app"]
        self.assertIn("embodied", app)
        self.assertEqual(app["embodied"]["sessions"]["completed"], 1)
        self.assertEqual(app["embodied"]["sessions"]["ids"], [session_id])

    def test_the_overview_endpoint_states_the_phase_and_its_deferrals(self) -> None:
        payload = self._client.get("/embodied/overview").json()
        self.assertEqual(payload["phase"], "25")
        self.assertTrue(payload["deferred"])
        self.assertEqual(
            set(payload["event_types"]),
            set(EVENT_TYPES),
            "the announced vocabulary is the real one",
        )

    def test_the_environments_endpoint_reports_each_dapter_honestly(self) -> None:
        payload = self._client.get("/embodied/environments").json()
        gridworld = next(
            item for item in payload["environments"] if item["environment_id"] == "gridworld"
        )
        self.assertEqual(gridworld["kind"], "simulation")
        self.assertEqual(gridworld["allowed"], True)
        self.assertIn("move", gridworld["actions"])
        verified = {cap["name"]: cap for cap in gridworld["capabilities"]}
        self.assertTrue(verified, "an adapter declares its capabilities")
        self.assertTrue(
            any(cap["verified"] for cap in verified.values()),
            "at least one capability was verified end to end, not merely declared",
        )

    def test_the_evaluation_names_the_ending_it_reports(self) -> None:
        """`Evaluation.terminal` must name the ending, not the state before it.

        The evaluation is built inside the same call that records the ending, so
        reading the status off the state left every record claiming its outcome
        was "running" — and that field exists precisely to tell a failed objective
        from a broken environment from an agent that crashed.
        """
        completed = self._run(self._create()["session_id"])
        self.assertEqual(completed["evaluation"]["terminal"], "completed")
        self.assertEqual(completed["evaluation"]["terminal"], completed["status"])

        exhausted = self._run(
            self._create(objective=_objective(max_actions=3), scenario="frozen")["session_id"]
        )
        self.assertEqual(exhausted["status"], "budget_exhausted")
        self.assertEqual(exhausted["evaluation"]["terminal"], "budget_exhausted")

    def test_the_sessions_list_carries_the_lifecycle_not_the_record(self) -> None:
        session_id = self._create()["session_id"]
        self._run(session_id)
        listed = self._client.get("/embodied/sessions").json()
        row = next(item for item in listed["sessions"] if item["session_id"] == session_id)
        self.assertEqual(row["status"], "completed")
        self.assertIn("actions_taken", row)
        for heavyweight in (
            "objective",
            "verdict",
            "last_observation",
            "trajectory",
            "evaluation",
        ):
            self.assertNotIn(
                heavyweight,
                row,
                "the list is an operator surface: the record lives on the single-session view",
            )
        record = self._view(session_id)
        self.assertIn("trajectory", record, "the view is where the record lives")
        self.assertIn("objective", record)
        self.assertTrue(record["last_observation"], "the view carries the observation")


class WorkflowMDenialTests(_EmbodiedWorkflowCase):
    """M: a refusal by the action policy ends the session, and sends nothing.

    The refusal is produced by the REAL permission layer: the environment input
    is re-declared CRITICAL on the application's own layer, which is a level the
    policy never allows without a person. Nothing about this test is mocked.
    """

    def _declare_critical(self) -> None:
        self._nova.risk.declare(
            ENVIRONMENT_INPUT,
            risk_level=RiskLevel.CRITICAL,
            required_permission=PermissionScope.ENVIRONMENT_CONTROL,
            requires_confirmation=False,
            reversible=True,
            destructive=False,
        )

    def test_a_policy_refusal_ends_the_session_as_denied_with_nothing_sent(self) -> None:
        session_id = self._create()["session_id"]
        self._declare_critical()
        run = self._run(session_id)
        self.assertEqual(run["status"], "denied", run["reason"])
        self.assertEqual(run["actions_taken"], 0, "no input may leave after a refusal")
        self.assertFalse(run["verdict"]["met"])
        self.assertIn("critical", run["reason"], "the reason names what refused it")

    def test_a_refusal_is_a_rejection_and_never_a_retry(self) -> None:
        session_id = self._create()["session_id"]
        self._declare_critical()
        run = self._run(session_id)
        evaluation = run["evaluation"]
        self.assertEqual(evaluation["actions_rejected"], 1, evaluation)
        self.assertEqual(
            evaluation["invalid_actions"],
            0,
            "a refused attempt is not a malformed one — a refusal is an answer",
        )
        self.assertEqual(evaluation["actions_failed"], 0, "a refusal is not a malfunction")
        self.assertEqual(evaluation["terminal"], "denied")
        refused = self._client.post(f"/embodied/sessions/{session_id}/run")
        self.assertEqual(refused.status_code, 422, "a refused session is not retried")
        self.assertIn("denied", str(refused.json()["detail"]))


class WorkflowKAuditTrailTests(_EmbodiedWorkflowCase):
    """K: every input lands on the shared automation audit trail, once."""

    def test_every_sent_input_is_audited_exactly_once(self) -> None:
        session_id = self._create()["session_id"]
        run = self._run(session_id)
        entries = asyncio.run(self._nova.embodied.audit_log.read())
        self.assertEqual(len(entries), run["actions_taken"])
        self.assertTrue(all(entry.output["session_id"] == session_id for entry in entries))
        self.assertTrue(all(entry.output["risk"] == "low" for entry in entries))
        self.assertEqual(
            [entry.status for entry in entries].count("completed"), run["actions_taken"]
        )

    def test_a_refused_input_is_audited_as_refused(self) -> None:
        session_id = self._create(scenario="refusing")["session_id"]
        run = self._run(session_id)
        entries = asyncio.run(self._nova.embodied.audit_log.read())
        self.assertEqual(len(entries), run["actions_taken"])
        self.assertGreaterEqual([entry.status for entry in entries].count("rejected"), 1)


class WorkflowLPersistenceTests(_EmbodiedWorkflowCase):
    """L: sessions survive a restart as work that must be revalidated first."""

    def test_a_live_session_comes_back_interrupted_and_needs_revalidation(self) -> None:
        session_id = self._create()["session_id"]
        # A second application over the SAME data directory is what a restart is.
        second = NovaControlApplication(data_dir=self._tmp.name)  # type: ignore[misc]
        try:
            restored = second.embodied_view(session_id)
            self.assertEqual(restored["status"], "interrupted")
            self.assertEqual(restored["needs_revalidation"], True)
            self.assertIn("restart", restored["reason"])
            report = second.embodied_status()["restored"]
            self.assertEqual(report["restored"], 1)
            self.assertEqual(report["interrupted"], [session_id])
            self.assertEqual(second.embodied.status()["wired"]["persistence"], True)
        finally:
            asyncio.run(second.stop())

    def test_a_finished_session_is_not_restored_as_work(self) -> None:
        session_id = self._create()["session_id"]
        self._run(session_id)
        second = NovaControlApplication(data_dir=self._tmp.name)  # type: ignore[misc]
        try:
            self.assertIsNone(second.embodied.get(session_id), "history is not work")
            self.assertEqual(second.embodied_status()["restored"]["restored"], 0)
        finally:
            asyncio.run(second.stop())

    def test_a_restored_scenario_session_runs_to_completion(self) -> None:
        """The configured world is rebuilt, so what comes back is actually drivable."""
        session_id = self._create(scenario="detour")["session_id"]
        second = NovaControlApplication(data_dir=self._tmp.name)  # type: ignore[misc]
        try:
            restored = second.embodied_view(session_id)
            self.assertEqual(restored["status"], "interrupted")
            self.assertEqual(restored["environment_id"], "gridworld:detour")
            self.assertIsNotNone(
                second.embodied.environment("gridworld:detour"),
                "a restored session's scenario is rebuilt on this process's registry",
            )
            run = asyncio.run(second.embodied_run_session(session_id))
            self.assertEqual(run["status"], "completed", run.get("reason"))
            self.assertFalse(run["needs_revalidation"], "running it revalidated the session")
        finally:
            asyncio.run(second.stop())


class RecordShapeTests(unittest.TestCase):
    """A session's record is operational metadata and nothing else."""

    def test_a_session_view_is_json_safe_and_carries_no_free_text_trace(self) -> None:
        from novacontrol.embodied.models import SessionState

        state = SessionState(
            environment_id="gridworld",
            scenario="open-east",
            status=SessionStatus.CREATED,
            objective=Objective.from_mapping(_objective()),
        )
        payload = state.to_dict()
        json.dumps(payload, sort_keys=True)  # a record a status surface can publish
        text = json.dumps(payload).lower()
        for forbidden in ("chain_of_thought", "reasoning", "prompt", "frame", "screenshot"):
            self.assertNotIn(forbidden, text, f"the record must not carry {forbidden}")
        entry = payload["trajectory"]
        self.assertEqual(entry, [], "a created session has not acted yet")

    def test_the_audit_row_is_the_shared_auditable_result_shape(self) -> None:
        row = EmbodiedActionResult(action_id="act-1", status=ActionStatus.COMPLETED)
        self.assertEqual(row.action_id, "act-1")
        self.assertEqual(row.status, "completed", "an AuditableResult carries a plain status")
        self.assertEqual(row.output, {})
        self.assertIsNone(row.error)
        self.assertIsNone(row.approval_id)
        self.assertEqual(row.to_dict()["status"], "completed")


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
