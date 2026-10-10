"""The world-model engine: the one door every fact enters and every question leaves (§1).

The pipeline this class runs is the phase's whole shape, in order:

    observation  ->  normalization  ->  duplicate and ordering checks
                 ->  state estimation  ->  new version + transitions + changes
                 ->  temporal memory (bounded)  ->  events (through a seam)
                 ->  queries / reasoning / prediction (read-only)

Everything expensive is opt-in and everything optional degrades honestly:

* **Ingest is deterministic and cheap.** Reconciliation is arithmetic over records
  — no model, no network, no disk. A world can be observed thousands of times a
  minute on a machine with nothing installed.
* **The state store is reached only when persistence is asked for** (or auto-save
  is on), and a store that fails never fails the observation.
* **Prediction is a separate request** through a provider that may not exist, and
  the engine reports that rather than guessing.
* **``status()`` never probes anything.** It reads attributes the engine already
  holds: no model, no HTTP, no disk. That rule is inherited deliberately from
  Phase 21, where a status surface that probed a runtime cost two seconds on the
  application's hottest path.

Events go out through an injected one-argument seam (``observer(type, payload)``)
rather than through a bus looked up here, so this package never imports the
application and a test can count announcements without a bus.
"""

from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.world import ingest as ingestion
from novacontrol.world import transitions as transition_factory
from novacontrol.world.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    capability_rows,
)
from novacontrol.world.estimation import EstimationOutcome, EstimationPolicy, estimate
from novacontrol.world.memory import TemporalMemory, WorldRetentionPolicy
from novacontrol.world.models import (
    IngestStatus,
    Observation,
    PredictionRequest,
    PredictionResult,
    PredictionStatus,
    ReasoningConclusion,
    RestoreReport,
    StateQuery,
    StateQueryResult,
    UncertaintyKind,
    UncertaintyRecord,
    UpdateReport,
    WorldState,
    WorldStateTransition,
    as_text,
    iso_now,
)
from novacontrol.world.prediction import (
    DEFAULT_MAX_HORIZON_SECONDS,
    DEFAULT_TIMEOUT_MS,
    NoPredictionProvider,
    PredictionProvider,
    PredictionService,
    ResourceGateLike,
)
from novacontrol.world.queries import run_query
from novacontrol.world.reasoning import answer as _answer
from novacontrol.world.store import WorldRepository
from novacontrol.world.timeutil import is_newer, seconds_between

__all__ = [
    "CONFLICT_DETECTED",
    "ENTITY_STATE_CHANGED",
    "EVENT_TYPES",
    "OBSERVATION_INGESTED",
    "PHASE",
    "PREDICTION_COMPLETED",
    "PREDICTION_FAILED",
    "RELATIONSHIP_CHANGED",
    "SCHEMA_VERSION",
    "STATE_TRANSITION_CREATED",
    "STATE_UPDATED",
    "STATE_RESTORED",
    "WorldModelEngine",
    "WorldTelemetry",
]

#: The phase this package implements.
PHASE = "phase22"

#: The schema version of the records this package serializes.
SCHEMA_VERSION = "phase22.1"

#: The announcement vocabulary, named here and translated by the application. Each
#: one carries ids, counts and kinds — never content, never an observation body.
OBSERVATION_INGESTED = "world.observation_ingested"
STATE_UPDATED = "world.state_updated"
STATE_TRANSITION_CREATED = "world.transition_created"
ENTITY_STATE_CHANGED = "world.entity_changed"
RELATIONSHIP_CHANGED = "world.relationship_changed"
CONFLICT_DETECTED = "world.conflict_detected"
STATE_RESTORED = "world.state_restored"
PREDICTION_COMPLETED = "world.prediction_completed"
PREDICTION_FAILED = "world.prediction_failed"

EVENT_TYPES: tuple[str, ...] = (
    OBSERVATION_INGESTED,
    STATE_UPDATED,
    STATE_TRANSITION_CREATED,
    ENTITY_STATE_CHANGED,
    RELATIONSHIP_CHANGED,
    CONFLICT_DETECTED,
    STATE_RESTORED,
    PREDICTION_COMPLETED,
    PREDICTION_FAILED,
)

#: How many content fingerprints are remembered for duplicate detection. Bounded:
#: duplicate detection is a courtesy to a retrying caller, not a ledger.
DEDUPE_WINDOW = 256

#: How many announcements one ingest may make. A large scene must not turn into a
#: burst of events; the excess is dropped and the counts in the status still move.
MAX_ANNOUNCEMENTS = 24

#: A seam the application injects: ``(event_type, payload)``.
WorldObserver = Callable[[str, Mapping[str, Any]], None]


def _elapsed(started: float) -> float:
    """Milliseconds since a ``perf_counter`` reading, rounded for a status surface."""
    return round((time.perf_counter() - started) * 1000, 3)


@dataclass(slots=True)
class WorldTelemetry:
    """Rolling counters — the cheap evidence that the layer is doing something."""

    ingested: int = 0
    rejected: int = 0
    duplicates: int = 0
    stale: int = 0
    out_of_order: int = 0
    transitions: int = 0
    changes: int = 0
    conflicts: int = 0
    queries: int = 0
    reasoning_requests: int = 0
    predictions: int = 0
    prediction_unavailable: int = 0
    restores: int = 0
    saves: int = 0
    last_ingest_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingested": self.ingested,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "stale": self.stale,
            "out_of_order": self.out_of_order,
            "transitions": self.transitions,
            "changes": self.changes,
            "conflicts": self.conflicts,
            "queries": self.queries,
            "reasoning_requests": self.reasoning_requests,
            "predictions": self.predictions,
            "prediction_unavailable": self.prediction_unavailable,
            "restores": self.restores,
            "saves": self.saves,
            "last_ingest_ms": self.last_ingest_ms,
        }


class WorldModelEngine:
    """The world model. Every collaborator is injected and every default is honest.

    With nothing passed, the engine still works: it holds state in memory, answers
    queries and reasoning requests deterministically, and reports prediction as
    unavailable. Persistence and prediction are the two things a caller may add,
    and neither changes what the state store can already do.
    """

    def __init__(
        self,
        *,
        world_id: str = "default",
        observer: WorldObserver | None = None,
        policy: EstimationPolicy | None = None,
        retention: WorldRetentionPolicy | None = None,
        repository: WorldRepository | None = None,
        gate: ResourceGateLike | None = None,
        prediction: PredictionService | None = None,
        prediction_provider: PredictionProvider | None = None,
        autosave: bool = False,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.world_id = as_text(world_id, "default")
        self.observer = observer
        self.policy = policy or EstimationPolicy()
        self.retention = retention or WorldRetentionPolicy()
        self.repository = repository
        self.autosave = bool(autosave)
        self._clock = clock or iso_now
        self.memory = TemporalMemory(world_id=self.world_id, retention=self.retention)
        if prediction is not None:
            self.prediction = prediction
        else:
            self.prediction = PredictionService(
                provider=prediction_provider or NoPredictionProvider(),
                gate=gate,
                timeout_ms=DEFAULT_TIMEOUT_MS,
                max_horizon_seconds=DEFAULT_MAX_HORIZON_SECONDS,
            )
        self.telemetry = WorldTelemetry()
        self._seen: deque[str] = deque(maxlen=DEDUPE_WINDOW)
        self._clock_watermark: dict[str, str] = {}
        self._last_saved_at = ""
        self._last_save_ok: bool | None = None

    # ── wiring ───────────────────────────────────────────────────────────────
    def set_observer(self, observer: WorldObserver | None) -> None:
        """Install the announcement seam — the one place this engine speaks."""
        self.observer = observer

    # ── ingest ───────────────────────────────────────────────────────────────
    def observe(
        self,
        value: Any,
        *,
        correlation_id: str = "",
        timestamp: str = "",
    ) -> UpdateReport:
        """Normalize and apply one observation, and report exactly what happened.

        A refusal is a REPORT: a malformed observation returns
        ``status=REJECTED`` with the reason and changes nothing. One bad caller
        cannot stop the world.
        """
        started = time.perf_counter()
        try:
            observation = ingestion.normalize(value, timestamp=timestamp or self._clock())
        except ingestion.ObservationRejected as exc:
            self.telemetry.rejected += 1
            return UpdateReport(
                status=IngestStatus.REJECTED,
                reason=exc.reason,
                elapsed_ms=_elapsed(started),
            )
        verdict, verdict_reason = ingestion.validate(observation)
        if verdict == "rejected":
            self.telemetry.rejected += 1
            return UpdateReport(
                status=IngestStatus.REJECTED,
                reason=verdict_reason,
                observation_id=observation.observation_id,
                elapsed_ms=_elapsed(started),
            )

        current = self.memory.state or self._empty_state(observation.timestamp)
        fingerprint = observation.content_key()
        if fingerprint in self._seen:
            self.telemetry.duplicates += 1
            return UpdateReport(
                status=IngestStatus.DUPLICATE,
                reason=(
                    "this exact observation (same source, scope, time and content) was "
                    "already ingested, so the state was not changed again"
                ),
                observation_id=observation.observation_id,
                state_id=current.state_id,
                version=current.version,
                duplicate=True,
                elapsed_ms=_elapsed(started),
            )

        status, ordering_reason, extra_uncertainty = self._ordering(observation, current)
        outcome = estimate(
            previous=current,
            observation=observation,
            policy=self.policy,
            evidence_id=observation.observation_id,
            extra_uncertainty=extra_uncertainty,
        )
        self.memory.set_state(outcome.state, observation=observation)
        self.memory.record_transitions(outcome.transitions)
        self._remember(fingerprint, observation)

        self.telemetry.ingested += 1
        self.telemetry.last_ingest_ms = _elapsed(started)
        self.telemetry.transitions += len(outcome.transitions)
        self.telemetry.changes += len(outcome.changes)
        self.telemetry.conflicts += sum(
            1 for item in outcome.uncertainty if item.kind is UncertaintyKind.CONFLICTING
        )
        if status is IngestStatus.STALE:
            self.telemetry.stale += 1
        if status is IngestStatus.OUT_OF_ORDER:
            self.telemetry.out_of_order += 1
        if verdict == "partial" and status is IngestStatus.ACCEPTED:
            status = IngestStatus.PARTIAL

        if self.autosave and self.repository is not None:
            self.save()

        # A caller that stamped the observation itself should not have to say the
        # same id twice: the observation's own correlation is the fallback.
        self._announce(
            outcome,
            observation,
            correlation_id=correlation_id or observation.correlation_id,
        )
        reason = verdict_reason or ordering_reason
        return UpdateReport(
            status=status,
            reason=reason,
            observation_id=observation.observation_id,
            state_id=outcome.state.state_id,
            version=outcome.state.version,
            entities_added=outcome.entities_added,
            entities_updated=outcome.entities_updated,
            entities_not_observed=outcome.entities_not_observed,
            entities_expired=outcome.entities_expired,
            relationships_added=outcome.relationships.added,
            relationships_retracted=outcome.relationships.retracted,
            transitions=outcome.transitions,
            changes=outcome.changes,
            estimates=outcome.estimates,
            uncertainty=outcome.uncertainty,
            stale=status is IngestStatus.STALE,
            elapsed_ms=_elapsed(started),
        )

    def ingest(self, observation: Observation) -> UpdateReport:
        """Apply an already-normalized observation (the same path as :meth:`observe`)."""
        return self.observe(observation)

    def _ordering(
        self, observation: Observation, current: WorldState
    ) -> tuple[IngestStatus, str, tuple[UncertaintyRecord, ...]]:
        """How this observation relates in time to what the world already holds (§8).

        Three readings, each recorded rather than acted on destructively:

        * **stale** — more than ``stale_after_seconds`` older than the state. Applied
          (the facts are still facts) with an uncertainty row naming the delay.
        * **out of order** — older than a previous observation from the SAME clock.
          Applied, with the ordering doubt recorded.
        * **accepted** — everything else.
        """
        rows: list[UncertaintyRecord] = []
        watermark = self._clock_watermark.get(observation.clock, "")
        out_of_order = False
        if watermark:
            newer = is_newer(
                observation.timestamp,
                watermark,
                clock=observation.clock,
                other_clock=observation.clock,
            )
            out_of_order = newer is False
        if out_of_order:
            rows.append(
                UncertaintyRecord(
                    subject=observation.observation_id,
                    kind=UncertaintyKind.OUT_OF_ORDER,
                    detail=(
                        f"this observation is older than {watermark} from the same clock; "
                        "its facts were applied but the stored newer values were kept"
                    ),
                    evidence=(observation.observation_id,),
                    recorded_at=observation.timestamp,
                )
            )
        # ``seconds_between(observation, current)`` is (current - observation), so
        # a POSITIVE age means the observation is older than the state it would
        # update — which is what "stale" means. A negative age is an observation
        # ahead of the state (clock skew), and that is not staleness.
        age = seconds_between(observation.timestamp, current.timestamp)
        lag = abs(age) if age is not None else None
        stale = (
            age is not None
            and age > 0
            and lag is not None
            and lag > self.policy.stale_after_seconds
        )
        if stale and lag is not None:
            rows.append(
                UncertaintyRecord(
                    subject=observation.observation_id,
                    kind=UncertaintyKind.STALE,
                    detail=(
                        f"this observation is {lag:.0f}s older than the state it was "
                        "applied to; the state's newer values were kept"
                    ),
                    evidence=(observation.observation_id,),
                    recorded_at=observation.timestamp,
                )
            )
        if not observation.timestamp:
            rows.append(
                UncertaintyRecord(
                    subject=observation.observation_id,
                    kind=UncertaintyKind.UNMEASURED,
                    detail=(
                        "the observation carried no timestamp, so its facts have no "
                        "readable time"
                    ),
                    evidence=(observation.observation_id,),
                    recorded_at=self._clock(),
                )
            )
        if out_of_order:
            return IngestStatus.OUT_OF_ORDER, rows[0].detail, tuple(rows)
        if stale:
            detail = next(item.detail for item in rows if item.kind is UncertaintyKind.STALE)
            return IngestStatus.STALE, detail, tuple(rows)
        return IngestStatus.ACCEPTED, "", tuple(rows)

    def _remember(self, fingerprint: str, observation: Observation) -> None:
        self._seen.append(fingerprint)
        watermark = self._clock_watermark.get(observation.clock, "")
        if not watermark or is_newer(observation.timestamp, watermark) is True:
            self._clock_watermark[observation.clock] = observation.timestamp

    def _announce(
        self, outcome: EstimationOutcome, observation: Observation, *, correlation_id: str
    ) -> None:
        """Announce what happened, through the injected seam, bounded and content-free."""
        announce = self.observer
        if announce is None:
            return
        state = outcome.state
        total = 0

        def emit(type_: str, **payload: Any) -> None:
            nonlocal total
            if total >= MAX_ANNOUNCEMENTS:
                return
            total += 1
            try:
                announce(type_, {"correlation_id": correlation_id, **payload})
            except Exception:  # noqa: BLE001 - an announcement never fails the ingest
                return

        emit(
            OBSERVATION_INGESTED,
            observation_id=observation.observation_id,
            source_kind=observation.source.value,
            entities=len(observation.entities),
            scope=observation.scope,
        )
        emit(
            STATE_UPDATED,
            state_id=state.state_id,
            version=state.version,
            entities=len(state.entities),
            relationships=len(state.relationships),
        )
        for transition in outcome.transitions:
            emit(
                STATE_TRANSITION_CREATED,
                transition_id=transition.transition_id,
                kind=transition.kind.value,
                state_id=state.state_id,
                entity_id=transition.entity_id,
            )
        for change in outcome.changes:
            emit(
                ENTITY_STATE_CHANGED,
                entity_id=change.entity_id,
                change=change.kind.value,
                detail=change.detail,
            )
        for relationship in outcome.relationships.relationships:
            if relationship.valid_from != state.timestamp and relationship.status.value == "active":
                continue
            emit(
                RELATIONSHIP_CHANGED,
                relationship_id=relationship.relationship_id,
                kind=relationship.kind.value,
                status=relationship.status.value,
            )
        for item in outcome.uncertainty:
            if item.kind is not UncertaintyKind.CONFLICTING:
                continue
            emit(CONFLICT_DETECTED, subject=item.subject, detail=item.detail)

    # ── read ─────────────────────────────────────────────────────────────────
    def state(self) -> WorldState:
        """The current version — an empty state at version 0 when nothing was seen.

        Never ``None``: a caller that has to check for nothing before reading the
        state writes the check wrong eventually, and "this world has never been
        observed" is exactly what version 0 with no entities says.
        """
        return self.memory.state or self._empty_state(self._clock())

    def snapshots(self) -> tuple[WorldState, ...]:
        return self.memory.snapshots

    def transitions(
        self,
        *,
        limit: int = 20,
        since: str = "",
        until: str = "",
        kind: str = "",
        entity_id: str = "",
    ) -> tuple[WorldStateTransition, ...]:
        """Recent transitions, filtered the way the query layer filters them."""
        rows: tuple[WorldStateTransition, ...]
        if since or until:
            rows = self.memory.changes_between(since, until)
        else:
            rows = self.memory.transitions
        if kind:
            rows = transition_factory.for_kind(rows, kind)
        if entity_id:
            rows = tuple(item for item in rows if item.entity_id == entity_id)
        bounded = max(1, min(500, int(limit or 20)))
        return tuple(rows[-bounded:])

    def changes(
        self, *, limit: int = 20, since: str = "", until: str = ""
    ) -> tuple[dict[str, Any], ...]:
        """Structured change events between retained versions (§13).

        Read from the transition log so it never has to re-compare states: a change
        is what a transition of a change-making kind recorded.
        """
        change_kinds = {
            "entity_added",
            "attribute_changed",
            "entity_moved",
            "relationship_changed",
            "entity_not_observed",
            "entity_confirmed_missing",
            "entity_expired",
            "state_conflict",
            "scene_changed",
        }
        rows = self.transitions(limit=500, since=since, until=until)
        return tuple(
            {
                "transition_id": item.transition_id,
                "kind": item.kind.value,
                "entity_id": item.entity_id,
                "attribute": item.attribute,
                "detail": item.detail,
                "previous": item.previous,
                "current": item.current,
                "timestamp": item.timestamp,
                "evidence": list(item.evidence),
            }
            for item in rows
            if item.kind.value in change_kinds
        )[-max(1, min(500, int(limit or 20))) :]

    def query(self, query: StateQuery | Mapping[str, Any]) -> StateQueryResult:
        """Answer a structured state question — bounded and never substituted."""
        resolved = query if isinstance(query, StateQuery) else StateQuery.from_mapping(query)
        result = run_query(
            resolved,
            memory=self.memory,
            world_id=self.world_id,
            thresholds=self.policy.thresholds,
        )
        self.telemetry.queries += 1
        return result

    def reason(
        self, question: Any, *, payload: Mapping[str, Any] | None = None
    ) -> tuple[ReasoningConclusion, ...]:
        """Answer through the named deterministic rules; never a guess (§15)."""
        self.telemetry.reasoning_requests += 1
        return _answer(question, memory=self.memory, payload=payload)

    def predict(
        self, request: PredictionRequest | Mapping[str, Any] | None = None
    ) -> PredictionResult:
        """Ask the prediction service — and report honestly when it cannot answer."""
        resolved = (
            request
            if isinstance(request, PredictionRequest)
            else PredictionRequest.from_mapping(request or {})
        )
        result = self.prediction.predict(
            resolved,
            state=self.state(),
            transitions=self.memory.transitions,
            snapshots=self.memory.snapshots,
        )
        self.telemetry.predictions += 1
        if result.status is PredictionStatus.MODEL_UNAVAILABLE:
            self.telemetry.prediction_unavailable += 1
        self._announce_prediction(result)
        return result

    def _announce_prediction(self, result: PredictionResult) -> None:
        if self.observer is None:
            return
        try:
            if result.status is PredictionStatus.FAILED:
                self.observer(
                    PREDICTION_FAILED,
                    {"status": result.status.value, "error": result.reason},
                )
                return
            self.observer(
                PREDICTION_COMPLETED,
                {
                    "status": result.status.value,
                    "provider": result.provider,
                    "rule_based": result.rule_based,
                },
            )
        except Exception:  # noqa: BLE001 - an announcement never fails the prediction
            return

    # ── persistence ──────────────────────────────────────────────────────────
    def payload(self) -> dict[str, Any]:
        """The stored shape of this world — references and records, never content."""
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": PHASE,
            "world_id": self.world_id,
            "saved_at": self._clock(),
            "policy": {
                "missing_after": self.policy.missing_after,
                "stale_after_seconds": self.policy.stale_after_seconds,
                "entity_ttl_seconds": self.policy.entity_ttl_seconds,
                "identity_iou_threshold": self.policy.identity.iou_threshold,
                "identity_distance_px": self.policy.identity.distance_px,
                "position_change_px": self.policy.thresholds.position_px,
                "confidence_change": self.policy.thresholds.confidence_delta,
            },
            **self.memory.to_dict(),
        }

    def save(self) -> bool:
        """Persist the world. Returns whether it landed; never raises."""
        if self.repository is None:
            return False
        ok = bool(self.repository.save(self.world_id, self.payload()))
        self._last_save_ok = ok
        if ok:
            self._last_saved_at = self._clock()
            self.telemetry.saves += 1
        return ok

    def restore(self) -> RestoreReport:
        """Load a stored world, reporting what was recovered and what was not (§12).

        A missing or unreadable file is not an error: it reports ``restored=False``
        with the reason, and the world simply starts empty. A payload with some
        unreadable rows restores the readable ones and COUNTS the skipped ones —
        "restored" is never claimed on the strength of a file having existed.
        """
        if self.repository is None:
            return RestoreReport(
                restored=False,
                reason="no world store is configured, so there is nothing to restore",
            )
        payload = self.repository.load(self.world_id)
        if not payload:
            return RestoreReport(
                restored=False,
                reason=(
                    "the store holds nothing for this world (absent, empty or unreadable); "
                    "the state starts empty"
                ),
            )
        stored_world = as_text(payload.get("world_id"))
        if stored_world and stored_world != self.world_id:
            return RestoreReport(
                restored=False,
                reason=(
                    f"the stored payload belongs to world {stored_world!r}, not "
                    f"{self.world_id!r}; nothing was loaded"
                ),
                skipped_records=0,
            )
        stored_schema = as_text(payload.get("schema_version"))
        skipped, reasons = self.memory.restore(payload)
        state = self.memory.state
        self.telemetry.restores += 1
        report = RestoreReport(
            restored=state is not None,
            reason=(
                ""
                if state is not None
                else "the payload held no readable state version"
            ),
            state_id=state.state_id if state is not None else "",
            version=state.version if state is not None else 0,
            transitions=len(self.memory.transitions),
            snapshots=len(self.memory.snapshots),
            observations=len(self.memory.observations),
            skipped_records=skipped,
            skipped_reasons=reasons,
        )
        if stored_schema and stored_schema != SCHEMA_VERSION:
            report = RestoreReport(
                restored=report.restored,
                reason=report.reason
                or (
                    f"the payload was written by schema {stored_schema}; it was read "
                    f"tolerantly by schema {SCHEMA_VERSION}"
                ),
                state_id=report.state_id,
                version=report.version,
                transitions=report.transitions,
                snapshots=report.snapshots,
                observations=report.observations,
                skipped_records=report.skipped_records,
                skipped_reasons=report.skipped_reasons,
                pruned=report.pruned,
            )
        if report.restored and self.observer is not None:
            # An announcement never fails a restore: a store that came back is
            # a fact worth reporting, and a broken watcher is not worth failing
            # a request over.
            with contextlib.suppress(Exception):
                self.observer(
                    STATE_RESTORED,
                    {"state_id": report.state_id, "version": report.version},
                )
        return report

    def prune(self) -> int:
        """Apply retention explicitly and return how many records were dropped."""
        return self.memory.retain(self._clock())

    def clear_state(self) -> None:
        """Forget this world entirely — the authorized state-management operation (§20).

        Not exposed over the API: inspection is read-only, and a world that could be
        wiped by an HTTP call is a world an operator cannot rely on. This exists for
        an operator's CLI, a test, and a deployment that tears a world down.
        """
        self.memory.clear()
        self._seen.clear()
        self._clock_watermark.clear()

    # ── surfaces ─────────────────────────────────────────────────────────────
    def _over_ceiling(self, current: WorldState | None) -> int:
        """How many live entities the state holds above its declared ceiling.

        Read from the state the estimator already stamped, so a status poll costs
        nothing and cannot disagree with what generation recorded.
        """
        if current is None:
            return 0
        recorded = current.metadata.get("entities_over_ceiling")
        try:
            value = int(recorded or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    def status(self) -> dict[str, Any]:
        """What this world is and holds right now — cheap, and completely probe-free.

        Every figure here is read from objects the engine already owns: the memory,
        the policy table, the prediction provider's own attributes. Nothing opens a
        socket, touches a disk or loads a model, so this is safe on a polled path.
        """
        current = self.memory.state
        return {
            "phase": PHASE,
            "schema_version": SCHEMA_VERSION,
            "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
            "world_id": self.world_id,
            "version": current.version if current is not None else 0,
            "state_id": current.state_id if current is not None else "",
            "timestamp": current.timestamp if current is not None else "",
            "observed": current is not None,
            "entities": len(current.entities) if current is not None else 0,
            # Live entities are never destroyed to satisfy max_entities, so a stream
            # of genuinely new ones can leave the state above the declared ceiling.
            # Reporting the overage here is what keeps the policy number and the
            # count beside it from contradicting each other silently (report §25.9).
            "entities_over_ceiling": self._over_ceiling(current),
            "relationships": len(current.relationships) if current is not None else 0,
            "conditions": len(current.conditions) if current is not None else 0,
            "uncertainty": len(current.uncertainty) if current is not None else 0,
            "memory": self.memory.view().to_dict(),
            "policy": {
                "missing_after": self.policy.missing_after,
                "stale_after_seconds": self.policy.stale_after_seconds,
                "entity_ttl_seconds": self.policy.entity_ttl_seconds,
                "identity_iou_threshold": self.policy.identity.iou_threshold,
                "identity_distance_px": self.policy.identity.distance_px,
                "identity_ambiguous_margin": self.policy.identity.ambiguous_margin,
                "position_change_px": self.policy.thresholds.position_px,
                "confidence_change": self.policy.thresholds.confidence_delta,
                "max_entities": self.policy.max_entities,
            },
            "retention": self.retention.to_dict(),
            "prediction": self.prediction.availability(),
            "persistence": {
                "store_wired": self.repository is not None,
                "autosave": self.autosave,
                "last_saved_at": self._last_saved_at,
                "last_save_ok": self._last_save_ok,
            },
            "telemetry": self.telemetry.to_dict(),
            "transition_summary": transition_factory.summary(self.memory.transitions),
            "capabilities": list(self.capabilities()),
        }

    def capabilities(self) -> tuple[dict[str, Any], ...]:
        """The capability table with live availability, as plain rows."""
        return tuple(row.to_dict() for row in capability_rows(self))

    def state_summary(self) -> dict[str, Any]:
        """A bounded, content-light view of the current state (§20).

        Labels, statuses and counts — never the attribute values, which can carry
        content a caller may not be entitled to. A caller that wants a value asks
        the query layer for it.
        """
        current = self.memory.state
        if current is None:
            return {"observed": False, "version": 0, "state_id": "", "entities": []}
        return {
            "observed": True,
            "world_id": current.world_id,
            "state_id": current.state_id,
            "version": current.version,
            "timestamp": current.timestamp,
            "previous_state_id": current.previous_state_id,
            "sources": list(current.sources),
            "confidence": current.confidence,
            "entities": [
                {
                    "entity_id": item.entity_id,
                    "label": item.label,
                    "entity_type": item.entity_type,
                    "status": item.status.value,
                    "bbox": item.bbox.to_dict() if item.bbox is not None else None,
                    "observed_count": item.observed_count,
                    "last_seen": item.last_seen,
                    "provisional": item.provisional,
                    "identity_confidence": item.identity_confidence,
                }
                for item in current.entities
            ],
            "relationships": [
                {
                    "relationship_id": item.relationship_id,
                    "kind": item.kind.value,
                    "source_entity_id": item.source_entity_id,
                    "target_entity_id": item.target_entity_id,
                    "status": item.status.value,
                    "relation_source": item.relation_source.value,
                    "confidence": item.confidence,
                }
                for item in current.relationships
            ],
            "uncertainty": [item.to_dict() for item in current.uncertainty],
        }

    # ── internals ────────────────────────────────────────────────────────────
    def _empty_state(self, timestamp: str) -> WorldState:
        """Version 0: a world that has not been observed yet, which is a real state."""
        return WorldState(
            world_id=self.world_id,
            version=0,
            timestamp=timestamp or self._clock(),
            metadata={"empty": True},
        )
