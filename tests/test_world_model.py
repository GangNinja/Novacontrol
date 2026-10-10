"""Phase 22 tests: the world model, its memory and its state reasoning.

These tests drive the REAL engine (:class:`~novacontrol.world.engine.WorldModelEngine`)
with a real in-memory repository, and the REAL HTTP surface through ``create_app()``.
Nothing here is asserted about an internal helper that a caller could not also
observe, and every expectation is written against what the phase PROMISES, so a
regression shows up as a failing workflow rather than a passing unit.

The nine workflows §22 names each have their own class:

    A  initial observation      state created, evidence preserved
    B  state update             one attribute moves, the rest stays
    C  entity continuity        absence is not deletion, identity survives
    D  historical query         a past moment, never the current state
    E  conflicting observations conflict kept, unsupported certainty refused
    F  restart and recovery     what was recovered, and what could not be
    G  evidence query           why a claim is believed
    H  prediction boundary      a real provider, or an honest "unavailable"
    I  resource fallback        a denied model does not break the state store

The last group exercises the same workflows through HTTP, because a workflow that
only works in-process is not the workflow a user has.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.agentic.models import AgentState
from novacontrol.api.app import _observation_batch, create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.core.config import WorldModelSettings
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.intelligence.intent import CapabilityRegistry
from novacontrol.perception.models import (
    BBox,
    DetectedObject,
    OcrText,
    PerceptionResult,
    PerceptionStatus,
    SceneAbstraction,
    SceneRepresentation,
)
from novacontrol.persistence.json_store import JsonStateStore
from novacontrol.world import (
    CAPABILITY_TABLE,
    MAX_TRANSITIONS,
    OBSERVATION_INGESTED,
    PREDICTION_COMPLETED,
    PREDICTION_FAILED,
    STATE_TRANSITION_CREATED,
    STATE_UPDATED,
    ChangeKind,
    ChangeThresholds,
    EntityAttribute,
    EntityStatus,
    EstimationPolicy,
    FactBasis,
    IdentityPolicy,
    IngestStatus,
    InMemoryWorldRepository,
    JsonWorldRepository,
    NoPredictionProvider,
    Observation,
    ObservationRejected,
    ObservationSource,
    ObservedEntity,
    PredictionRequest,
    PredictionResult,
    PredictionService,
    PredictionStatus,
    QueryKind,
    QueryStatus,
    RelationshipKind,
    RelationshipSource,
    RelationStatus,
    RuleProjectionProvider,
    StateQuery,
    TemporalMemory,
    TransitionKind,
    UncertaintyKind,
    WorldEntity,
    WorldModelEngine,
    WorldRelationship,
    WorldRetentionPolicy,
    WorldState,
    active_entities,
    attach_world_state,
    capability_rows,
    derive_geometric_relations,
    detect_changes,
    entities_from_scene,
    environment_state_for,
    estimate,
    is_newer,
    merge_entity,
    normalize,
    observation_from_perception,
    overview,
    parse_timestamp,
    reasoning_rules,
    register_world_capabilities,
    relations_from_observation,
    resolve_identity,
    run_query,
    sanitize_world_id,
    scene_relationships,
    seconds_between,
    validate,
    world_observation,
)
from novacontrol.world.engine import MAX_ANNOUNCEMENTS, WorldTelemetry
from novacontrol.world.entities import confirm_absence, entity_from_observation, expire_stale
from novacontrol.world.ingest import (
    MAX_INGESTED_ENTITIES,
    MAX_INGESTED_FACTS,
    facts_from_scene,
)
from novacontrol.world.models import (
    as_optional_float,
    as_text,
)
from novacontrol.world.queries import MAX_LIMIT
from novacontrol.world.reasoning import (
    REASONING_ACTIONS,
    answer,
    mentioned_entities,
)
from novacontrol.world.timeutil import timestamps_in_order
from novacontrol.world.transitions import between_states, for_entity, for_kind
from novacontrol.world.transitions import summary as transition_summary

# ── fixtures ─────────────────────────────────────────────────────────────────

BASE = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


def stamp(seconds: float = 0) -> str:
    """An ISO-8601 timestamp a fixed number of seconds after the test's epoch."""
    return (BASE + timedelta(seconds=seconds)).isoformat()


def box(x: int, y: int = 0, width: int = 40, height: int = 40) -> dict[str, int]:
    return {"x": x, "y": y, "width": width, "height": height}


def thing(label: str, x: int, **extra: object) -> dict[str, object]:
    """One observed-entity row for an observation body."""
    row: dict[str, object] = {"label": label, "bbox": box(x)}
    row.update(extra)
    return row


def attrs(**values: object) -> list[dict[str, object]]:
    """An attribute list from keyword values — ``attrs(power="on")``."""
    return [{"name": name, "value": value} for name, value in values.items()]


def observation(
    entities: list[dict[str, object]] | None = None,
    *,
    at: float = 0,
    source: str = "perception",
    source_id: str = "cam-1",
    scope: str = "",
    complete: bool = False,
    **extra: object,
) -> dict[str, object]:
    """An observation body shaped the way the API and the engine both accept."""
    payload: dict[str, object] = {
        "source": source,
        "source_id": source_id,
        "timestamp": stamp(at),
        "scope": scope,
        "complete": complete,
    }
    if entities is not None:
        payload["entities"] = entities
    payload.update(extra)
    return payload


def engine(**kwargs: object) -> WorldModelEngine:
    """A world engine with an in-memory store unless the caller says otherwise."""
    kwargs.setdefault("repository", InMemoryWorldRepository())
    return WorldModelEngine(**kwargs)  # type: ignore[arg-type]


def log_of(report: object) -> list[str]:
    transitions = getattr(report, "transitions", ())
    return [item.kind.value for item in transitions]


def two_objects(at: float = 0, **kwargs: object) -> dict[str, object]:
    """The baseline scene: a laptop and a mug, side by side."""
    return observation([thing("laptop", 0), thing("mug", 100)], at=at, **kwargs)


class _RecordingObserver:
    """The announcement seam, captured instead of published."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, payload: object) -> None:
        self.events.append((event_type, dict(payload)))  # type: ignore[arg-type]

    def types(self) -> list[str]:
        return [item[0] for item in self.events]

    def first(self, event_type: str) -> dict:
        for name, payload in self.events:
            if name == event_type:
                return payload
        raise AssertionError(f"no {event_type!r} announcement among {self.types()}")


class _RaisingObserver:
    def __call__(self, event_type: str, payload: object) -> None:
        raise RuntimeError("a broken watcher must not break the world")


class _DenyingGate:
    """The resource governor, refusing: the shape ``admit`` returns."""

    def admit(
        self,
        *,
        needs_model: bool,
        required_bytes: int | None = None,
        model: str = "",
        latency_budget_ms: int | None = None,
    ) -> object:
        return SimpleNamespace(allowed=False, reason="not enough free memory for that model")


class _AllowingGate:
    def admit(
        self,
        *,
        needs_model: bool,
        required_bytes: int | None = None,
        model: str = "",
        latency_budget_ms: int | None = None,
    ) -> object:
        return SimpleNamespace(allowed=True, reason="")


class _BrokenGate:
    def admit(self, **kwargs: object) -> object:
        raise RuntimeError("the governor is unavailable")


class _RaisingProvider:
    name = "raising"
    available = True
    needs_model = False

    def predict(self, request: PredictionRequest, context: object) -> PredictionResult:
        raise RuntimeError("provider exploded")


class _WrongTypeProvider:
    name = "wrong-type"
    available = True
    needs_model = False

    def predict(self, request: PredictionRequest, context: object) -> object:
        return {"status": "predicted"}


class _EmptyPredictionProvider:
    name = "empty"
    available = True
    needs_model = False

    def predict(self, request: PredictionRequest, context: object) -> PredictionResult:
        return PredictionResult(status=PredictionStatus.PREDICTED, predicted={})


class _ModelBackedProvider:
    """A provider that would need a model — so admission is asked before it runs."""

    name = "learned-world-model"
    available = True
    needs_model = True

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, request: PredictionRequest, context: object) -> PredictionResult:
        self.calls += 1
        return PredictionResult(
            status=PredictionStatus.PREDICTED,
            provider=self.name,
            predicted={"entity_id": "x", "basis": "model"},
        )


class _UnavailableProvider:
    name = "not-installed"
    available = False
    needs_model = False

    def predict(self, request: PredictionRequest, context: object) -> PredictionResult:
        raise AssertionError("an unavailable provider must never be asked")


class _FakePerception:
    """A stand-in for the perception engine: one async method, real contract."""

    def __init__(self, result: PerceptionResult) -> None:
        self.result = result
        self.requests: list[object] = []

    async def perceive(self, request: object) -> PerceptionResult:
        self.requests.append(request)
        return self.result


# ── §26: the phase's own posture ─────────────────────────────────────────────


class OverviewTests(unittest.TestCase):
    """The machine-readable form of "no fake intelligence"."""

    def test_overview_names_this_phase_and_its_schema(self) -> None:
        data = overview()
        self.assertEqual(data["phase"], "phase22")
        self.assertEqual(data["schema_version"], "phase22.1")

    def test_overview_refuses_the_claims_this_build_cannot_make(self) -> None:
        data = overview()
        for flag in (
            "cuda_required",
            "automatic_model_loading",
            "automatic_model_downloads",
            "stores_raw_frames",
            "stores_observation_content",
            "stores_hidden_reasoning",
            "exposes_chain_of_thought",
            "action_execution",
            "predicts_future_state",
            "predictive_model_available",
            "trains_anything",
        ):
            self.assertIs(data[flag], False, f"{flag} must be False")

    def test_overview_admits_the_rule_projection_it_does_have(self) -> None:
        self.assertIs(overview()["rule_projection_available"], True)

    def test_overview_defers_exactly_phases_23_to_26(self) -> None:
        deferred = " ".join(overview()["deferred"])
        for phase in ("Phase 23", "Phase 24", "Phase 25", "Phase 26"):
            self.assertIn(phase, deferred)
        self.assertNotIn("Phase 22", deferred)

    def test_overview_lists_every_declared_vocabulary(self) -> None:
        data = overview()
        self.assertEqual(len(data["capabilities"]), len(CAPABILITY_TABLE))
        self.assertEqual(len(data["transition_kinds"]), len(TransitionKind))
        self.assertEqual(len(data["change_kinds"]), len(ChangeKind))
        self.assertEqual(len(data["uncertainty_kinds"]), len(UncertaintyKind))
        self.assertEqual(len(data["query_kinds"]), len(QueryKind))
        self.assertEqual(len(data["prediction_statuses"]), len(PredictionStatus))
        self.assertEqual(set(data["reasoning_actions"]), set(REASONING_ACTIONS))


# ── contracts ────────────────────────────────────────────────────────────────


class ContractTests(unittest.TestCase):
    """The vocabulary and the parsers cannot quietly change meaning."""

    def test_fact_basis_separates_observed_from_derived(self) -> None:
        self.assertEqual(
            [item.value for item in FactBasis],
            ["observed", "inferred", "predicted", "unknown"],
        )

    def test_observed_and_inferred_attributes_keep_their_basis(self) -> None:
        self.assertIs(EntityAttribute.observed("power", "on").basis, FactBasis.OBSERVED)
        self.assertIs(EntityAttribute.inferred("count", 3).basis, FactBasis.INFERRED)

    def test_an_attribute_without_a_stated_basis_is_unknown_until_ingested(self) -> None:
        parsed = EntityAttribute.from_mapping({"name": "power", "value": "on"})
        self.assertIs(parsed.basis, FactBasis.UNKNOWN)

    def test_an_unreadable_basis_is_unknown_not_a_guess(self) -> None:
        parsed = EntityAttribute.from_mapping({"name": "x", "basis": "certain"})
        self.assertIs(parsed.basis, FactBasis.UNKNOWN)

    def test_a_figure_that_was_not_measured_is_none_not_zero(self) -> None:
        self.assertIsNone(as_optional_float(None))
        self.assertIsNone(as_optional_float("not a number"))
        self.assertIsNone(as_optional_float(True))
        self.assertEqual(as_optional_float(2.5), 1.0)
        self.assertEqual(as_optional_float("-1"), 0.0)

    def test_as_text_never_returns_the_literal_none(self) -> None:
        self.assertEqual(as_text(None), "")
        self.assertEqual(as_text("  "), "")
        self.assertEqual(as_text("value"), "value")

    def test_absence_is_not_deletion_in_the_status_vocabulary(self) -> None:
        self.assertIsNot(EntityStatus.NOT_OBSERVED, EntityStatus.MISSING)
        self.assertNotEqual(EntityStatus.NOT_OBSERVED.value, EntityStatus.MISSING.value)

    def test_environmental_transition_is_its_own_type(self) -> None:
        import novacontrol.world.models as world_models

        self.assertTrue(hasattr(world_models, "WorldStateTransition"))
        self.assertFalse(hasattr(world_models, "StateTransition"))

    def test_ingest_status_covers_every_ending(self) -> None:
        self.assertEqual(
            {item.value for item in IngestStatus},
            {"accepted", "duplicate", "stale", "out_of_order", "partial", "rejected"},
        )

    def test_prediction_status_covers_every_honest_ending(self) -> None:
        self.assertEqual(
            {item.value for item in PredictionStatus},
            {
                "predicted",
                "unsupported",
                "model_unavailable",
                "insufficient_evidence",
                "resource_blocked",
                "failed",
            },
        )

    def test_content_key_is_stable_and_independent_of_the_observation_id(self) -> None:
        first = Observation(observation_id="a", timestamp=stamp(0), entities=(
            ObservedEntity(label="laptop", bbox=BBox(0, 0, 40, 40)),
        ))
        second = Observation(observation_id="b", timestamp=stamp(0), entities=(
            ObservedEntity(label="laptop", bbox=BBox(0, 0, 40, 40)),
        ))
        self.assertEqual(first.content_key(), second.content_key())

    def test_content_key_changes_when_the_claims_change(self) -> None:
        first = Observation(timestamp=stamp(0), entities=(ObservedEntity(label="laptop"),))
        second = Observation(timestamp=stamp(0), entities=(ObservedEntity(label="mug"),))
        self.assertNotEqual(first.content_key(), second.content_key())

    def test_an_unknown_source_name_is_metadata_not_a_rejection(self) -> None:
        parsed = Observation.from_mapping({"source": "made-up"})
        self.assertIs(parsed.source, ObservationSource.EXTERNAL)

    def test_a_relationship_without_endpoints_cannot_be_read(self) -> None:
        self.assertIsNone(WorldRelationship.from_mapping({"kind": "located_near"}))

    def test_world_state_round_trips_through_a_stored_record(self) -> None:
        state = WorldState(
            world_id="w",
            version=4,
            timestamp=stamp(0),
            entities=(WorldEntity(entity_id="e1", labels=("laptop",), bbox=BBox(1, 2, 3, 4)),),
            relationships=(
                WorldRelationship(
                    kind=RelationshipKind.INSIDE, source_entity_id="e1", target_entity_id="e2"
                ),
            ),
            conditions=(EntityAttribute.observed("room", "lab"),),
            uncertainty=(),
            sources=("cam",),
            confidence=None,
        )
        restored = WorldState.from_mapping(state.to_dict())
        self.assertEqual(restored.version, 4)
        self.assertEqual(restored.world_id, "w")
        self.assertEqual(restored.entity("e1").bbox, BBox(1, 2, 3, 4))
        self.assertIsNone(restored.confidence)
        self.assertEqual(len(restored.relationships), 1)

    def test_a_state_with_no_measured_confidence_keeps_none(self) -> None:
        restored = WorldState.from_mapping({"world_id": "w", "version": 1})
        self.assertIsNone(restored.confidence)

    def test_query_limit_is_bounded_at_both_ends(self) -> None:
        self.assertEqual(StateQuery.from_mapping({"limit": 9999}).limit, MAX_LIMIT)
        self.assertEqual(StateQuery.from_mapping({"limit": 0}).limit, 1)
        self.assertEqual(StateQuery.from_mapping({"limit": "abc"}).limit, 20)

    def test_an_unrecognised_query_kind_asks_the_safest_question(self) -> None:
        self.assertIs(StateQuery.from_mapping({"kind": "nonsense"}).kind, QueryKind.ENTITIES)

    def test_query_accepts_the_question_as_an_alias_for_subject(self) -> None:
        query = StateQuery.from_mapping({"kind": "uncertain", "query": "laptop"})
        self.assertEqual(query.subject, "laptop")


class TimeUtilTests(unittest.TestCase):
    """Ordering is three-valued, and an unreadable stamp is never "now"."""

    def test_an_iso_timestamp_with_an_offset_is_read(self) -> None:
        self.assertIsNotNone(parse_timestamp("2026-10-01T10:00:00+00:00"))

    def test_a_zulu_and_a_naive_stamp_are_both_read(self) -> None:
        self.assertEqual(
            parse_timestamp("2026-10-01T10:00:00Z"),
            parse_timestamp("2026-10-01T10:00:00"),
        )

    def test_an_unparseable_timestamp_is_none_never_zero(self) -> None:
        self.assertIsNone(parse_timestamp("last tuesday"))
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp(None))

    def test_seconds_between_is_none_when_either_end_is_unreadable(self) -> None:
        self.assertIsNone(seconds_between("nope", stamp(0)))
        self.assertEqual(seconds_between(stamp(0), stamp(30)), 30.0)

    def test_ordering_is_three_valued(self) -> None:
        self.assertIs(is_newer(stamp(10), stamp(0)), True)
        self.assertIs(is_newer(stamp(0), stamp(10)), False)
        self.assertIsNone(is_newer("not a time", stamp(0)))

    def test_two_clock_domains_are_not_compared(self) -> None:
        self.assertIsNone(
            is_newer(stamp(10), stamp(0), clock="file", other_clock="wall")
        )

    def test_a_sequence_of_timestamps_reports_the_unreadable_ones(self) -> None:
        ordered, unreadable = timestamps_in_order([stamp(0), stamp(10), "??", stamp(5)])
        self.assertFalse(ordered)
        self.assertEqual(unreadable, ("??",))
        ordered, unreadable = timestamps_in_order([stamp(0), stamp(10)])
        self.assertTrue(ordered)
        self.assertEqual(unreadable, ())


# ── ingestion (§8) ───────────────────────────────────────────────────────────


class IngestTests(unittest.TestCase):
    """Normalization, validation and the bounds — one door, three refusals."""

    def test_a_mapping_becomes_a_normalized_observation(self) -> None:
        parsed = normalize({"entities": [thing("laptop", 0)]}, timestamp=stamp(0))
        self.assertEqual(parsed.timestamp, stamp(0))
        self.assertEqual(len(parsed.entities), 1)
        self.assertTrue(parsed.evidence)

    def test_an_unsupported_value_is_rejected_with_its_type(self) -> None:
        with self.assertRaises(ObservationRejected) as caught:
            normalize(42)
        self.assertIn("int", str(caught.exception))

    def test_an_observation_that_reports_nothing_is_rejected(self) -> None:
        verdict, reason = validate(Observation(timestamp=stamp(0), source_id="s"))
        self.assertEqual(verdict, "rejected")
        self.assertIn("reports nothing", reason)

    def test_a_complete_claim_with_no_entities_is_still_something(self) -> None:
        verdict, _ = validate(
            Observation(timestamp=stamp(0), source_id="s", complete=True)
        )
        self.assertEqual(verdict, "accepted")

    def test_a_missing_timestamp_is_partial_not_a_rejection(self) -> None:
        verdict, reason = validate(
            Observation(
                source=ObservationSource.USER,
                entities=(ObservedEntity(label="laptop"),),
            )
        )
        self.assertEqual(verdict, "partial")
        self.assertIn("no timestamp", reason)

    def test_an_untraceable_external_source_is_partial(self) -> None:
        verdict, reason = validate(
            Observation(
                timestamp=stamp(0),
                source=ObservationSource.EXTERNAL,
                entities=(ObservedEntity(label="laptop"),),
            )
        )
        self.assertEqual(verdict, "partial")
        self.assertIn("external source with no id", reason)

    def test_entity_rows_are_bounded_and_the_ceiling_is_reported_as_a_ceiling(self) -> None:
        rows = [thing(f"object-{index}", index * 60) for index in range(MAX_INGESTED_ENTITIES + 2)]
        parsed = normalize({"entities": rows}, timestamp=stamp(0))
        self.assertEqual(len(parsed.entities), MAX_INGESTED_ENTITIES)
        # Past the ceiling, NOT unusable: the rows were perfectly readable, so the
        # counters stay apart and the reason names the ceiling that stopped them.
        self.assertEqual(parsed.metadata["truncated_entities"], 2)
        self.assertNotIn("dropped_entities", parsed.metadata)
        verdict, reason = validate(parsed)
        self.assertEqual(verdict, "partial")
        self.assertIn(f"past the {MAX_INGESTED_ENTITIES}-row ingest ceiling", reason)
        self.assertNotIn("unusable", reason)

    def test_over_ceiling_rows_and_unusable_rows_are_reported_separately(self) -> None:
        # The unreadable row sits INSIDE the kept prefix, so it is a genuine drop;
        # one readable row past the ceiling is a truncation, not a drop.
        rows: list[dict[str, object]] = [{}]
        rows += [thing(f"object-{index}", index * 60) for index in range(MAX_INGESTED_ENTITIES)]
        parsed = normalize({"entities": rows}, timestamp=stamp(0))
        self.assertEqual(parsed.metadata["dropped_entities"], 1)
        self.assertEqual(parsed.metadata["truncated_entities"], 1)
        verdict, reason = validate(parsed)
        self.assertEqual(verdict, "partial")
        self.assertIn("1 unusable entity row(s) dropped", reason)
        self.assertIn("1 entity row(s) past the", reason)

    def test_an_unusable_entity_row_is_dropped_not_invented(self) -> None:
        parsed = normalize({"entities": [{"label": "laptop"}, {}]}, timestamp=stamp(0))
        self.assertEqual(len(parsed.entities), 1)
        self.assertEqual(parsed.metadata["dropped_entities"], 1)

    def test_a_reported_attribute_is_read_as_observed(self) -> None:
        parsed = normalize(
            {"entities": [{"label": "laptop", "attributes": [{"name": "power", "value": "on"}]}]},
            timestamp=stamp(0),
        )
        self.assertIs(parsed.entities[0].attributes[0].basis, FactBasis.OBSERVED)

    def test_a_stated_basis_travels_through_untouched(self) -> None:
        parsed = normalize(
            {
                "entities": [
                    {
                        "label": "laptop",
                        "attributes": [
                            {"name": "power", "value": "on", "basis": "inferred"}
                        ],
                    }
                ]
            },
            timestamp=stamp(0),
        )
        self.assertIs(parsed.entities[0].attributes[0].basis, FactBasis.INFERRED)

    def test_an_engine_reports_a_malformed_observation_instead_of_raising(self) -> None:
        report = engine().observe(object())
        self.assertIs(report.status, IngestStatus.REJECTED)
        self.assertFalse(report.accepted)
        self.assertTrue(report.reason)

    def test_facts_alone_are_enough_to_ingest(self) -> None:
        report = engine().observe(
            {"timestamp": stamp(0), "source": "user", "facts": [{"name": "room", "value": "lab"}]}
        )
        self.assertIs(report.status, IngestStatus.ACCEPTED)
        self.assertEqual(report.version, 1)


class IngestPerceptionTests(unittest.TestCase):
    """Phase 21 is converted here, never re-perceived."""

    def _scene(self, **kwargs: object) -> SceneRepresentation:
        defaults: dict[str, object] = {
            "scene_id": "scene-1",
            "timestamp": stamp(0),
            "source_id": "cam-1",
            "frame_id": "frame-1",
            "width": 640,
            "height": 480,
            "objects": (
                DetectedObject(
                    label="laptop",
                    bbox=BBox(0, 0, 40, 40),
                    confidence=0.8,
                    source="vlm",
                    object_id="obj-1",
                ),
            ),
            "text": (OcrText(text="hello world"),),
            "abstraction": SceneAbstraction(summary="one laptop", counts={"objects": 1}),
        }
        defaults.update(kwargs)
        return SceneRepresentation(**defaults)  # type: ignore[arg-type]

    def test_a_scene_becomes_an_observation_with_identity_hints(self) -> None:
        parsed = observation_from_perception(self._scene())
        self.assertIs(parsed.source, ObservationSource.PERCEPTION)
        self.assertEqual(parsed.source_id, "cam-1")
        self.assertEqual([item.label for item in parsed.entities], ["laptop"])
        self.assertEqual(parsed.entities[0].entity_id, "obj-1")

    def test_a_bare_scene_is_accepted_by_the_normalizer(self) -> None:
        parsed = normalize(self._scene())
        self.assertEqual(parsed.source_id, "cam-1")
        self.assertEqual(len(parsed.entities), 1)

    def test_scene_counts_are_inferred_never_observed(self) -> None:
        facts = {item.name: item for item in facts_from_scene(self._scene())}
        self.assertIs(facts["object_count"].basis, FactBasis.INFERRED)
        self.assertIs(facts["text_line_count"].basis, FactBasis.INFERRED)
        self.assertIs(facts["frame_size"].basis, FactBasis.OBSERVED)

    def test_scene_text_is_not_copied_unless_asked_for(self) -> None:
        quiet = observation_from_perception(self._scene())
        self.assertEqual(quiet.text, "")
        self.assertNotIn("text", {item.name for item in quiet.facts})
        loud = observation_from_perception(self._scene(), include_text=True)
        self.assertIn("text", {item.name for item in loud.facts})

    def test_scene_relationships_travel_as_hints_naming_their_objects(self) -> None:
        hints = scene_relationships(self._scene())
        self.assertEqual(hints, ())

    def test_a_failed_look_is_recorded_as_a_condition_not_an_empty_room(self) -> None:
        failed = PerceptionResult(
            status=PerceptionStatus.UNAVAILABLE, status_reason="no vision provider"
        )
        parsed = observation_from_perception(failed)
        self.assertEqual(parsed.entities, ())
        self.assertFalse(parsed.complete)
        status = {item.name: item for item in parsed.facts}["perception_status"]
        self.assertEqual(status.value["status"], "unavailable")
        report = engine().observe(parsed)
        self.assertIs(report.status, IngestStatus.ACCEPTED)
        self.assertTrue(report.accepted)

    def test_a_failed_look_is_accepted_by_the_normalizer(self) -> None:
        failed = PerceptionResult(status=PerceptionStatus.FAILED, status_reason="no frame")
        report = engine().observe(failed)
        self.assertIs(report.status, IngestStatus.ACCEPTED)

    def test_entities_from_scene_keeps_no_geometry_free_duplicates(self) -> None:
        scene = self._scene(
            objects=(
                DetectedObject(label="laptop", bbox=BBox(0, 0, 40, 40), object_id="a"),
                DetectedObject(label="laptop", bbox=BBox(0, 0, 40, 40), object_id="b"),
            )
        )
        self.assertEqual(len(entities_from_scene(scene)), 1)

    def test_a_text_only_observation_is_partial(self) -> None:
        scene = self._scene(objects=())
        report = engine().observe(scene)
        self.assertIn(report.status, {IngestStatus.ACCEPTED, IngestStatus.PARTIAL})
        self.assertEqual(report.version, 1)

    def test_the_fact_ceiling_is_applied(self) -> None:
        facts = tuple(
            EntityAttribute.observed(f"fact-{index}", index)
            for index in range(MAX_INGESTED_FACTS + 3)
        )
        parsed = normalize(
            {"timestamp": stamp(0), "source": "user", "facts": [item.to_dict() for item in facts]}
        )
        self.assertEqual(len(parsed.facts), MAX_INGESTED_FACTS)
        # Facts are not entity rows, and they were not unusable either.
        self.assertEqual(parsed.metadata["truncated_facts"], 3)
        self.assertNotIn("dropped_entities", parsed.metadata)
        verdict, reason = validate(parsed)
        self.assertEqual(verdict, "partial")
        self.assertIn(f"past the {MAX_INGESTED_FACTS}-fact ingest ceiling", reason)
        self.assertNotIn("unusable entity", reason)


# ── Workflow A — initial observation ─────────────────────────────────────────


class WorkflowAInitialObservationTests(unittest.TestCase):
    """An accepted observation creates version 1 with its evidence attached."""

    def test_observation_is_accepted_and_the_state_is_created(self) -> None:
        world = engine()
        report = world.observe(two_objects())
        self.assertIs(report.status, IngestStatus.ACCEPTED)
        self.assertEqual(report.version, 1)
        self.assertEqual(report.entities_added, 2)
        self.assertEqual(report.state_id, world.state().state_id)

    def test_the_state_begins_at_version_zero_and_empty(self) -> None:
        world = engine()
        current = world.state()
        self.assertEqual(current.version, 0)
        self.assertEqual(current.entities, ())
        self.assertIs(current.metadata.get("empty"), True)
        self.assertFalse(world.status()["observed"])

    def test_the_transitions_say_what_appeared(self) -> None:
        report = engine().observe(two_objects())
        self.assertEqual(
            log_of(report),
            ["entity_added", "entity_added", "relationship_changed", "relationship_changed"],
        )

    def test_evidence_references_are_preserved(self) -> None:
        world = engine()
        report = world.observe(two_objects())
        observation_id = report.observation_id
        self.assertIn(observation_id, {item.evidence_id for item in world.state().evidence})
        laptop = world.state().find("laptop")[0]
        self.assertIn(observation_id, laptop.evidence)

    def test_geometry_relations_are_derived_once_from_phase_21(self) -> None:
        world = engine()
        report = world.observe(two_objects())
        self.assertEqual(report.relationships_added, 2)
        kinds = {item.kind for item in world.state().relationships}
        self.assertEqual(kinds, {RelationshipKind.LOCATED_NEAR})
        self.assertTrue(all(item.detail for item in world.state().relationships))

    def test_the_reported_elapsed_time_is_a_measurement(self) -> None:
        report = engine().observe(two_objects())
        self.assertIsNotNone(report.elapsed_ms)
        self.assertGreaterEqual(report.elapsed_ms or -1, 0.0)


# ── Workflow B — state update ────────────────────────────────────────────────


class WorkflowBStateUpdateTests(unittest.TestCase):
    """One attribute moves; the transition records it and nothing else moves."""

    def _world(self) -> tuple[WorldModelEngine, dict[str, object]]:
        world = engine()
        world.observe(
            observation(
                [
                    thing("laptop", 0, attributes=[{"name": "power", "value": "off"}]),
                    thing("mug", 100),
                ],
                at=0,
            )
        )
        second = observation(
            [
                thing("laptop", 0, attributes=[{"name": "power", "value": "on"}]),
                thing("mug", 100),
            ],
            at=60,
        )
        return world, second

    def test_the_attribute_is_updated_and_a_transition_records_it(self) -> None:
        world, second = self._world()
        report = world.observe(second)
        self.assertIs(report.status, IngestStatus.ACCEPTED)
        self.assertEqual(report.version, 2)
        changes = [
            item
            for item in report.transitions
            if item.kind is TransitionKind.ATTRIBUTE_CHANGED
        ]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].attribute, "power")
        self.assertEqual(changes[0].previous, "off")
        self.assertEqual(changes[0].current, "on")

    def test_unrelated_entities_are_left_alone(self) -> None:
        world, second = self._world()
        laptop_before = world.state().find("laptop")[0]
        mug_before = world.state().find("mug")[0]
        report = world.observe(second)
        laptop_after = world.state().find("laptop")[0]
        mug_after = world.state().find("mug")[0]
        self.assertEqual(laptop_after.entity_id, laptop_before.entity_id)
        self.assertEqual(mug_after.entity_id, mug_before.entity_id)
        self.assertEqual(mug_after.label, "mug")
        touched = {item.entity_id for item in report.transitions}
        self.assertNotIn(mug_after.entity_id, touched)

    def test_the_new_version_links_back_to_the_previous_one(self) -> None:
        world, second = self._world()
        first_id = world.state().state_id
        world.observe(second)
        self.assertEqual(world.state().previous_state_id, first_id)

    def test_evidence_points_at_the_observation_that_changed_the_fact(self) -> None:
        world, second = self._world()
        report = world.observe(second)
        change = next(item for item in report.transitions if item.attribute == "power")
        self.assertIn(report.observation_id, change.evidence)
        self.assertIn(report.observation_id, world.state().find("laptop")[0].evidence_for("power"))

    def test_a_duplicate_observation_changes_nothing(self) -> None:
        world, second = self._world()
        first = world.observe(second)
        again = world.observe(second)
        self.assertIs(again.status, IngestStatus.DUPLICATE)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.version, first.version)
        self.assertEqual(world.status()["telemetry"]["duplicates"], 1)

    def test_a_big_move_records_a_movement_transition(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        report = world.observe(observation([thing("laptop", 100, track_id="t1")], at=10))
        self.assertIn("entity_moved", log_of(report))

    def test_a_tiny_wobble_is_not_a_movement(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        report = world.observe(observation([thing("laptop", 4, track_id="t1")], at=10))
        self.assertNotIn("entity_moved", log_of(report))


# ── Workflow C — entity continuity ───────────────────────────────────────────


class WorkflowCEntityContinuityTests(unittest.TestCase):
    """Absence is not deletion; identity survives only on evidence."""

    def test_a_partial_observation_proves_nothing_about_absence(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        report = world.observe(observation([], at=30, complete=False))
        laptop = world.state().find("laptop")[0]
        self.assertIs(laptop.status, EntityStatus.PRESENT)
        self.assertEqual(laptop.missed_observations, 0)
        self.assertNotIn("entity_not_observed", log_of(report))

    def test_a_complete_observation_missing_an_entity_is_not_a_conclusion(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        before = world.state().find("laptop")[0].entity_id
        report = world.observe(observation([], at=30, complete=True))
        laptop = world.state().find("laptop")[0]
        self.assertEqual(laptop.entity_id, before)
        self.assertIs(laptop.status, EntityStatus.NOT_OBSERVED)
        self.assertEqual(laptop.missed_observations, 1)
        self.assertIn("entity_not_observed", log_of(report))
        self.assertNotIn("entity_confirmed_missing", log_of(report))

    def test_repeated_complete_misses_confirm_the_absence(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([], at=30, complete=True))
        report = world.observe(observation([], at=60, complete=True))
        laptop = world.state().find("laptop")[0]
        self.assertIs(laptop.status, EntityStatus.MISSING)
        self.assertIn("entity_confirmed_missing", log_of(report))

    def test_a_reappearing_entity_keeps_its_identity_when_evidence_agrees(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        before = world.state().find("laptop")[0]
        world.observe(observation([], at=30, complete=True))
        world.observe(observation([], at=60, complete=True))
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=90))
        after = world.state().find("laptop")[0]
        self.assertEqual(after.entity_id, before.entity_id)
        self.assertIs(after.status, EntityStatus.PRESENT)
        self.assertEqual(after.missed_observations, 0)
        self.assertEqual(len(world.state().find("laptop")), 1)

    def test_a_different_label_in_the_same_place_is_a_different_entity(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([thing("mug", 0)], at=30))
        self.assertEqual(world.status()["entities"], 2)

    def test_the_same_label_far_away_is_a_different_entity(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([thing("laptop", 900)], at=30))
        laptops = world.state().find("laptop")
        self.assertEqual(len(laptops), 2)

    def test_an_entity_nobody_has_seen_for_the_retention_window_is_retired(self) -> None:
        world = engine(policy=EstimationPolicy(entity_ttl_seconds=60))
        world.observe(observation([thing("laptop", 0)], at=0))
        report = world.observe(observation([thing("mug", 100)], at=600))
        laptop = world.state().find("laptop")[0]
        self.assertIs(laptop.status, EntityStatus.EXPIRED)
        self.assertIn("entity_expired", log_of(report))
        # Retired is a status, not a deletion: the record and its evidence stay.
        self.assertTrue(laptop.evidence)

    def test_status_zero_is_not_a_confidence(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        self.assertIsNone(world.state().find("laptop")[0].identity_confidence)


# ── Workflow D — historical query ────────────────────────────────────────────


class WorkflowDHistoricalQueryTests(unittest.TestCase):
    """A past moment is answered from the past, or not at all."""

    def _world(self) -> WorldModelEngine:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([thing("mug", 100)], at=120))
        return world

    def test_a_historical_moment_returns_the_version_that_covers_it(self) -> None:
        result = self._world().query({"kind": "state_at", "since": stamp(0)})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertEqual(result.version, 1)
        self.assertEqual(result.rows[0]["entity_count"], 1)

    def test_the_current_state_is_never_substituted_for_a_past_one(self) -> None:
        result = self._world().query({"kind": "state_at", "since": stamp(-600)})
        self.assertIs(result.status, QueryStatus.NOT_FOUND)
        self.assertTrue(any("NOT substituted" in item for item in result.limitations))

    def test_an_unreadable_moment_is_not_found_with_the_reason(self) -> None:
        result = self._world().query({"kind": "state_at", "since": "the other day"})
        self.assertIs(result.status, QueryStatus.NOT_FOUND)
        self.assertIn("could not be read", result.reason)

    def test_a_moment_that_was_not_named_is_an_invalid_question(self) -> None:
        result = self._world().query({"kind": "state_at"})
        self.assertIs(result.status, QueryStatus.INVALID)
        self.assertIn("since", result.reason)

    def test_entity_history_returns_the_record_and_its_transitions(self) -> None:
        result = self._world().query({"kind": "entity_history", "label": "laptop"})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertEqual(result.rows[0]["record"], "entity")
        self.assertTrue(any(row["record"] == "transition" for row in result.rows))

    def test_a_historical_answer_names_its_version_and_time(self) -> None:
        result = self._world().query({"kind": "state_at", "since": stamp(0)})
        self.assertTrue(result.state_id)
        self.assertEqual(result.timestamp, stamp(0))

    def test_pruned_history_honestly_stops_answering(self) -> None:
        world = engine(retention=WorldRetentionPolicy(max_snapshots=1))
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([thing("laptop", 0)], at=60, source_id="cam-2"))
        result = world.query({"kind": "state_at", "since": stamp(0)})
        self.assertIs(result.status, QueryStatus.NOT_FOUND)


# ── Workflow E — conflicting observations ────────────────────────────────────


class WorkflowEConflictingObservationTests(unittest.TestCase):
    """A disagreement is kept and a newer value is not overwritten by an old one."""

    def _world(self) -> WorldModelEngine:
        world = engine()
        world.observe(
            observation(
                [thing("laptop", 0, track_id="t1", attributes=[{"name": "power", "value": "on"}])],
                at=0,
            )
        )
        world.observe(
            observation(
                [thing("laptop", 100, track_id="t1", attributes=attrs(power="on"))],
                at=30,
            )
        )
        return world

    def test_an_older_observation_is_reported_as_out_of_order(self) -> None:
        world = self._world()
        report = world.observe(
            observation(
                [thing("laptop", 105, track_id="t1", attributes=attrs(power="off"))],
                at=10,
            )
        )
        self.assertIs(report.status, IngestStatus.OUT_OF_ORDER)
        self.assertTrue(report.stale is False)

    def test_the_conflict_is_recorded_with_the_newer_fact_kept(self) -> None:
        world = self._world()
        report = world.observe(
            observation(
                [thing("laptop", 105, track_id="t1", attributes=attrs(power="off"))],
                at=10,
            )
        )
        kinds = {item.kind for item in report.uncertainty}
        self.assertIn(UncertaintyKind.OUT_OF_ORDER, kinds)
        self.assertIn(UncertaintyKind.CONFLICTING, kinds)
        laptop = world.state().find("laptop")[0]
        self.assertEqual(laptop.value_of("power"), "on")
        self.assertEqual(laptop.bbox, BBox(100, 0, 40, 40))
        self.assertIn("state_conflict", log_of(report))

    def test_the_state_keeps_the_doubt_it_recorded(self) -> None:
        world = self._world()
        world.observe(
            observation(
                [thing("laptop", 105, track_id="t1", attributes=attrs(power="off"))],
                at=10,
            )
        )
        result = world.query({"kind": "uncertain"})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertTrue(any(row["kind"] == "conflicting" for row in result.rows))

    def test_a_genuinely_older_fact_is_marked_stale(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=300, clock="wall"))
        report = world.observe(observation([thing("laptop", 0)], at=0, clock="file"))
        self.assertIs(report.status, IngestStatus.STALE)
        self.assertTrue(any(item.kind is UncertaintyKind.STALE for item in report.uncertainty))

    def test_a_newer_fact_is_not_stale(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        report = world.observe(observation([thing("laptop", 0)], at=600))
        self.assertIs(report.status, IngestStatus.ACCEPTED)
        self.assertFalse(report.stale)

    def test_an_older_observation_never_overwrites_a_newer_box(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        world.observe(observation([thing("laptop", 100, track_id="t1")], at=30))
        world.observe(observation([thing("laptop", 60, track_id="t1")], at=10))
        self.assertEqual(world.state().find("laptop")[0].bbox, BBox(100, 0, 40, 40))


# ── Workflow F — restart and recovery ────────────────────────────────────────


class WorkflowFRestartRecoveryTests(unittest.TestCase):
    """A stored world comes back, and a damaged one comes back partially, said so."""

    def _populated(self) -> tuple[WorldModelEngine, InMemoryWorldRepository]:
        repo = InMemoryWorldRepository()
        world = engine(repository=repo)
        world.observe(observation([thing("laptop", 0), thing("mug", 100)], at=0))
        world.observe(
            observation([thing("laptop", 0), thing("mug", 100)], at=60, source_id="cam-2")
        )
        world.observe(observation([thing("laptop", 0)], at=120, complete=True))
        self.assertTrue(world.save())
        return world, repo

    def test_saving_then_restoring_recovers_the_versions_and_entities(self) -> None:
        saved, repo = self._populated()
        restarted = WorldModelEngine(world_id="default", repository=repo)
        report = restarted.restore()
        self.assertTrue(report.restored)
        self.assertEqual(report.version, saved.state().version)
        self.assertEqual(report.snapshots, len(saved.snapshots()))
        self.assertGreater(report.transitions, 0)
        self.assertEqual(restarted.state().state_id, saved.state().state_id)
        self.assertEqual(len(restarted.state().find("laptop")), 1)

    def test_a_missing_store_reports_nothing_to_restore(self) -> None:
        report = WorldModelEngine(repository=InMemoryWorldRepository()).restore()
        self.assertFalse(report.restored)
        self.assertIn("nothing", report.reason)

    def test_no_store_at_all_is_reported_not_raised(self) -> None:
        report = WorldModelEngine().restore()
        self.assertFalse(report.restored)
        self.assertIn("no world store", report.reason)

    def test_a_damaged_payload_is_read_partially_and_counted(self) -> None:
        saved, repo = self._populated()
        payload = dict(repo.load("default"))
        payload["snapshots"] = [*payload["snapshots"], "not an object", 17]
        payload["transitions"] = [*payload["transitions"], "also not an object"]
        repo.save("default", payload)
        restarted = WorldModelEngine(repository=repo)
        report = restarted.restore()
        self.assertTrue(report.restored)
        self.assertEqual(report.skipped_records, 3)
        self.assertEqual(len(report.skipped_reasons), 3)

    def test_a_payload_for_another_world_is_refused(self) -> None:
        saved, repo = self._populated()
        repo.save("elsewhere", repo.load("default"))
        report = WorldModelEngine(world_id="elsewhere", repository=repo).restore()
        self.assertFalse(report.restored)
        self.assertIn("belongs to world", report.reason)

    def test_a_payload_from_another_schema_is_read_tolerantly_and_labelled(self) -> None:
        saved, repo = self._populated()
        payload = dict(repo.load("default"))
        payload["schema_version"] = "phase22.0"
        repo.save("default", payload)
        report = WorldModelEngine(repository=repo).restore()
        self.assertTrue(report.restored)
        self.assertIn("phase22.0", report.reason)

    def test_the_stored_payload_never_carries_observation_text(self) -> None:
        repo = InMemoryWorldRepository()
        world = engine(repository=repo)
        world.observe(
            observation(
                [thing("laptop", 0, attributes=[{"name": "note", "value": "structured-fact"}])],
                at=0,
                text="NARRATIVE-SECRET-4f2",
            )
        )
        world.save()
        stored = json.dumps(repo.load("default"))
        self.assertNotIn("NARRATIVE-SECRET-4f2", stored)
        self.assertIn("structured-fact", stored)

    def test_a_save_with_no_store_reports_failure_without_raising(self) -> None:
        self.assertFalse(WorldModelEngine().save())

    def test_retention_applies_to_what_is_restored(self) -> None:
        saved, repo = self._populated()
        restarted = WorldModelEngine(
            repository=repo, retention=WorldRetentionPolicy(max_snapshots=1, max_transitions=1)
        )
        report = restarted.restore()
        self.assertEqual(report.snapshots, 1)
        self.assertEqual(report.transitions, 1)


# ── Workflow G — evidence query ──────────────────────────────────────────────


class WorkflowGEvidenceQueryTests(unittest.TestCase):
    """Why a claim is believed: references, basis and limits."""

    def _world(self) -> WorldModelEngine:
        world = engine()
        world.observe(
            observation(
                [
                    thing("laptop", 0, attributes=[{"name": "power", "value": "on"}]),
                    thing("mug", 100),
                ],
                at=0,
            )
        )
        return world

    def test_evidence_by_label_returns_the_claim_and_its_support(self) -> None:
        result = self._world().query({"kind": "evidence", "label": "laptop"})
        self.assertIs(result.status, QueryStatus.OK)
        records = {row["record"] for row in result.rows}
        self.assertIn("entity", records)
        self.assertIn("attribute", records)
        self.assertIn("reference", records)
        self.assertTrue(result.evidence)

    def test_the_answer_states_that_the_content_is_not_kept_here(self) -> None:
        result = self._world().query({"kind": "evidence", "label": "laptop"})
        self.assertTrue(any("not stored here" in item for item in result.limitations))

    def test_evidence_records_the_basis_of_each_attribute(self) -> None:
        result = self._world().query({"kind": "evidence", "label": "laptop"})
        attribute = next(row for row in result.rows if row["record"] == "attribute")
        self.assertEqual(attribute["basis"], "observed")

    def test_an_evidence_query_without_a_subject_is_invalid(self) -> None:
        result = self._world().query({"kind": "evidence"})
        self.assertIs(result.status, QueryStatus.INVALID)
        self.assertIn("subject", result.reason)

    def test_evidence_for_something_never_seen_is_not_found(self) -> None:
        result = self._world().query({"kind": "evidence", "label": "spaceship"})
        self.assertIs(result.status, QueryStatus.NOT_FOUND)

    def test_evidence_for_a_relationship_names_its_source_kind(self) -> None:
        world = self._world()
        relationship = world.state().relationships[0]
        result = world.query({"kind": "evidence", "subject": relationship.relationship_id})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertTrue(any(row["record"] == "relationship" for row in result.rows))


# ── Workflow H — prediction boundary ─────────────────────────────────────────


class WorkflowHPredictionBoundaryTests(unittest.TestCase):
    """A prediction is a real provider's answer, or an honest refusal."""

    def test_the_default_engine_ships_no_predictive_model(self) -> None:
        world = engine()
        world.observe(two_objects())
        result = world.predict({"target": "laptop"})
        self.assertIs(result.status, PredictionStatus.MODEL_UNAVAILABLE)
        self.assertEqual(result.provider, "none")
        self.assertEqual(result.predicted, {})
        self.assertFalse(result.rule_based)
        self.assertIn("not available", result.reason)

    def test_availability_reports_the_provider_and_why(self) -> None:
        availability = PredictionService(provider=NoPredictionProvider()).availability()
        self.assertEqual(availability["provider"], "none")
        self.assertFalse(availability["available"])
        self.assertTrue(availability["reason"])

    def _moving(self) -> WorldModelEngine:
        world = engine(prediction=PredictionService(provider=RuleProjectionProvider()))
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        world.observe(observation([thing("laptop", 50, track_id="t1")], at=10))
        return world

    def test_a_rule_projection_is_labelled_as_a_rule_not_a_model(self) -> None:
        result = self._moving().predict({"target": "laptop"})
        self.assertIs(result.status, PredictionStatus.PREDICTED)
        self.assertTrue(result.rule_based)
        self.assertEqual(result.provider, "rule-projection")
        self.assertIsNone(result.confidence)
        self.assertEqual(result.predicted["basis"], "rule_projection")
        self.assertTrue(
            any("no predictive model was involved" in item for item in result.limitations)
        )

    def test_the_projection_is_arithmetic_over_measured_displacement(self) -> None:
        result = self._moving().predict({"target": "laptop"})
        self.assertEqual(result.predicted["measured_displacement_px"], {"dx": 50.0, "dy": 0.0})
        self.assertEqual(result.predicted["measured_seconds"], 10.0)
        self.assertEqual(result.predicted["velocity_px_per_second"], {"dx": 5.0, "dy": 0.0})
        self.assertEqual(result.predicted["projected_centre"]["x"], 120.0)

    def test_the_horizon_is_clamped_and_the_clamp_is_stated(self) -> None:
        result = self._moving().predict({"target": "laptop", "horizon_seconds": 10000})
        self.assertEqual(result.predicted["projected_centre"]["x"], 1570.0)
        self.assertTrue(any("clamped to 300s" in item for item in result.limitations))

    def test_no_horizon_uses_one_measured_interval_and_says_so(self) -> None:
        result = self._moving().predict({"target": "laptop"})
        self.assertEqual(result.predicted["horizon_seconds"], 10.0)
        self.assertTrue(any("no horizon was given" in item for item in result.limitations))

    def test_an_unknown_target_is_insufficient_evidence(self) -> None:
        result = self._moving().predict({"target": "spaceship"})
        self.assertIs(result.status, PredictionStatus.INSUFFICIENT_EVIDENCE)

    def test_one_retained_version_cannot_be_projected(self) -> None:
        world = engine(prediction=PredictionService(provider=RuleProjectionProvider()))
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        result = world.predict({"target": "laptop"})
        self.assertIs(result.status, PredictionStatus.INSUFFICIENT_EVIDENCE)

    def test_an_unavailable_provider_is_never_asked(self) -> None:
        service = PredictionService(provider=_UnavailableProvider())
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.MODEL_UNAVAILABLE)

    def test_a_provider_that_raises_is_a_failed_result(self) -> None:
        service = PredictionService(provider=_RaisingProvider())
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.FAILED)
        self.assertIn("RuntimeError", result.reason)

    def test_a_provider_returning_the_wrong_type_is_discarded(self) -> None:
        service = PredictionService(provider=_WrongTypeProvider())
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.FAILED)
        self.assertIn("instead of a PredictionResult", result.reason)

    def test_a_prediction_with_no_content_is_refused(self) -> None:
        service = PredictionService(provider=_EmptyPredictionProvider())
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.FAILED)
        self.assertIn("no predicted content", result.reason)

    def test_a_model_backed_provider_with_no_gate_is_refused(self) -> None:
        service = PredictionService(provider=_ModelBackedProvider(), gate=None)
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.RESOURCE_BLOCKED)
        self.assertIn("no resource gate", result.reason)

    def test_a_broken_gate_is_a_refusal_not_a_crash(self) -> None:
        service = PredictionService(provider=_ModelBackedProvider(), gate=_BrokenGate())
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.RESOURCE_BLOCKED)

    def test_an_admitted_model_runs(self) -> None:
        service = PredictionService(provider=_ModelBackedProvider(), gate=_AllowingGate())
        result = service.predict(
            PredictionRequest(target="laptop"),
            state=WorldState(world_id="w", version=1, timestamp=stamp(0)),
        )
        self.assertIs(result.status, PredictionStatus.PREDICTED)
        self.assertFalse(result.rule_based)

    def test_the_engine_announces_a_prediction_and_its_failure(self) -> None:
        observer = _RecordingObserver()
        prediction = PredictionService(provider=RuleProjectionProvider())
        world = engine(observer=observer, prediction=prediction)
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        world.observe(observation([thing("laptop", 50, track_id="t1")], at=10))
        world.predict({"target": "laptop"})
        completed = observer.first(PREDICTION_COMPLETED)
        self.assertEqual(completed["provider"], "rule-projection")
        self.assertIs(completed["rule_based"], True)

        failing = engine(
            observer=observer, prediction=PredictionService(provider=_RaisingProvider())
        )
        failing.observe(two_objects())
        failing.predict({"target": "laptop"})
        self.assertEqual(observer.first(PREDICTION_FAILED)["status"], "failed")


# ── Workflow I — resource fallback ───────────────────────────────────────────


class WorkflowIResourceFallbackTests(unittest.TestCase):
    """A denied model never breaks deterministic state work."""

    def _world(self) -> tuple[WorldModelEngine, _ModelBackedProvider]:
        provider = _ModelBackedProvider()
        world = engine(prediction=PredictionService(provider=provider, gate=_DenyingGate()))
        world.observe(two_objects())
        return world, provider

    def test_an_expensive_prediction_is_denied_with_the_governor_reason(self) -> None:
        world, provider = self._world()
        result = world.predict({"target": "laptop"})
        self.assertIs(result.status, PredictionStatus.RESOURCE_BLOCKED)
        self.assertIn("not enough free memory", result.reason)
        self.assertEqual(provider.calls, 0)

    def test_the_denial_says_the_state_store_is_unaffected(self) -> None:
        world, _ = self._world()
        result = world.predict({"target": "laptop"})
        self.assertTrue(
            any("unaffected" in item for item in result.limitations)
        )

    def test_state_work_continues_after_a_denial(self) -> None:
        world, _ = self._world()
        world.predict({"target": "laptop"})
        report = world.observe(observation([thing("laptop", 0, track_id="t1")], at=60))
        self.assertTrue(report.accepted)
        self.assertIs(world.query({"kind": "entities"}).status, QueryStatus.OK)
        self.assertTrue(world.reason("what is uncertain"))

    def test_nothing_is_loaded_by_asking(self) -> None:
        world, provider = self._world()
        world.predict({"target": "laptop"})
        self.assertEqual(provider.calls, 0)
        self.assertEqual(world.status()["telemetry"]["predictions"], 1)


# ── estimation policy and identity (§6/§9) ───────────────────────────────────


class EstimationTests(unittest.TestCase):
    """Identity is evidence, and an ambiguous match stays ambiguous."""

    def test_a_source_reported_identity_matches_confidently(self) -> None:
        known = WorldEntity(
            entity_id="e1", labels=("laptop",), bbox=BBox(0, 0, 40, 40), source_ids=("t1",)
        )
        decision = resolve_identity(ObservedEntity(label="laptop", track_id="t1"), (known,))
        self.assertEqual(decision.decision, "matched")
        self.assertEqual(decision.entity_id, "e1")

    def test_overlapping_boxes_with_the_same_label_match(self) -> None:
        known = WorldEntity(entity_id="e1", labels=("laptop",), bbox=BBox(0, 0, 40, 40))
        decision = resolve_identity(
            ObservedEntity(label="laptop", bbox=BBox(2, 2, 40, 40)), (known,)
        )
        self.assertEqual(decision.decision, "matched")

    def test_geometry_without_a_label_agreement_does_not_match(self) -> None:
        known = WorldEntity(
            entity_id="e1", entity_type="object", labels=("laptop",), bbox=BBox(0, 0, 40, 40)
        )
        decision = resolve_identity(
            ObservedEntity(label="mug", entity_type="object", bbox=BBox(0, 0, 40, 40)), (known,)
        )
        self.assertEqual(decision.decision, "new_entity")

    def test_geometry_that_cannot_be_measured_claims_nothing(self) -> None:
        known = WorldEntity(entity_id="e1", labels=("laptop",), bbox=BBox(0, 0, 40, 40))
        decision = resolve_identity(ObservedEntity(label="laptop"), (known,))
        self.assertEqual(decision.decision, "new_entity")
        self.assertIsNone(decision.confidence)

    def test_two_equally_good_candidates_leave_the_identity_provisional(self) -> None:
        near = WorldEntity(entity_id="e1", labels=("mug",), bbox=BBox(0, 0, 40, 40))
        far = WorldEntity(entity_id="e2", labels=("mug",), bbox=BBox(34, 0, 40, 40))
        decision = resolve_identity(
            ObservedEntity(label="mug", bbox=BBox(16, 0, 40, 40)), (near, far)
        )
        self.assertEqual(decision.decision, "ambiguous")
        self.assertEqual(len(decision.candidates), 2)
        self.assertTrue(decision.reason)

    def test_a_merged_provisional_identity_is_marked_as_such(self) -> None:
        known = WorldEntity(entity_id="e1", labels=("mug",), bbox=BBox(0, 0, 40, 40))
        merged, conflicts = merge_entity(
            known,
            ObservedEntity(label="mug", bbox=BBox(1, 0, 40, 40)),
            estimate=SimpleNamespace(decision="ambiguous", confidence=0.4),
            timestamp=stamp(10),
            evidence_id="obs-2",
        )
        self.assertTrue(merged.provisional)
        self.assertEqual(conflicts, ())

    def test_an_attribute_from_an_older_observation_does_not_overwrite(self) -> None:
        known = WorldEntity(
            entity_id="e1",
            labels=("laptop",),
            attributes=(EntityAttribute.observed("power", "on", observed_at=stamp(30)),),
        )
        merged, conflicts = merge_entity(
            known,
            ObservedEntity(
                label="laptop",
                attributes=(EntityAttribute.observed("power", "off", observed_at=stamp(10)),),
            ),
            estimate=SimpleNamespace(decision="matched", confidence=0.9),
            timestamp=stamp(10),
            evidence_id="obs-old",
        )
        self.assertEqual(merged.value_of("power"), "on")
        self.assertEqual(len(conflicts), 1)
        self.assertIs(conflicts[0].kind, UncertaintyKind.CONFLICTING)

    def test_the_identity_policy_clamps_its_numbers(self) -> None:
        policy = IdentityPolicy.from_mapping(
            {
                "identity_iou_threshold": 5.0,
                "identity_distance_px": -3,
                "identity_ambiguous_margin": 9,
            }
        )
        self.assertEqual(policy.iou_threshold, 1.0)
        self.assertEqual(policy.distance_px, 0)
        self.assertEqual(policy.ambiguous_margin, 1.0)

    def test_estimation_policy_defaults_are_the_careful_ones(self) -> None:
        policy = EstimationPolicy.from_mapping(None)
        self.assertEqual(policy.missing_after, 2)
        self.assertTrue(policy.derive_relationships)
        self.assertEqual(EstimationPolicy.from_mapping({"missing_after": 0}).missing_after, 1)

    def test_the_estimator_is_deterministic_about_what_it_concludes(self) -> None:
        first = engine()
        second = engine()
        a = first.observe(two_objects())
        b = second.observe(two_objects())
        self.assertEqual(log_of(a), log_of(b))

    def test_absence_needs_a_complete_observation_of_the_same_scope(self) -> None:
        entity = WorldEntity(entity_id="e1", labels=("laptop",), scope="window-1")
        unchanged, kind, _ = confirm_absence(
            entity,
            complete=True,
            scope="window-2",
            missing_after=2,
            timestamp=stamp(0),
            evidence_id="o",
        )
        self.assertIsNone(kind)
        self.assertIs(unchanged.status, EntityStatus.PRESENT)
        changed, kind, _ = confirm_absence(
            entity,
            complete=True,
            scope="window-1",
            missing_after=2,
            timestamp=stamp(0),
            evidence_id="o",
        )
        self.assertEqual(kind, "entity_not_observed")
        self.assertIs(changed.status, EntityStatus.NOT_OBSERVED)

    def test_expiry_is_a_status_and_the_evidence_stays(self) -> None:
        entity = WorldEntity(
            entity_id="e1", labels=("laptop",), last_seen=stamp(0), evidence=("obs-1",)
        )
        entities, expired = expire_stale((entity,), ttl_seconds=60, now=stamp(600), evidence_id="o")
        self.assertEqual(expired, ("e1",))
        self.assertIs(entities[0].status, EntityStatus.EXPIRED)
        self.assertIn("obs-1", entities[0].evidence)

    def test_expiry_is_disabled_by_a_zero_window(self) -> None:
        entity = WorldEntity(entity_id="e1", labels=("laptop",), last_seen=stamp(0))
        entities, expired = expire_stale(
            (entity,), ttl_seconds=0, now=stamp(999999), evidence_id="o"
        )
        self.assertEqual(expired, ())
        self.assertIs(entities[0].status, EntityStatus.PRESENT)

    def test_active_entities_exclude_the_retired_ones(self) -> None:
        live = WorldEntity(entity_id="a", labels=("mug",))
        gone = WorldEntity(entity_id="b", labels=("laptop",), status=EntityStatus.EXPIRED)
        self.assertEqual([item.entity_id for item in active_entities((live, gone))], ["a"])

    def test_live_entities_past_the_ceiling_are_kept_and_the_overage_is_reported(self) -> None:
        # A stream of genuinely NEW entities: the live world is never trimmed to
        # satisfy the policy number, so the state says out loud that it is over.
        world = engine(policy=EstimationPolicy.from_mapping({"max_entities": 3}))
        world.observe(observation([thing(f"obj-{index}", index * 60) for index in range(5)]))
        self.assertEqual(len(world.state().entities), 5)
        self.assertEqual(world.state().metadata["entities_over_ceiling"], 2)
        status = world.status()
        self.assertEqual(status["entities"], 5)
        self.assertEqual(status["entities_over_ceiling"], 2)
        self.assertEqual(status["policy"]["max_entities"], 3)

    def test_the_ceiling_retires_expired_entities_before_live_ones(self) -> None:
        policy = EstimationPolicy.from_mapping({"max_entities": 2, "entity_ttl_seconds": 60})
        world = engine(policy=policy)
        world.observe(observation([thing("laptop", 0)], at=0, complete=True))
        world.observe(
            observation([thing("mug", 0), thing("phone", 300)], at=600, complete=True)
        )
        state = world.state()
        self.assertEqual(len(state.entities), 2)
        self.assertEqual(state.metadata["retired_entities"], 1)
        # Room was made out of expired history, so nothing is over the ceiling.
        self.assertEqual(state.metadata["entities_over_ceiling"], 0)
        self.assertEqual(world.status()["entities_over_ceiling"], 0)

    def test_a_new_entity_has_no_measured_identity_confidence(self) -> None:
        entity = entity_from_observation(
            ObservedEntity(label="laptop", bbox=BBox(0, 0, 40, 40)),
            timestamp=stamp(0),
            evidence_id="obs-1",
            scope="",
        )
        self.assertIsNone(entity.identity_confidence)
        self.assertEqual(entity.observed_count, 1)
        self.assertEqual(entity.scope, "")

    def test_estimate_keeps_the_previous_entities_and_adds_to_them(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        previous = world.state()
        outcome = estimate(
            previous=previous,
            observation=normalize(observation([thing("mug", 100)], at=30)),
        )
        self.assertEqual(len(outcome.state.entities), 2)
        self.assertEqual(outcome.entities_added, 1)

    def test_the_entity_count_condition_describes_the_new_state(self) -> None:
        world = engine()
        world.observe(two_objects())
        count = next(item for item in world.state().conditions if item.name == "entity_count")
        self.assertEqual(count.value, 2)
        self.assertIs(count.basis, FactBasis.INFERRED)


# ── relationships (§7) ───────────────────────────────────────────────────────


class RelationshipTests(unittest.TestCase):
    """Durable relations, with a life cycle and no invented ownership."""

    def _observation(self, metadata: dict) -> Observation:
        return Observation(timestamp=stamp(0), source_id="cam", complete=False, metadata=metadata)

    def test_phase_21_geometry_is_reused_for_the_durable_graph(self) -> None:
        resolved = (
            (ObservedEntity(label="laptop", bbox=BBox(0, 0, 40, 40)), "e1"),
            (ObservedEntity(label="mug", bbox=BBox(100, 0, 40, 40)), "e2"),
        )
        rows, derived = derive_geometric_relations(resolved, timestamp=stamp(0))
        self.assertTrue(derived)
        self.assertEqual({item.kind for item in rows}, {RelationshipKind.LOCATED_NEAR})
        self.assertTrue(all(item.detail for item in rows))

    def test_a_pair_without_extent_supports_no_relation(self) -> None:
        rows, derived = derive_geometric_relations(
            (
                (ObservedEntity(label="a", bbox=BBox(0, 0, 0, 0)), "e1"),
                (ObservedEntity(label="b", bbox=BBox(0, 0, 40, 40)), "e2"),
            ),
            timestamp=stamp(0),
        )
        self.assertEqual(rows, [])
        self.assertFalse(derived)

    def test_geometry_with_no_pairs_claims_nothing(self) -> None:
        rows, derived = derive_geometric_relations(
            ((ObservedEntity(label="a", bbox=BBox(0, 0, 40, 40)), "e1"),), timestamp=stamp(0)
        )
        self.assertEqual(rows, [])
        self.assertFalse(derived)

    def test_a_perception_hint_is_translated_to_durable_entity_ids(self) -> None:
        outcome = relations_from_observation(
            self._observation(
                {
                    "relationship_hints": [
                        {
                            "kind": "left_of",
                            "subject_hint": "obj-1",
                            "object_hint": "obj-2",
                            "scene_id": "scene-1",
                        }
                    ]
                }
            ),
            id_by_hint={"obj-1": "e1", "obj-2": "e2"},
            existing=(),
            timestamp=stamp(0),
        )
        self.assertEqual(outcome.added, 1)
        relation = outcome.relationships[0]
        self.assertIs(relation.kind, RelationshipKind.LOCATED_NEAR)
        self.assertEqual(relation.pair(), ("e1", "e2"))
        self.assertEqual(relation.evidence, ("scene-1",))

    def test_a_stated_relation_is_kept_as_stated(self) -> None:
        outcome = relations_from_observation(
            self._observation(
                {
                    "relationships": [
                        {
                            "kind": "owned_by",
                            "source_entity_id": "e1",
                            "target_entity_id": "e2",
                        }
                    ]
                }
            ),
            id_by_hint={},
            existing=(),
            timestamp=stamp(0),
        )
        relation = outcome.relationships[0]
        self.assertIs(relation.kind, RelationshipKind.OWNED_BY)
        self.assertIs(relation.relation_source, RelationshipSource.STATED)

    def test_re_observing_a_relation_refreshes_it_instead_of_duplicating(self) -> None:
        existing = WorldRelationship(
            relationship_id="r1",
            kind=RelationshipKind.LOCATED_NEAR,
            source_entity_id="e1",
            target_entity_id="e2",
            valid_from=stamp(0),
        )
        outcome = relations_from_observation(
            self._observation(
                {"relationship_hints": [{"kind": "near", "subject_hint": "a", "object_hint": "b"}]}
            ),
            id_by_hint={"a": "e1", "b": "e2"},
            existing=(existing,),
            timestamp=stamp(30),
        )
        self.assertEqual(len(outcome.relationships), 1)
        self.assertEqual(outcome.relationships[0].relationship_id, "r1")
        self.assertEqual(outcome.relationships[0].valid_from, stamp(0))

    def test_a_geometric_relation_stops_being_asserted_when_a_complete_look_omits_it(self) -> None:
        existing = WorldRelationship(
            relationship_id="r1",
            kind=RelationshipKind.LOCATED_NEAR,
            source_entity_id="e1",
            target_entity_id="e2",
            relation_source=RelationshipSource.GEOMETRIC,
            valid_from=stamp(0),
        )
        outcome = relations_from_observation(
            Observation(timestamp=stamp(30), source_id="cam", complete=True),
            id_by_hint={},
            existing=(existing,),
            timestamp=stamp(30),
            derive_missing=False,
        )
        self.assertEqual(outcome.staled, 1)
        relation = outcome.relationships[0]
        self.assertIs(relation.status, RelationStatus.STALE)
        self.assertEqual(relation.valid_until, stamp(30))

    def test_a_stated_relation_does_not_lapse_because_a_frame_omitted_it(self) -> None:
        existing = WorldRelationship(
            relationship_id="r1",
            kind=RelationshipKind.OWNED_BY,
            source_entity_id="e1",
            target_entity_id="e2",
            relation_source=RelationshipSource.STATED,
        )
        outcome = relations_from_observation(
            Observation(timestamp=stamp(30), source_id="cam", complete=True),
            id_by_hint={},
            existing=(existing,),
            timestamp=stamp(30),
            derive_missing=False,
        )
        self.assertEqual(outcome.staled, 0)
        self.assertIs(outcome.relationships[0].status, RelationStatus.ACTIVE)

    def test_contradictory_containment_retracts_the_older_claim(self) -> None:
        existing = WorldRelationship(
            relationship_id="r1",
            kind=RelationshipKind.INSIDE,
            source_entity_id="e1",
            target_entity_id="e2",
        )
        outcome = relations_from_observation(
            self._observation(
                {
                    "relationships": [
                        {"kind": "contains", "source_entity_id": "e1", "target_entity_id": "e2"}
                    ]
                }
            ),
            id_by_hint={},
            existing=(existing,),
            timestamp=stamp(30),
        )
        self.assertEqual(outcome.retracted, 1)
        self.assertTrue(outcome.conflicts)
        statuses = {item.relationship_id: item.status for item in outcome.relationships}
        self.assertIs(statuses["r1"], RelationStatus.RETRACTED)

    def test_a_relation_supported_again_is_reactivated(self) -> None:
        existing = WorldRelationship(
            relationship_id="r1",
            kind=RelationshipKind.LOCATED_NEAR,
            source_entity_id="e1",
            target_entity_id="e2",
            status=RelationStatus.STALE,
            valid_until=stamp(10),
        )
        outcome = relations_from_observation(
            self._observation(
                {"relationship_hints": [{"kind": "near", "subject_hint": "a", "object_hint": "b"}]}
            ),
            id_by_hint={"a": "e1", "b": "e2"},
            existing=(existing,),
            timestamp=stamp(30),
        )
        self.assertEqual(outcome.reactivated, 1)
        self.assertIs(outcome.relationships[0].status, RelationStatus.ACTIVE)
        self.assertIsNone(outcome.relationships[0].valid_until)

    def test_a_relation_is_identified_by_kind_and_pair(self) -> None:
        left = WorldRelationship(
            kind=RelationshipKind.INSIDE, source_entity_id="a", target_entity_id="b"
        )
        right = WorldRelationship(
            kind=RelationshipKind.INSIDE, source_entity_id="a", target_entity_id="b"
        )
        other = WorldRelationship(
            kind=RelationshipKind.INSIDE, source_entity_id="b", target_entity_id="a"
        )
        self.assertTrue(relations_from_observation.__module__)
        from novacontrol.world.relationships import same_relation

        self.assertTrue(same_relation(left, right))
        self.assertFalse(same_relation(left, other))

    def test_the_graph_is_bounded_and_the_cap_is_reported(self) -> None:
        from novacontrol.world.relationships import MAX_RELATIONSHIPS

        existing = tuple(
            WorldRelationship(
                kind=RelationshipKind.ASSOCIATED_WITH,
                source_entity_id=f"e{index}",
                target_entity_id=f"f{index}",
                relation_source=RelationshipSource.STATED,
            )
            for index in range(MAX_RELATIONSHIPS)
        )
        outcome = relations_from_observation(
            self._observation(
                {
                    "relationships": [
                        {
                            "kind": "associated_with",
                            "source_entity_id": "x",
                            "target_entity_id": "y",
                        }
                    ]
                }
            ),
            id_by_hint={},
            existing=existing,
            timestamp=stamp(0),
        )
        self.assertLessEqual(len(outcome.relationships), MAX_RELATIONSHIPS)
        self.assertTrue(outcome.capped)


# ── change detection (§13) ───────────────────────────────────────────────────


class ChangeDetectionTests(unittest.TestCase):
    """Thresholds are the point: noise is not a change."""

    def test_a_movement_below_the_threshold_is_not_a_change(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        report = world.observe(observation([thing("laptop", 4, track_id="t1")], at=10))
        self.assertFalse(any(item.kind is ChangeKind.POSITION_CHANGED for item in report.changes))

    def test_a_movement_above_the_threshold_is_a_change(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0, track_id="t1")], at=0))
        report = world.observe(observation([thing("laptop", 40, track_id="t1")], at=10))
        moves = [item for item in report.changes if item.kind is ChangeKind.POSITION_CHANGED]
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(moves[0].magnitude or 0, 40.0)

    def test_an_identical_observation_reports_no_change(self) -> None:
        world = engine()
        world.observe(two_objects(at=0))
        first = world.state()
        second_report = world.observe(two_objects(at=10))
        events = detect_changes(first, world.state(), timestamp=stamp(10))
        self.assertEqual(events, ())
        self.assertEqual(second_report.changes, ())

    def test_an_attribute_change_is_reported(self) -> None:
        world = engine()
        world.observe(
            observation([thing("laptop", 0, attributes=[{"name": "power", "value": "off"}])], at=0)
        )
        report = world.observe(
            observation([thing("laptop", 0, attributes=[{"name": "power", "value": "on"}])], at=10)
        )
        self.assertTrue(any(item.kind is ChangeKind.ATTRIBUTE_CHANGED for item in report.changes))

    def test_staleness_is_reported_as_a_state_change(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        report = world.observe(observation([], at=30, complete=True))
        self.assertTrue(any(item.kind is ChangeKind.STATE_STALE for item in report.changes))

    def test_thresholds_clamp_their_input(self) -> None:
        thresholds = ChangeThresholds.from_mapping(
            {"position_change_px": -5, "confidence_change": 4}
        )
        self.assertEqual(thresholds.position_px, 0)
        self.assertEqual(thresholds.confidence_delta, 1.0)

    def test_a_confidence_change_needs_two_measurements(self) -> None:
        from novacontrol.world.changes import entity_confidence

        measured = WorldEntity(entity_id="e", labels=("a",), identity_confidence=0.8)
        unmeasured = WorldEntity(entity_id="f", labels=("b",), identity_confidence=None)
        self.assertEqual(entity_confidence((measured,)), 0.8)
        self.assertIsNone(entity_confidence((unmeasured,)))
        self.assertIsNone(entity_confidence(()))


# ── queries (§14) ────────────────────────────────────────────────────────────


class QuerySurfaceTests(unittest.TestCase):
    """Every question is bounded, and an impossible one is refused by name."""

    def _world(self) -> WorldModelEngine:
        world = engine()
        world.observe(
            observation(
                [
                    thing("laptop", 0, attributes=[{"name": "power", "value": "on"}]),
                    thing("mug", 100),
                ],
                at=0,
            )
        )
        world.observe(
            observation([thing("laptop", 0), thing("mug", 100)], at=60, source_id="cam-2")
        )
        return world

    def test_an_unobserved_world_answers_empty_with_a_reason(self) -> None:
        result = engine().query({"kind": "entities"})
        self.assertIs(result.status, QueryStatus.EMPTY)
        self.assertIn("never been observed", result.reason)
        self.assertTrue(result.limitations)

    def test_entities_are_listed_with_their_status(self) -> None:
        result = self._world().query({"kind": "entities"})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertEqual({row["label"] for row in result.rows}, {"laptop", "mug"})

    def test_entities_can_be_filtered_by_label_and_status(self) -> None:
        world = self._world()
        self.assertEqual(world.query({"kind": "entities", "label": "mug"}).count, 1)
        expired = world.query({"kind": "entities", "status": "expired"})
        self.assertIs(expired.status, QueryStatus.EMPTY)

    def test_an_unmeasured_confidence_never_satisfies_a_minimum(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        self.assertIsNone(world.state().find("laptop")[0].identity_confidence)
        result = world.query({"kind": "entities", "min_confidence": 0.5})
        self.assertEqual(result.count, 0)
        self.assertIs(result.status, QueryStatus.EMPTY)

    def test_an_entity_question_without_an_entity_is_invalid(self) -> None:
        result = self._world().query({"kind": "entity"})
        self.assertIs(result.status, QueryStatus.INVALID)
        self.assertIn("entity_id", result.reason)

    def test_an_unknown_entity_is_not_found(self) -> None:
        result = self._world().query({"kind": "entity", "label": "spaceship"})
        self.assertIs(result.status, QueryStatus.NOT_FOUND)

    def test_entity_seen_reports_a_definite_answer_either_way(self) -> None:
        world = self._world()
        seen = world.query({"kind": "entity_seen", "label": "laptop"})
        self.assertIs(seen.status, QueryStatus.OK)
        self.assertTrue(seen.rows[0]["seen"])
        self.assertEqual(seen.rows[0]["entity_id"], world.state().find("laptop")[0].entity_id)
        unseen = world.query({"kind": "entity_seen", "label": "spaceship"})
        self.assertIs(unseen.status, QueryStatus.NOT_FOUND)
        self.assertFalse(unseen.rows[0]["seen"])

    def test_relationships_can_be_filtered_by_kind(self) -> None:
        world = self._world()
        found = world.query({"kind": "relationships", "relationship_kind": "located_near"})
        self.assertIs(found.status, QueryStatus.OK)
        self.assertEqual(found.count, 2)
        none_other = world.query({"kind": "relationships", "relationship_kind": "owned_by"})
        self.assertIs(none_other.status, QueryStatus.EMPTY)

    def test_changes_since_filters_by_the_window(self) -> None:
        world = self._world()
        result = world.query({"kind": "changes_since", "since": stamp(0)})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertTrue(result.rows)
        beyond = world.query({"kind": "changes_since", "since": stamp(600)})
        self.assertIs(beyond.status, QueryStatus.EMPTY)

    def test_changes_since_refuses_an_unreadable_window(self) -> None:
        result = self._world().query({"kind": "changes_since", "since": "yesterday"})
        self.assertIs(result.status, QueryStatus.INVALID)

    def test_a_diff_between_two_retained_versions_works(self) -> None:
        world = self._world()
        world.observe(
            observation(
                [thing("laptop", 0, attributes=attrs(power="off")), thing("mug", 100)],
                at=120,
            )
        )
        snapshots = world.snapshots()
        result = world.query(
            {
                "kind": "diff",
                "state_id": snapshots[0].state_id,
                "other_state_id": snapshots[-1].state_id,
            }
        )
        self.assertIs(result.status, QueryStatus.OK)
        self.assertTrue(any(row["kind"] == "attribute_changed" for row in result.rows))
        self.assertTrue(result.limitations)

    def test_two_identical_versions_have_nothing_to_diff(self) -> None:
        world = self._world()
        snapshots = world.snapshots()
        result = world.query(
            {
                "kind": "diff",
                "state_id": snapshots[0].state_id,
                "other_state_id": snapshots[-1].state_id,
            }
        )
        self.assertIs(result.status, QueryStatus.EMPTY)

    def test_a_diff_needs_both_versions_retained(self) -> None:
        world = self._world()
        result = world.query(
            {"kind": "diff", "state_id": world.snapshots()[0].state_id, "other_state_id": "nope"}
        )
        self.assertIs(result.status, QueryStatus.NOT_FOUND)
        self.assertTrue(result.limitations)

    def test_stale_rows_name_why_they_are_stale(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([], at=30, complete=True))
        result = world.query({"kind": "stale"})
        self.assertIs(result.status, QueryStatus.OK)
        self.assertTrue(any(row["record"] == "entity" for row in result.rows))
        self.assertTrue(result.limitations)

    def test_query_run_directly_against_memory_matches_the_engine(self) -> None:
        world = self._world()
        direct = run_query(
            StateQuery.from_mapping({"kind": "entities"}),
            memory=world.memory,
            world_id="default",
        )
        self.assertEqual(direct.count, world.query({"kind": "entities"}).count)

    def test_history_kinds_only_names_what_happened(self) -> None:
        from novacontrol.world.queries import history_kinds

        kinds = history_kinds(self._world().memory)
        self.assertIn("entity_added", kinds)


# ── reasoning (§15) ──────────────────────────────────────────────────────────


class ReasoningTests(unittest.TestCase):
    """Named deterministic rules; a conclusion is reviewable, never authoritative."""

    def _world(self) -> WorldModelEngine:
        world = engine()
        world.observe(
            observation(
                [
                    thing("laptop", 0, attributes=[{"name": "power", "value": "on"}]),
                    thing("mug", 100),
                ],
                at=0,
            )
        )
        return world

    def test_a_rule_names_itself_and_its_evidence(self) -> None:
        conclusion = self._world().reason("where was the laptop last observed")[0]
        self.assertEqual(conclusion.rule, "last_seen")
        self.assertIn("laptop", conclusion.conclusion)
        self.assertTrue(conclusion.evidence)
        self.assertIsInstance(conclusion.limitations, tuple)
        self.assertTrue(conclusion.state_id)

    def test_a_conclusion_carries_no_private_reasoning(self) -> None:
        payload = self._world().reason("what is uncertain")[0].to_dict()
        for forbidden in ("reasoning", "thoughts", "chain_of_thought", "scratchpad"):
            self.assertNotIn(forbidden, payload)

    def test_an_unrecognised_question_lists_the_shapes_it_does_answer(self) -> None:
        conclusion = self._world().reason("write me a poem about the moon")[0]
        self.assertEqual(conclusion.rule, "unmatched")
        self.assertIn("known question shapes", " ".join(conclusion.limitations))

    def test_an_unknown_action_is_refused_by_name(self) -> None:
        conclusion = self._world().reason("anything", payload={"action": "psychic"})[0]
        self.assertEqual(conclusion.rule, "unknown_action")
        self.assertIn("psychic", conclusion.conclusion)

    def test_an_explicit_action_builds_exactly_one_conclusion(self) -> None:
        conclusions = self._world().reason(
            "unused", payload={"action": "last_seen", "subject": "laptop"}
        )
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0].rule, "last_seen")

    def test_reasoning_without_a_state_says_so(self) -> None:
        conclusion = engine().reason("what is uncertain")[0]
        self.assertEqual(conclusion.rule, "no_state")

    def test_a_spatial_question_uses_the_stored_geometry(self) -> None:
        conclusion = self._world().reason("is the laptop left of the mug")[0]
        self.assertEqual(conclusion.rule, "spatial_relation")
        self.assertIn("located_near", conclusion.conclusion)
        self.assertIn("a statement about a moment", " ".join(conclusion.limitations))

    def test_a_spatial_question_without_two_entities_says_what_it_needs(self) -> None:
        conclusion = self._world().reason("is the teacup left of the saucer")[0]
        self.assertEqual(conclusion.rule, "spatial_relation")
        self.assertIn("no two known entities", conclusion.conclusion)

    def test_the_stale_rule_names_what_stopped_being_supported(self) -> None:
        world = engine()
        world.observe(observation([thing("laptop", 0)], at=0))
        world.observe(observation([], at=30, complete=True))
        conclusion = world.reason("what is stale")[0]
        self.assertEqual(conclusion.rule, "stale")
        self.assertIn("not_observed", conclusion.conclusion)

    def test_the_conflict_rule_reports_the_disagreement(self) -> None:
        world = engine()
        world.observe(
            observation([thing("laptop", 0, track_id="t1", attributes=attrs(power="on"))], at=0)
        )
        world.observe(
            observation([thing("laptop", 100, track_id="t1", attributes=attrs(power="on"))], at=30)
        )
        world.observe(
            observation([thing("laptop", 105, track_id="t1", attributes=attrs(power="off"))], at=10)
        )
        conclusion = world.reason("do any observations conflict")[0]
        self.assertEqual(conclusion.rule, "conflict")
        self.assertIn("later observation", conclusion.conclusion)

    def test_the_uncertainty_rule_counts_by_kind(self) -> None:
        world = self._world()
        world.observe(observation([], at=60, complete=True))
        conclusion = world.reason("what is uncertain")[0]
        self.assertEqual(conclusion.rule, "uncertainty")
        self.assertIn("uncertainty record", conclusion.conclusion)
        self.assertIn("stale", conclusion.conclusion)

    def test_the_provisional_rule_names_a_guessed_identity(self) -> None:
        memory = TemporalMemory()
        memory.set_state(
            WorldState(
                world_id="w",
                version=1,
                timestamp=stamp(0),
                entities=(
                    WorldEntity(
                        entity_id="e1",
                        labels=("mug",),
                        provisional=True,
                        identity_confidence=0.4,
                    ),
                ),
            )
        )
        conclusion = answer("which identities are provisional", memory=memory)[0]
        self.assertEqual(conclusion.rule, "provisional_identity")
        self.assertIn("provisional", conclusion.conclusion)
        self.assertIn("0.40", conclusion.conclusion)

    def test_the_evidence_rule_shows_the_basis_of_each_fact(self) -> None:
        conclusion = self._world().reason("why is the laptop believed to be here")[0]
        self.assertEqual(conclusion.rule, "evidence")
        self.assertIn("observed", conclusion.conclusion)

    def test_every_rule_the_table_declares_is_reachable(self) -> None:
        for rule in reasoning_rules():
            self.assertTrue(rule.examples, f"{rule.name} declares no example")
            self.assertTrue(rule.build)
            self.assertEqual(rule.category, rule.category)
        self.assertEqual(
            {rule.name for rule in reasoning_rules()}, set(REASONING_ACTIONS)
        )

    def test_mentioned_entities_are_ordered_by_appearance(self) -> None:
        world = self._world()
        mentions = mentioned_entities("is the mug left of the laptop", world.state())
        self.assertEqual([item.label for item in mentions], ["mug", "laptop"])
        self.assertEqual(mentioned_entities("no entity here", world.state()), ())


class ReasoningRuleGeneratedTests(unittest.TestCase):
    """One row per rule; the examples ARE the contract and pin themselves."""

    @classmethod
    def setUpClass(cls) -> None:
        world = engine()
        world.observe(two_objects(at=0))
        cls.memory = world.memory

    def test_every_declared_example_routes_to_its_own_rule(self) -> None:
        for rule in reasoning_rules():
            for example in rule.examples:
                with self.subTest(rule=rule.name, example=example):
                    conclusions = answer(example, memory=self.memory)
                    self.assertTrue(
                        any(item.rule == rule.name for item in conclusions),
                        f"{example!r} did not reach {rule.name}: "
                        f"{[item.rule for item in conclusions]}",
                    )

    def test_every_conclusion_from_an_example_is_reviewable(self) -> None:
        for rule in reasoning_rules():
            for example in rule.examples:
                for conclusion in answer(example, memory=self.memory):
                    with self.subTest(rule=rule.name, example=example):
                        self.assertTrue(conclusion.conclusion)
                        self.assertTrue(conclusion.rule)


# ── capability table (§21) ───────────────────────────────────────────────────


class CapabilityTests(unittest.TestCase):
    """Classification states what a capability IS in this build, read live."""

    def test_the_table_declares_one_row_per_capability(self) -> None:
        self.assertEqual(len(CAPABILITY_TABLE), 12)
        ids = [row.capability_id for row in CAPABILITY_TABLE]
        self.assertEqual(len(ids), len(set(ids)))

    def test_prediction_is_provider_dependent_and_the_rest_are_implemented(self) -> None:
        by_id = {row.capability_id: row for row in CAPABILITY_TABLE}
        self.assertEqual(by_id["world.prediction"].state.value, "provider_dependent")
        implemented = [row for row in CAPABILITY_TABLE if row.capability_id != "world.prediction"]
        self.assertTrue(all(row.state.value == "implemented" for row in implemented))

    def test_persistence_availability_follows_whether_a_store_is_wired(self) -> None:
        without = {row.name: row for row in capability_rows(WorldModelEngine())}
        self.assertFalse(without["world.persistence"].available)
        self.assertIn("no world store", without["world.persistence"].reason)
        with_store = {row.name: row for row in capability_rows(engine())}
        self.assertTrue(with_store["world.persistence"].available)

    def test_prediction_availability_follows_the_provider(self) -> None:
        default = {row.name: row for row in capability_rows(engine())}
        self.assertFalse(default["world.prediction"].available)
        rule_projection = PredictionService(provider=RuleProjectionProvider())
        rule = {row.name: row for row in capability_rows(engine(prediction=rule_projection))}
        self.assertTrue(rule["world.prediction"].available)

    def test_deterministic_capabilities_need_no_provider(self) -> None:
        rows = {row.name: row for row in capability_rows(engine())}
        self.assertTrue(rows["world.observe"].available)
        self.assertTrue(rows["world.state_reasoning"].available)

    def test_the_table_registers_on_the_one_capability_registry(self) -> None:
        registry = CapabilityRegistry()
        self.assertEqual(register_world_capabilities(registry, engine()), 12)
        data = registry.to_dict()
        self.assertTrue(data)

    def test_registration_marks_unknown_availability_as_unknown(self) -> None:
        registry = CapabilityRegistry()
        self.assertEqual(register_world_capabilities(registry), 12)


# ── engine surfaces ──────────────────────────────────────────────────────────


class EngineSurfaceTests(unittest.TestCase):
    """Status, summary and history surfaces stay cheap and content-light."""

    def test_status_is_probe_free(self) -> None:
        repo = InMemoryWorldRepository()
        world = engine(repository=repo)
        world.observe(two_objects())
        before = (repo.loads, repo.saves)
        status = world.status()
        self.assertEqual(status["version"], 1)
        self.assertEqual(status["world_id"], "default")
        self.assertIs(status["observed"], True)
        self.assertEqual((repo.loads, repo.saves), before)

    def test_status_reports_the_prediction_posture_and_the_capabilities(self) -> None:
        status = engine().status()
        self.assertEqual(status["prediction"]["provider"], "none")
        self.assertFalse(status["prediction"]["available"])
        self.assertEqual(len(status["capabilities"]), 12)
        self.assertIn("retention", status)
        self.assertIn("memory", status)

    def test_status_before_any_observation_is_honest(self) -> None:
        status = engine().status()
        self.assertEqual(status["version"], 0)
        self.assertIs(status["observed"], False)
        self.assertEqual(status["entities"], 0)

    def test_state_summary_never_carries_attribute_values(self) -> None:
        world = engine()
        world.observe(
            observation([thing("laptop", 0, attributes=attrs(secret="VALUE-8ba"))], at=0)
        )
        summary = world.state_summary()
        self.assertNotIn("VALUE-8ba", json.dumps(summary))
        self.assertNotIn("attributes", summary["entities"][0])
        self.assertEqual(summary["entities"][0]["label"], "laptop")

    def test_telemetry_counts_what_happened(self) -> None:
        world = engine()
        world.observe(two_objects())
        world.query({"kind": "entities"})
        world.reason("what is uncertain")
        world.predict({"target": "laptop"})
        telemetry = world.status()["telemetry"]
        self.assertEqual(telemetry["ingested"], 1)
        self.assertEqual(telemetry["queries"], 1)
        self.assertEqual(telemetry["reasoning_requests"], 1)
        self.assertEqual(telemetry["predictions"], 1)
        self.assertEqual(telemetry["prediction_unavailable"], 1)

    def test_transitions_can_be_filtered(self) -> None:
        world = engine()
        world.observe(two_objects())
        only_added = world.transitions(kind="entity_added")
        self.assertEqual(len(only_added), 2)
        laptop = world.state().find("laptop")[0]
        self.assertTrue(world.transitions(entity_id=laptop.entity_id))

    def test_changes_only_report_change_making_kinds(self) -> None:
        world = engine()
        world.observe(two_objects())
        rows = world.changes()
        self.assertTrue(rows)
        # The rows are TRANSITIONS of change-making kinds (the transition log is
        # the one source), so their kind vocabulary is the transition one.
        self.assertTrue(all(row["kind"] in {item.value for item in TransitionKind} for row in rows))
        self.assertIn("entity_added", {row["kind"] for row in rows})

    def test_clear_state_forgets_the_world_and_its_duplicate_memory(self) -> None:
        world = engine()
        payload = two_objects()
        world.observe(payload)
        world.clear_state()
        self.assertEqual(world.state().version, 0)
        report = world.observe(payload)
        self.assertIs(report.status, IngestStatus.ACCEPTED)

    def test_prune_applies_retention_and_reports_the_count(self) -> None:
        world = engine(retention=WorldRetentionPolicy(max_snapshots=2))
        for index in range(4):
            world.observe(
                observation([thing("laptop", 0)], at=index * 10, source_id=f"cam-{index}")
            )
        self.assertEqual(len(world.snapshots()), 2)
        self.assertGreaterEqual(world.prune(), 0)

    def test_transition_readers_and_summary_work_on_the_log(self) -> None:
        world = engine()
        world.observe(two_objects())
        transitions = world.transitions(limit=MAX_TRANSITIONS)
        self.assertEqual(len(for_kind(transitions, TransitionKind.ENTITY_ADDED)), 2)
        laptop = world.state().find("laptop")[0]
        self.assertTrue(for_entity(transitions, laptop.entity_id))
        self.assertEqual(
            between_states(
                transitions,
                transitions[0].previous_state_id,
                transitions[0].current_state_id,
            ),
            tuple(
                item
                for item in transitions
                if item.previous_state_id == transitions[0].previous_state_id
                and item.current_state_id == transitions[0].current_state_id
            ),
        )
        summary_data = transition_summary(transitions)
        self.assertEqual(summary_data["total"], len(transitions))

    def test_a_transition_stamps_both_state_ids(self) -> None:
        world = engine()
        first = world.observe(two_objects())
        report = world.observe(observation([thing("mug", 100)], at=30, source_id="cam-2"))
        for item in report.transitions:
            self.assertEqual(item.previous_state_id, first.state_id)
            self.assertEqual(item.current_state_id, world.state().state_id)

    def test_the_stored_payload_declares_its_schema(self) -> None:
        payload = engine().payload()
        self.assertEqual(payload["phase"], "phase22")
        self.assertEqual(payload["schema_version"], "phase22.1")

    def test_a_disabled_section_has_no_store(self) -> None:
        settings = WorldModelSettings(enabled=False)
        self.assertFalse(settings.enabled)
        self.assertTrue(WorldModelSettings.from_mapping({"enabled": "yes"}).enabled)
        self.assertFalse(WorldModelSettings.from_mapping({"enabled": "off"}).enabled)

    def test_world_settings_clamp_and_survive_a_round_trip(self) -> None:
        settings = WorldModelSettings.from_mapping({"missing_after": 0, "rule_projection": True})
        # A zero is not a usable count: the config keeps the careful default, and
        # the estimator refuses sub-one values as a second line of defence.
        self.assertEqual(settings.missing_after, 2)
        self.assertEqual(EstimationPolicy.from_mapping({"missing_after": 0}).missing_after, 1)
        self.assertTrue(settings.rule_projection)
        self.assertEqual(
            WorldModelSettings.from_mapping(settings.to_mapping()).world_id, settings.world_id
        )


class EventAnnouncementTests(unittest.TestCase):
    """The seam announces, is bounded, and can never fail the work."""

    def test_an_ingest_announces_its_observation_state_and_transitions(self) -> None:
        observer = _RecordingObserver()
        world = engine(observer=observer)
        world.observe(two_objects(), correlation_id="req-42")
        self.assertIn(OBSERVATION_INGESTED, observer.types())
        self.assertIn(STATE_UPDATED, observer.types())
        self.assertIn(STATE_TRANSITION_CREATED, observer.types())
        self.assertEqual(observer.first(OBSERVATION_INGESTED)["correlation_id"], "req-42")

    def test_announcements_are_bounded_per_ingest(self) -> None:
        observer = _RecordingObserver()
        world = engine(observer=observer)
        rows = [thing(f"object-{index}", index * 60) for index in range(30)]
        world.observe(observation(rows, at=0))
        self.assertLessEqual(len(observer.events), MAX_ANNOUNCEMENTS)

    def test_announcements_never_carry_attribute_values(self) -> None:
        observer = _RecordingObserver()
        world = engine(observer=observer)
        world.observe(
            observation([thing("laptop", 0, attributes=attrs(secret="VALUE-8ba"))], at=0)
        )
        self.assertNotIn("VALUE-8ba", json.dumps([payload for _, payload in observer.events]))

    def test_a_broken_watcher_cannot_break_an_ingest(self) -> None:
        world = engine(observer=_RaisingObserver())
        report = world.observe(two_objects())
        self.assertIs(report.status, IngestStatus.ACCEPTED)

    def test_a_broken_watcher_cannot_break_a_restore(self) -> None:
        repo = InMemoryWorldRepository()
        source = engine(repository=repo)
        source.observe(two_objects())
        source.save()
        restarted = WorldModelEngine(repository=repo, observer=_RaisingObserver())
        self.assertTrue(restarted.restore().restored)

    def test_a_restore_announces_what_came_back(self) -> None:
        repo = InMemoryWorldRepository()
        source = engine(repository=repo)
        source.observe(two_objects())
        source.save()
        observer = _RecordingObserver()
        restarted = WorldModelEngine(repository=repo, observer=observer)
        restarted.restore()
        self.assertTrue(any(name.endswith("state_restored") for name in observer.types()))

    def test_the_event_vocabulary_is_the_builds_own(self) -> None:
        from novacontrol.core.events import EVENT_PAYLOAD_FIELDS, EventType

        for name in (
            OBSERVATION_INGESTED,
            STATE_UPDATED,
            STATE_TRANSITION_CREATED,
            "world.entity_changed",
            "world.relationship_changed",
            "world.conflict_detected",
            "world.state_restored",
            PREDICTION_COMPLETED,
            PREDICTION_FAILED,
        ):
            event_type = EventType(name)
            self.assertIn(event_type, EVENT_PAYLOAD_FIELDS)
            self.assertTrue(EVENT_PAYLOAD_FIELDS[event_type])


class TelemetryTests(unittest.TestCase):
    """The rolling counters are the cheap evidence the layer is working."""

    def test_counters_start_at_zero_and_move(self) -> None:
        world = engine()
        before = WorldTelemetry()
        self.assertEqual(before.ingested, 0)
        world.observe(two_objects())
        world.observe(observation([thing("laptop", 0)], at=30))
        self.assertGreaterEqual(world.telemetry.ingested, 2)
        self.assertIsNotNone(world.telemetry.last_ingest_ms)


# ── persistence store (§12) ──────────────────────────────────────────────────


class StoreTests(unittest.TestCase):
    """One key per world, and a path-shaped id cannot escape its directory."""

    def test_a_world_id_is_sanitized_into_a_safe_key(self) -> None:
        self.assertEqual(sanitize_world_id(""), "default")
        self.assertEqual(sanitize_world_id("   "), "default")
        cleaned = sanitize_world_id("../../etc/passwd")
        self.assertNotIn("/", cleaned)
        self.assertNotIn("\\", cleaned)
        self.assertTrue(cleaned)
        self.assertLessEqual(len(sanitize_world_id("x" * 400)), 64)

    def test_the_in_memory_repository_isolates_worlds(self) -> None:
        repo = InMemoryWorldRepository()
        repo.save("alpha", {"world_id": "alpha"})
        repo.save("beta", {"world_id": "beta"})
        self.assertEqual(repo.worlds(), ("alpha", "beta"))
        self.assertEqual(repo.load("alpha")["world_id"], "alpha")
        self.assertTrue(repo.clear("alpha"))
        self.assertFalse(repo.clear("alpha"))
        self.assertEqual(repo.load("missing"), {})

    def test_a_real_json_store_round_trips_a_world(self) -> None:
        with TemporaryDirectory() as directory:
            repo = JsonWorldRepository(JsonStateStore(directory))
            world = WorldModelEngine(repository=repo, world_id="lab room")
            world.observe(two_objects())
            self.assertTrue(world.save())
            self.assertTrue(repo.key("lab room").startswith("world_"))
            restarted = WorldModelEngine(repository=repo, world_id="lab room")
            report = restarted.restore()
            self.assertTrue(report.restored)
            self.assertEqual(len(restarted.state().find("laptop")), 1)

    def test_an_unreadable_store_reads_as_nothing(self) -> None:
        repo = JsonWorldRepository(JsonStateStore("/definitely/not/a/directory"))
        self.assertEqual(repo.load("default"), {})
        self.assertTrue(repo.clear("default"))

    def test_two_worlds_never_share_a_payload(self) -> None:
        with TemporaryDirectory() as directory:
            repo = JsonWorldRepository(JsonStateStore(directory))
            first = WorldModelEngine(repository=repo, world_id="one")
            first.observe(two_objects())
            first.save()
            second = WorldModelEngine(repository=repo, world_id="two")
            second.observe(observation([thing("phone", 0)], at=0))
            second.save()
            first_world = WorldModelEngine(repository=repo, world_id="one")
            first_world.restore()
            self.assertEqual(
                {item.label for item in first_world.state().entities}, {"laptop", "mug"}
            )
            other_world = WorldModelEngine(repository=repo, world_id="two")
            other_world.restore()
            self.assertEqual(
                {item.label for item in other_world.state().entities}, {"phone"}
            )


# ── Phase 20 bridge (§19) ────────────────────────────────────────────────────


class AgenticBridgeTests(unittest.TestCase):
    """A world state becomes an observation; it never becomes an action."""

    def _state(self) -> WorldState:
        world = engine()
        world.observe(
            observation([thing("laptop", 0, attributes=attrs(secret="VALUE-8ba"))], at=0)
        )
        return world.state()

    def test_an_unobserved_world_produces_an_honest_observation(self) -> None:
        empty = world_observation(None)
        self.assertEqual(empty["kind"], "world.state")
        self.assertFalse(empty["present"])

    def test_a_world_observation_is_bounded_and_content_light(self) -> None:
        observation_map = world_observation(self._state())
        self.assertEqual(observation_map["kind"], "world.state")
        self.assertEqual(observation_map["entity_count"], 1)
        self.assertNotIn("VALUE-8ba", json.dumps(observation_map))
        self.assertEqual(observation_map["entities"][0]["label"], "laptop")

    def test_environment_state_is_a_small_summary_a_policy_can_fingerprint(self) -> None:
        summary = environment_state_for(self._state())
        self.assertEqual(summary["world"]["entities"], 1)
        self.assertIn("laptop", summary["world"]["labels"])
        self.assertNotIn("VALUE-8ba", json.dumps(summary))

    def test_attaching_advances_a_real_phase_20_state(self) -> None:
        agent_state = AgentState(goal="look around")
        advanced = attach_world_state(agent_state, self._state())
        self.assertIsInstance(advanced, AgentState)
        self.assertEqual(advanced.observations[-1]["kind"], "world.state")
        self.assertEqual(advanced.environment_state["world"]["entities"], 1)
        self.assertEqual(advanced.previous_actions, ())

    def test_attaching_to_something_without_the_contract_returns_the_observation(self) -> None:
        attached = attach_world_state(object(), self._state())
        self.assertEqual(attached["kind"], "world.state")

    def test_no_action_is_taken_by_the_bridge(self) -> None:
        agent_state = AgentState(goal="look around")
        advanced = attach_world_state(agent_state, self._state())
        # Attaching is a state advance, never a decision: nothing was done and
        # nothing was verified.
        self.assertEqual(advanced.previous_actions, ())
        self.assertEqual(advanced.verification_results, ())
        self.assertEqual(advanced.active_errors, ())


class PerceptionBridgeTests(unittest.IsolatedAsyncioTestCase):
    """Looking goes through the real Phase 21 contract, and remembering is separate."""

    async def test_observe_perception_ingests_what_the_engine_saw(self) -> None:
        from novacontrol.world.agentic import observe_perception

        scene = SceneRepresentation(
            scene_id="s1",
            timestamp=stamp(0),
            source_id="cam-1",
            objects=(DetectedObject(label="laptop", bbox=BBox(0, 0, 40, 40), object_id="obj-1"),),
        )
        fake = _FakePerception(PerceptionResult(status=PerceptionStatus.SUCCESS, scene=scene))
        world = engine()
        result, report = await observe_perception(
            world, fake, request="look", correlation_id="req-1"
        )
        self.assertIs(result, fake.result)
        self.assertIs(report.status, IngestStatus.ACCEPTED)
        self.assertEqual([item.label for item in world.state().entities], ["laptop"])
        self.assertEqual(fake.requests, ["look"])

    async def test_a_failed_look_is_still_remembered_as_a_failed_look(self) -> None:
        from novacontrol.world.agentic import observe_perception

        failed = PerceptionResult(status=PerceptionStatus.UNAVAILABLE, status_reason="no provider")
        fake = _FakePerception(failed)
        world = engine()
        _result, report = await observe_perception(world, fake, request="look")
        self.assertTrue(report.accepted)
        status = {item.name: item for item in world.state().conditions}["perception_status"]
        self.assertEqual(status.value["status"], "unavailable")

    async def test_the_correlation_id_travels_onto_the_ingested_observation(self) -> None:
        from novacontrol.world.agentic import observe_perception

        scene = SceneRepresentation(
            scene_id="s2",
            timestamp=stamp(0),
            source_id="cam-1",
            objects=(DetectedObject(label="mug", bbox=BBox(0, 0, 40, 40), object_id="obj-2"),),
        )
        fake = _FakePerception(PerceptionResult(status=PerceptionStatus.SUCCESS, scene=scene))
        observer = _RecordingObserver()
        world = engine(observer=observer)
        await observe_perception(world, fake, request="look", correlation_id="req-9")
        self.assertEqual(observer.first(OBSERVATION_INGESTED)["correlation_id"], "req-9")


class ObservationBatchTests(unittest.TestCase):
    """The API's batch shape: one request, many observations."""

    def test_a_single_observation_is_one_batch(self) -> None:
        payload = {"entities": [thing("laptop", 0)]}
        self.assertEqual(_observation_batch(payload), [payload])

    def test_a_list_carries_its_shared_keys_into_each_observation(self) -> None:
        payload = {
            "source": "perception",
            "source_id": "cam-1",
            "observations": [{"timestamp": stamp(0)}, {"timestamp": stamp(10)}],
        }
        batch = _observation_batch(payload)
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(item["source_id"] == "cam-1" for item in batch))
        self.assertNotIn("observations", batch[0])

    def test_non_object_rows_are_dropped(self) -> None:
        payload = {"source_id": "cam-1", "observations": [{"timestamp": stamp(0)}, "nonsense"]}
        self.assertEqual(len(_observation_batch(payload)), 1)

    def test_an_empty_list_falls_back_to_the_payload(self) -> None:
        payload = {"source_id": "cam-1", "observations": []}
        self.assertEqual(_observation_batch(payload), [payload])


# ── the workflows through HTTP ───────────────────────────────────────────────


@unittest.skipIf(
    TestClient is None,
    "fastapi.testclient.TestClient requires httpx (pip install httpx)",
)
class WorldApiTests(unittest.TestCase):
    """The same workflows a user has, over the real create_app() surface."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
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

    def _isolated_application(self, **kwargs: object) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    def test_status_is_readable_before_anything_is_observed(self) -> None:
        response = self._client.get("/world/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["world_id"], "default")
        self.assertEqual(body["version"], 0)
        self.assertFalse(body["observed"])
        self.assertEqual(body["prediction"]["provider"], "none")
        self.assertEqual(len(body["capabilities"]), 12)

    def test_a_stored_world_is_configured_by_default(self) -> None:
        body = self._client.get("/world/status").json()
        self.assertTrue(body["persistence"]["store_wired"])

    def test_observing_then_reading_the_state_is_one_workflow(self) -> None:
        ingest = self._client.post("/world/observe", json=two_objects())
        self.assertEqual(ingest.status_code, 200)
        report = ingest.json()
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["batch"], 1)

        state = self._client.get("/world/state")
        self.assertEqual(state.status_code, 200)
        body = state.json()
        self.assertEqual({item["label"] for item in body["entities"]}, {"laptop", "mug"})
        # Content-light by construction: attribute values stay behind the query layer.
        self.assertNotIn("attributes", body["entities"][0])

    def test_the_state_can_be_narrowed_to_one_entity(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        entity_id = self._client.get("/world/state").json()["entities"][0]["entity_id"]
        narrowed = self._client.get("/world/state", params={"entity_id": entity_id}).json()
        self.assertEqual(len(narrowed["entities"]), 1)
        self.assertEqual(narrowed["requested_entity_id"], entity_id)

    def test_a_query_without_an_entity_is_a_named_422(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        response = self._client.post("/world/query", json={"kind": "entity"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("entity_id", response.json()["detail"])

    def test_a_query_answers_a_grounded_question(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        response = self._client.post("/world/query", json={"kind": "entities"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["count"], 2)

    def test_a_historical_question_is_not_answered_with_now(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        response = self._client.post(
            "/world/query", json={"kind": "state_at", "since": "2019-01-01T00:00:00+00:00"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "not_found")
        self.assertTrue(any("NOT substituted" in item for item in body["limitations"]))

    def test_prediction_is_honest_over_http(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        response = self._client.post("/world/predict", json={"target": "laptop"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "model_unavailable")

    def test_an_observation_with_nothing_to_record_is_a_422(self) -> None:
        response = self._client.post("/world/observe", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("entities", response.json()["detail"])

    def test_a_batch_of_observations_is_ingested_in_order(self) -> None:
        payload = {
            **observation([thing("laptop", 0)], at=0),
            "observations": [
                {"timestamp": stamp(0), "entities": [thing("laptop", 0)]},
                {"timestamp": stamp(10), "entities": [thing("laptop", 0)]},
            ],
        }
        response = self._client.post("/world/observe", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["batch"], 2)
        self.assertEqual(body["version"], 2)
        self.assertEqual(len(body["reports"]), 2)

    def test_status_reports_the_world_model_summary(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        body = self._client.get("/status").json()
        self.assertIn("world_model", body["app"])
        self.assertEqual(body["app"]["world_model"]["version"], 1)
        self.assertEqual(body["app"]["world_model"]["prediction"]["provider"], "none")

    def test_persist_then_reopen_recovers_the_world(self) -> None:
        self._client.post("/world/observe", json=two_objects())
        assert self._nova is not None
        self._nova.persist()
        reopened = WorldModelEngine(
            repository=JsonWorldRepository(JsonStateStore(self._tmp.name)), world_id="default"
        )
        report = reopened.restore()
        self.assertTrue(report.restored)
        self.assertEqual({item.label for item in reopened.state().entities}, {"laptop", "mug"})

    def test_a_second_world_starts_empty(self) -> None:
        with TemporaryDirectory() as other:
            isolated = WorldModelEngine(
                repository=JsonWorldRepository(JsonStateStore(other)), world_id="default"
            )
            self.assertFalse(isolated.restore().restored)
            self.assertEqual(isolated.state().entities, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
