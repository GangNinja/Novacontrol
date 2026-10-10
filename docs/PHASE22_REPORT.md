# Phase 22 Report — World Model, Memory & State Reasoning

## Summary

Phase 22 turns NovaControl's perception output into a **structured, uncertainty-aware
state of the environment**: a versioned world state that is updated from
observations, remembered over time, queried both currently and historically,
reasoned about through named deterministic rules, and — when nothing can honestly
answer — says so instead of guessing.

It is built **on** Phase 21 and on the layers before it, never beside them. Phase
21's `PerceptionResult` and `SceneRepresentation` become normalized observations;
Phase 21's own spatial arithmetic is what decides a geometric relation;
NovaControl's existing `JsonStateStore` persists a world; the application's ONE
event bus carries the announcements; the ONE `CapabilityRegistry` holds the
capability table; the SAME `ResourceGovernor` stands in front of any model-backed
prediction. What is new here is everything the phase is about:

```
Phase 21 PerceptionResult / SceneRepresentation / external source
        │
    observation normalization       world/ingest.py       content key, ordering, nothing claimed → rejected
        │
    state estimation                world/estimation.py   identity, attributes, absence, expiration
        │
    world state                     world/models.py       entities, relationships, conditions, uncertainty
        │
    entity tracking                 world/entities.py     source ids, IoU + distance, provisional when ambiguous
        │
    relationship graph              world/relationships.py  durable relations over time (Phase 21 geometry)
        │
    temporal memory                 world/memory.py       bounded snapshots + observation references
        │
    state transitions               world/transitions.py  what changed between versions, with evidence
        │
    change detection                world/changes.py      differences above stated thresholds
        │
    state query                     world/queries.py      11 structured questions, current and historical
        │
    state reasoning                 world/reasoning.py    7 named deterministic rules, with limitations
        │
    prediction interface            world/prediction.py   a provider that may not exist, and says so
        │
    structured result + confidence + provenance   →  status(), overview(), the event bus, the API
```

Six rules are load-bearing, and each is enforced in code rather than in prose:

1. **A fact that was not observed is never stored as an observed fact.** Every
   attribute carries a `FactBasis` (`observed` / `inferred` / `predicted` /
   `unknown`) and the evidence reference that produced it.
2. **A figure that was not measured is `None`, never a zero.** An unmeasured
   latency, a fresh entity's `identity_confidence`, an unprobed availability —
   all `None`, and every reader treats `None` as "cannot tell", not as success.
3. **Absence from a partial observation proves nothing.** Only a complete
   observation can move an entity off `present`, and only after the policy's
   `missing_after` count.
4. **A historical question is never answered with the current state.** `state_at`
   returns `NOT_FOUND` with a limitation that says the answer was not substituted.
5. **No predictive model ships.** The only projection available is a rule
   projection, labelled `rule_based=True` with `confidence=None`, and it is off
   by default.
6. **Nothing here acts, trains, or downloads anything.** `action_execution`,
   `trains_anything`, `automatic_model_loading`, `automatic_model_downloads`,
   `stores_raw_frames` and `stores_hidden_reasoning` are all `false` in
   `world.overview()`.

**Verdict: PASS**, with `world.prediction` classified `PROVIDER_DEPENDENT`
on purpose (see §16 and §26).

## 1. Objective

Build a structured, uncertainty-aware representation of the environment that can be
updated from observations, maintained over time, queried, and used to reason about
the current state — extending the real Phase 21 implementation, recreating nothing
that already exists, and never claiming a learned world model where only a state
store exists.

The nine usage workflows the phase names (A–I, exercised as real test classes in
§23) are the acceptance criteria: initial observation, state update, entity
continuity, historical query, conflicting observations, restart and recovery,
evidence query, prediction boundary, resource fallback.

### Files added

| Path | What it owns |
| --- | --- |
| `src/novacontrol/world/models.py` | The value types and the vocabularies: `Observation`, `ObservedEntity`, `EntityAttribute`, `WorldEntity`, `WorldRelationship`, `WorldState`, `UncertaintyRecord`, `WorldStateTransition`, `ChangeEvent`, `AttributeChange`, `StateEstimate`, `UpdateReport`, `RestoreReport`, `StateQuery`/`StateQueryResult`, `ReasoningConclusion`, `PredictionRequest`/`Result`, `EvidenceReference`, and the 13 enums (`FactBasis`, `ObservationSource`, `IngestStatus`, `EntityStatus`, `TransitionKind`, `ChangeKind`, `UncertaintyKind`, `QueryKind`, `QueryStatus`, `PredictionStatus`, `RelationshipKind`, `RelationshipSource`, `RelationStatus`). |
| `src/novacontrol/world/timeutil.py` | Timestamp parsing and arithmetic in one place: `parse_timestamp`, `seconds_between`, `is_newer`, `timestamps_in_order` — the only module that decides what a moment means. |
| `src/novacontrol/world/ingest.py` | `normalize`, `validate`, `observation_from_perception`, `entities_from_scene`, `facts_from_scene`, `scene_relationships`, `ObservationRejected` — the door facts enter through. |
| `src/novacontrol/world/entities.py` | `resolve_identity`, `merge_entity`, `expire_stale`, `active_entities`, `IdentityPolicy` — identity and the four-state life cycle. |
| `src/novacontrol/world/relationships.py` | Durable relations: `derive_geometric_relations`, `RelationshipOutcome`, `for_entity`, `for_kind`, and the Phase 21 kind mapping. |
| `src/novacontrol/world/estimation.py` | `estimate`, `EstimationPolicy`, `EstimationOutcome` — the reconciler, and `EPHEMERAL_CONDITIONS`. |
| `src/novacontrol/world/transitions.py` | `between_states` and the transition factory — what changed between two versions, each row with evidence. |
| `src/novacontrol/world/changes.py` | `detect_changes`, `ChangeThresholds`, `MAX_CHANGES` — differences above the stated thresholds. |
| `src/novacontrol/world/memory.py` | `TemporalMemory`, `WorldRetentionPolicy`, `WorldMemoryView` — bounded snapshots, transitions and observation references, plus `restore`. |
| `src/novacontrol/world/store.py` | `WorldRepository` protocol, `JsonWorldRepository` (over the shared `JsonStateStore`), `InMemoryWorldRepository`, `sanitize_world_id`. |
| `src/novacontrol/world/queries.py` | `run_query` and the 11 query kinds — bounded, content-light, never substituted. |
| `src/novacontrol/world/reasoning.py` | `_RULES` (7 rows), `_answer`, `ReasoningRule` — named deterministic rules with limitations. |
| `src/novacontrol/world/prediction.py` | `PredictionService`, `NoPredictionProvider`, `RuleProjectionProvider`, `DEFAULT_MAX_HORIZON_SECONDS` — the honest prediction boundary. |
| `src/novacontrol/world/capabilities.py` | `CAPABILITY_TABLE` (12 rows), `capability_rows`, `register_world_capabilities`, `CAPABILITY_SCHEMA_VERSION`. |
| `src/novacontrol/world/engine.py` | `WorldModelEngine`, `WorldTelemetry` — the walk, the announcements, telemetry, save/restore, `status()`, `overview` flags. |
| `src/novacontrol/world/agentic.py` | `world_observation`, `attach_world_state`, `environment_state_for`, `observe_perception`, `WORLD_OBSERVATION_KIND` — the Phase 20 adapter, and nothing else. |
| `src/novacontrol/world/__init__.py` | The package surface, `PHASE`, `SCHEMA_VERSION`, `DEFERRED_PHASES`, `overview()`. |
| `tests/test_world_model.py` | 2,894 lines, 254 tests + 28 subtests across 29 test classes (plus 11 doubles) — §23. |
| `docs/PHASE22_REPORT.md` | This file. |

Package size: **16 modules + `__init__`, 9,121 lines.**

### Files modified

| File | Change |
| --- | --- |
| `src/novacontrol/core/config.py` | `WorldModelSettings` (19 policy fields + `to_mapping`/`from_mapping`), `MAX_WORLD_ID`, `MAX_WORLD_ENTITIES`, `NovaControlConfig.world`, and the `_count_setting` / `_ratio_setting` helpers. |
| `src/novacontrol/core/events.py` | 9 `EventType.WORLD_*` members and their `EVENT_PAYLOAD_FIELDS` rows. |
| `src/novacontrol/application.py` | `_build_world_model`, `_world_event`, `self.world`, `register_world_capabilities`, `persist()` → `world.save()`, boot `restore()`, `status()["world_model"]`. |
| `src/novacontrol/api/app.py` | `_observation_batch` and the 5 `/world/*` handlers. |
| `src/novacontrol/api/models.py` | 5 `ApiRoute` rows. |
| `src/novacontrol/api/route_consumers.py` | 5 `no-render` consumer rows. |
| `src/novacontrol/telemetry/service.py` | `_world_summary` and the flat `status()["world_model"]` slice. |
| `docs/API.md` | Regenerated: 215 routes. |
| `README.md`, `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md`, `AGENTS.md` | Phase 22 status, learnings and log entries. |

## 2. Existing Components Reused

Nothing in this list was recreated, and each reuse is load-bearing rather than
decorative:

| Reused | How |
| --- | --- |
| `persistence/json_store.py::JsonStateStore` | The world store: staged temp file + `os.replace`, forgiving reads, one key per world. `JsonWorldRepository` is a 40-line adapter over it, not a second persistence mechanism. |
| `core/events.py` (`EventBus`, `EventType`, `EVENT_PAYLOAD_FIELDS`, `Event.of`) | Every announcement goes through the injected observer seam into the ONE bus. The 9 new payload field sets are validated by the same machinery as the rest. |
| `intelligence/intent.py::CapabilityRegistry` + `CapabilityAvailability` | `register_world_capabilities` declares 12 capabilities through `register_action`, the same door Phase 21 uses; live availability is read from the engine, never guessed. |
| Phase 21 `perception/models.py` (`BBox`, `DetectedObject`, `SceneRepresentation`, `PerceptionResult`, `PerceptionStatus`) | `observation_from_perception` converts them; `BBox` arithmetic (`center`, `iou`, `gap_to`) is what identity and geometry use. |
| Phase 21 `perception/spatial.py::derive_relationships` | A geometric relation that was not stored is derived through Phase 21's own function, so the world model and the perception layer cannot disagree about "left of". |
| Phase 14 `optimization` governor + `HardwareMonitor` | `PerceptionResourceGate` is the injected `gate` in front of any model-backed prediction; the same monitor instance the rest of the build reads. |
| `agentic/models.py::AgentState` | `attach_world_state` uses `AgentState.advanced(observation=…)`; the world does not get its own agent-state type. |
| `agentcore` bug log / telemetry service | Status surfaces are extended in place; `_world_summary` reads the status document the call above already built, with no extra probe. |

## 3. Architecture Implemented

The engine is one orchestrator with every collaborator injected and every default
honest:

```
WorldModelEngine
  ├─ observe(value) ──► ingest.normalize/validate ──► estimation.estimate
  │                        (content key, ordering)      (identity, attributes,
  │                                                      conditions, uncertainty)
  │                     ◄── UpdateReport (status, transitions, changes, evidence)
  │                        └─► memory.set_state / record_transitions / retain
  │                        └─► _announce(9 event types)
  ├─ query(StateQuery)  ──► queries.run_query   → StateQueryResult
  ├─ reason(question)   ──► reasoning._RULES    → tuple[ReasoningConclusion, ...]
  ├─ predict(request)   ──► PredictionService   → PredictionResult
  ├─ state/snapshots/transitions/changes/state_summary
  ├─ save/restore/prune/clear_state
  └─ status()/capabilities()/payload()
```

* **Ingestion is one door.** `observe()` accepts a mapping, an `Observation`, a
  Phase 21 `PerceptionResult` or a bare `SceneRepresentation`, and normalizes it in
  `ingest.py`. Nothing else writes to the state.
* **Estimation is one reconciler.** `estimate()` takes the previous state and the
  new observation and returns an `EstimationOutcome` — the new state, the
  transitions, the changes and the uncertainty rows. There is no second path that
  mutates entities.
* **Reads are one layer.** `queries.py` is the read surface; `reasoning.py` calls
  into it rather than walking the state itself; `changes.py` derives from the
  transition log rather than re-comparing states.
* **The engine is not a scheduler.** `observe()` is synchronous and returns a
  report; there is no thread, no worker and no timer in the package.
* **Telemetry is counters only.** `WorldTelemetry` counts ingestions, rejects,
  duplicates, stale and out-of-order observations, transitions, changes, conflicts,
  queries, reasoning requests, predictions, restores and saves, plus
  `last_ingest_ms` (`None` until something was ingested).

## 4. World-State Model

`WorldState` is the phase's central value: `world_id`, `state_id`, `version`,
`timestamp`, `previous_state_id`, `entities`, `relationships`, `conditions`,
`uncertainty`, `evidence`, `sources`, `confidence`, `schema_version`, `metadata`.

* **Version 0 is the honest empty state.** `WorldModelEngine.state()` never returns
  `None`; a world that has never been observed returns version 0 with
  `metadata["empty"] is True` and `status()["observed"] is False`, so a caller
  cannot confuse "not wired" with "nothing happened yet".
* **Entities carry provenance, not just values.** `WorldEntity` holds
  `entity_id`, `entity_type`, `labels`, `status`, `bbox`, `attributes` (each an
  `EntityAttribute` with `name`, `value`, `basis`, `confidence`, `evidence`,
  `observed_at`, `source`), `first_seen`, `last_seen`, `last_confirmed`,
  `observed_count`, `missed_observations`, `provisional`, `provenance`,
  `source_ids`, `scope`, and `identity_confidence` — `None` until an identity was
  actually matched.
* **Conditions are the state's own non-entity facts** (`entity_count`,
  `perception_status`, …), each with a basis; `EPHEMERAL_CONDITIONS` marks the ones
  that describe one look rather than the world.
* **Uncertainty is first-class.** `UncertaintyRecord` rows are stored on the state
  with `subject`, `kind`, `detail`, `confidence`, `evidence` and `recorded_at` —
  `low_confidence`, `conflicting`, `stale`, `ambiguous_identity`, `unmeasured`,
  `out_of_order`.
* **Relationships are durable.** They are not re-derived per read: a relation has
  `kind`, `status` (`active` / `stale` / `retracted`), `relation_source`,
  `confidence`, `detail`, `evidence` and its `valid_from`/`valid_until`, and a stale
  relation is kept and labelled rather than deleted.

## 5. Observation Ingestion

`ingest.py` produces an `Observation` the estimator can trust, and refuses exactly
one thing: an observation that claims nothing.

* **Normalization** accepts a mapping, an `Observation`, a Phase 21
  `PerceptionResult`, or a bare `SceneRepresentation`. Unknown keys are ignored;
  a malformed entity row is dropped and counted in metadata (`PARTIAL`) rather
  than cancelling the observation.
* **Not a rejection:** a missing timestamp (accepted, facts marked as carrying no
  readable time, and an uncertainty row recorded instead), an unrecognized source
  name (the label is metadata), and one unusable row.
* **Rejection:** `validate()` raises `ObservationRejected` when an observation
  claims nothing — no entities, no facts, and no condition. The engine turns that
  into `IngestStatus.REJECTED` and the HTTP route into a 422.
* **Duplicates are detected by content key.** `Observation.content_key()`
  fingerprints what the report CLAIMS — source, source id, scope, timestamp,
  completeness, entities and facts, never the metadata — so a retry with a new id
  and the same content is still a duplicate. The engine keeps a bounded dedupe
  window; a repeat within it is `DUPLICATE` and does not advance the version.
* **Ordering is decided in one place** (`engine._ordering`): an observation older
  than the current state's clock is out of order (recorded as such, not silently
  applied), and a replay from a different clock domain is `stale`.
* **Phase 21 conversion** maps scene objects and tracks to observed entities with
  their ids as identity hints, and does **not** copy scene text unless
  `include_text=True`.
* **Batches.** `_observation_batch` (API) accepts a single observation, an
  `observations` list with shared keys merged into each row, or a
  `perception`/`scene` payload; an empty list falls back to the whole body.

## 6. Entity Tracking

Identity is *hinted*, then *measured*, then *stated* — never assumed:

* A source or track id from Phase 21 is the strongest hint.
* Otherwise `resolve_identity` matches by IoU (`identity_iou_threshold`, default
  0.35) and centre distance (`identity_distance_px`, default 40 px), and a match
  that is close to two candidates within `identity_ambiguous_margin` (0.25) is
  **provisional**: the entity is kept, and an `ambiguous_identity` uncertainty row
  is recorded rather than asserting which came first.
* A new entity's `identity_confidence` is `None` — nothing measured it — never a
  default of 0.0 or 1.0.
* The four-state life cycle is `present` → `not_observed` → `missing` → `expired`:
  absence is only registered after a **complete** observation and only once the
  policy's `missing_after` count (default 2) is exceeded; `entity_ttl_seconds`
  (default 900) expires an entity, and the evidence is kept.
* Movement is measured, not inferred: a move beyond `position_change_px`
  (default 8 px) with a shared identity is an `entity_moved` transition, and 4 px
  is not.
* Ceilings: an observation is capped at `MAX_INGESTED_ENTITIES` (128) rows and the
  state at `max_entities` (default 256 in the app, `MAX_WORLD_ENTITIES` 4096 as the
  hard configuration bound), with `max_observation_refs` bounding the evidence
  list. **A ceiling never destroys a live fact, and exceeding one is reported**:
  past the ingest ceiling the excess is counted as truncated (not as "unusable",
  §25.8), and past the state ceiling expired entities are retired first and any
  remaining overage is published as `status()["entities_over_ceiling"]`
  (§25.9).

## 7. Relationship Graph

* Relations are **durable rows** on the state (`located_near`, `inside`,
  `contains`, `observed_with`, `connected_to`, `owned_by`, `associated_with`), each
  with `kind`, `status`, `relation_source` (`geometric` / `stated` / `inferred` /
  `external`), `confidence`, `detail`, `evidence` and its validity window.
* **Geometry is Phase 21's.** `derive_geometric_relations` calls Phase 21's
  `derive_relationships`, so "left of"/"inside" mean the same thing in both layers
  and cannot drift.
* **A lost relation is labelled, not deleted.** When newer evidence stops
  supporting a relation it becomes `stale` with a `relationship_changed`
  transition, and a contradiction retracts it.
* `derive_relationships` in config (default `True`) is the switch; with it off,
  only relations explicitly present in an observation are stored.
* `query(kind="relationships")` filters by `kind`/`entity_id`, and the reasoning
  rule `spatial_relation` uses a stored `active` relation first and Phase 21's
  arithmetic only as a fallback — and its conclusion always carries the limitation
  "a spatial relation is a claim about a moment, not about the world".

## 8. State Estimation

`estimation.py` is the reconciler, and its policy is the phase's caution made
numeric (`EstimationPolicy` / `WorldModelSettings`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `missing_after` | 2 | Complete observations an entity may be absent before it is `missing`. |
| `stale_after_seconds` | 60 | How old a fact may be before it is reported stale. |
| `entity_ttl_seconds` | 900 | When an absent entity expires (evidence retained). |
| `identity_iou_threshold` / `identity_distance_px` / `identity_ambiguous_margin` | 0.35 / 40 / 0.25 | Identity matching and when it stays provisional. |
| `position_change_px` / `confidence_change` | 8 / 0.1 | The change thresholds. |

Each estimate produces: the new state, its transitions, its changes, and its
uncertainty rows. An attribute's basis is `observed` when the value came from the
observation, `inferred` when it was carried forward or derived, and the evidence
reference is stored with it. `EPHEMERAL_CONDITIONS` (`perception_status`) are
dropped on carry-forward and ignored when computing changed conditions, so a failed
look can never linger as a stale success — or the reverse.

## 9. State Transitions

Every version bump records what changed, in `TransitionKind` vocabulary:
`entity_added`, `attribute_changed`, `entity_moved`, `relationship_changed`,
`entity_not_observed`, `entity_confirmed_missing`, `entity_expired`,
`state_conflict`, `scene_changed`.

* Each `WorldStateTransition` carries `transition_id`, `kind`, the
  `previous_state_id`/`current_state_id`, `timestamp`, `entity_id` where it
  applies, the `attribute` it concerns, `previous`/`current` values, `detail`,
  `confidence`, `evidence` and its `world_id` — so a transition explains itself
  without re-reading the state.
* `between_states(previous, current)` is one function, and the engine's
  `transitions(limit=, since=, until=, kind=, entity_id=)` filters the log rather
  than recomputing anything.
* The log is bounded (`max_transitions`, default 500, `MAX_TRANSITIONS` from
  config) and `retention_seconds` (86400) ages rows out.
* `status()["transition_summary"]` gives total, by-kind and the newest/oldest
  timestamps, so a UI can show activity without reading rows.

## 10. Temporal Memory

`TemporalMemory` holds three bounded histories plus the current state:

* **Snapshots** — up to `max_snapshots` (default 32) full versions, newest last;
  `state_at(timestamp)` reads one, and `state_by_id` by id.
* **Transitions** — the durable change log (§9).
* **Observation references** — up to `max_observation_refs` (default 128)
  `EvidenceReference` rows (`evidence_id`, `kind`, `source`, `detail`,
  `timestamp`), never observation bodies.
* **Retention** — `WorldRetentionPolicy.retain()` prunes by count and by age, and
  `prune()` is callable from the engine and returns how many rows it dropped.
* **Isolation** — memory is per `world_id`, and the store key is
  `world_<sanitized id>`, so a fact from one environment cannot appear in another.
* Historical reads are honest: `state_at` on a moment older than the retention
  window returns `NOT_FOUND` with a limitation that says the value was **not
  substituted** from the current state.

## 11. Persistence and Recovery

* **The store is the shared one.** `JsonWorldRepository` wraps the application's
  `JsonStateStore` (staged temp file, `os.replace`, forgiving reads) with a
  three-method `WorldRepository` protocol (`load`/`save`/`clear`), plus an
  `InMemoryWorldRepository` for tests and no-disk mode.
* **`world_id` is sanitized** before it forms a filename (`sanitize_world_id`:
  blank → `default`, everything unsafe → `_`, capped at `MAX_WORLD_ID` 64), and the
  result is deterministic so the same caller reopens the same world.
* **Saving is never fatal.** `save()` returns `bool` and swallows store exceptions;
  `status()["persistence"]` reports `last_saved_at` and `last_save_ok`, where a
  `None` `last_save_ok` means "nothing has been saved yet".
* **Restoring reports what it could not read.** `TemporalMemory.restore` reads the
  payload row by row, skips what it cannot parse and counts it
  (`RestoreReport.skipped_records` + `skipped_reasons`); a payload that belongs to a
  different world is refused by name, and an older schema (`phase22.0`) is tolerated
  and named in the reason. A damaged payload (a non-mapping, garbage rows) restores
  a smaller-but-valid world instead of failing.
* **The application wires it**: `self.state_store = JsonStateStore(self.data_dir)`
  → `_build_world_model()` → `engine.restore()` on boot when enabled, and
  `persist()` calls `world.save()`.
* **The app constructs the engine with `autosave=False`** (even though the
  `WorldModelSettings` default is `True`), deliberately: the cheap ingest path must
  not pay a disk write per observation, so a world is written on `persist()`
  (shutdown and explicit saves) instead. With the config section disabled the
  engine is built with **no repository at all**, so the status surface says "no
  world store is configured" and nothing is written — the switch means "do not
  remember across restarts", not "do not work".

## 12. Change Detection

* `changes.py::detect_changes` compares two versions above stated thresholds
  (`ChangeThresholds`, `MAX_CHANGES`) and produces `ChangeEvent`s in `ChangeKind`
  vocabulary: `entity_appeared`, `entity_disappeared`, `attribute_changed`,
  `position_changed`, `relationship_changed`, `state_stale`, `observation_conflict`,
  `confidence_changed`, `environment_changed`.
* **The engine's `changes()` reads the transition log**, so a change is what a
  transition of a change-making kind actually recorded — it never re-compares
  states and cannot disagree with the transition history. Its rows' `kind` is the
  `TransitionKind` value that produced them, on purpose: the log is the source.
* **Identical observations produce no changes.** `detect_changes(previous,
  current)` returns `()` for the same content, and the `UpdateReport.changes` is
  `()` too — an identical observation is a `DUPLICATE` and bumps nothing.

## 13. State Queries

`queries.py::run_query` answers 11 `QueryKind`s, all bounded, with one of four
`QueryStatus`es (`ok` / `empty` / `not_found` / `invalid`):

| Kind | Answers |
| --- | --- |
| `entities` | What is present now (with `status`, `label`, `min_confidence`, `limit` filters). |
| `entity` | One entity by id — `NOT_FOUND` when it was never seen. |
| `entity_history` | That entity's transitions over time. |
| `entity_seen` | Was it ever seen, and when. |
| `relationships` | Relations, filtered by kind and entity. |
| `changes_since` | Change rows in a window (`INVALID` on an unreadable window). |
| `state_at` | The retained version at a moment — never substituted. |
| `uncertain` | Everything the state knows it does not know. |
| `stale` | Facts whose latest evidence stopped supporting them. |
| `evidence` | The references behind a claim. |
| `diff` | What differs between two retained versions (`NOT_FOUND` on a bad id). |

* **Bounds are real.** Every read is bounded by `limit` (capped at 200 in the
  query layer), plus `MAX_CHANGES`, `max_entities` and per-source caps in the
  evidence read — no query kind walks the store unbounded; rows are summaries, and
  `StateQueryResult` carries `count`, `rows`, `reason`, `evidence` and
  `limitations` so a query that could not say everything (or could not answer at
  all) says why.
* **`min_confidence` excludes unmeasured values** rather than treating `None` as
  low: filtering a fresh entity (whose `identity_confidence` is `None`) returns
  nothing, honestly.
* **An unresolved subject does not become "no filter".** `evidence` for a label
  that never resolved anchors on the unresolved label and returns `NOT_FOUND`, so
  a question about something that does not exist cannot return unrelated rows.
* Historical reads (`state_at`, `diff`, `changes_since`) work only over retained
  versions and say `NOT_FOUND`/`INVALID` — with a limitation naming why — when the
  window is outside the memory.

## 14. State Reasoning

`reasoning.py` is **bounded, named, deterministic reasoning over the recorded
state** — not an LLM, not a planner, not a guess. Seven rules, each with a
description, explicit limitations and self-registering examples:

| Rule | Question shape | Limitation it always states |
| --- | --- | --- |
| `spatial_relation` | "is the phone left of the laptop" | a spatial relation is a claim about a moment, not about the world |
| `last_seen` | "where was the phone last observed" | the record is only as fresh as the last observation |
| `stale` | "what is stale" | a stale fact was true when it was recorded; stale is not false |
| `conflict` | "do any observations conflict" | the state records the disagreement rather than resolving it |
| `uncertainty` | "what is uncertain" | only recorded doubts appear here |
| `provisional_identity` | "which identities are provisional" | a provisional identity may be one thing or two |
| `evidence` | "why is the laptop believed to be here" | evidence points at observations; their content is not copied here |

* Each `ReasoningConclusion` carries `rule`, `category`, `conclusion`,
  `confidence` (`None` when nothing measured it), `evidence`, `limitations`,
  `state_id`, `subject` and a structured `payload`.
* **A question that matches nothing is answered `unmatched`** and says which
  question shapes exist — it does not fall back to a generic answer.
* **No hidden chain-of-thought exists here.** `ReasoningConclusion` has no
  `reasoning` / `thoughts` / `chain_of_thought` / `scratchpad` field, and the
  recorder in Phase 15 stores structured payloads only.
* The API route accepts `payload` with an explicit `action` + `subject`, which
  yields exactly one conclusion for a caller that already knows what it wants.
* `mentioned_entities(question, state)` resolves the labels in a question against
  the state so a caller can bind subjects without re-parsing.

## 15. Prediction Interface

`prediction.py` is a provider boundary with an honest default:

* **`PredictionProvider` protocol** — one method, `predict(request, context)`.
  Optional provider metadata (`name`, `available`, `needs_model`) is read with
  defaults by `PredictionService.availability()`, so a minimal provider is a single
  method and nothing else has to exist.
* **`NoPredictionProvider`** is the default: `available = False`, and a request
  returns `PredictionStatus.MODEL_UNAVAILABLE` with provider `"none"` and a reason
  that says a learned world model is not shipped. (The *availability* reason says
  "does not ship a learned world model"; the *result* reason is the shorter
  "not available".)
* **`RuleProjectionProvider`** is the only projection that ships: it takes one
  measured displacement between two retained versions and projects it forward. Its
  result is `predicted`, `provider="rule-projection"`, `rule_based=True` and
  **`confidence=None`** — it is a rule, and it is labelled as one.
* **`PredictionService`** is the single caller of a provider, and it respects the
  injected resource gate **before** asking: a refused load is
  `RESOURCE_BLOCKED` and the provider is never called. It also bounds the horizon
  (`max_horizon_seconds`, default 300, clamped with a limitation that says so),
  applies a timeout, and maps a raising or misbehaving provider to `FAILED`.
* **Every ending has a status**: `predicted`, `unsupported`,
  `model_unavailable`, `insufficient_evidence`, `resource_blocked`, `failed`. No
  path fabricates a prediction.
* Wire-compatible with the rest of the build: a caller may inject any provider
  (including an Ollama-backed one) through `set_provider` without touching the
  engine.

## 16. Actual Predictive Capability

**This build maintains a state store. It has not learned a predictive world
model, and it does not claim one.**

* `world.overview()` reports `predicts_future_state: false`,
  `predictive_model_available: false`, `rule_projection_available: true`.
* The 12-row capability table classifies `world.prediction` as
  **`PROVIDER_DEPENDENT`**, and the live availability probe returns
  `available=False, provider="none"` with the reason "no predictive provider is
  wired; this build ships a state store, not a learned world model".
* The only projection that can be obtained is `RuleProjectionProvider`: a
  constant-velocity extrapolation from two measured versions, `rule_based=True`,
  `confidence=None`, and it is **off by default** (`settings.rule_projection`
  False) — the application wires it only when the setting is turned on.
* A rule projection requires two retained versions with a measured displacement;
  with one version it returns `INSUFFICIENT_EVIDENCE` rather than inventing a
  velocity, and the no-horizon case uses one measured interval as its horizon and
  says so in a limitation.
* Nothing downloads, loads or trains a model as part of prediction. Wiring an
  actual predictive model is the provider's job, and the interface is the only
  thing Phase 22 supplies for it.

## 17. EventBus Integration

Nine typed events, all through the injected `WorldObserver` seam (set with
`set_observer`) — never a direct bus lookup inside the package:

| Event | Payload fields |
| --- | --- |
| `world.observation_ingested` | `observation_id`, `source_kind`, `entities` |
| `world.state_updated` | `state_id`, `version`, `entities` |
| `world.transition_created` | `transition_id`, `kind`, `state_id` |
| `world.entity_changed` | `entity_id`, `change` |
| `world.relationship_changed` | `relationship_id`, `kind`, `status` |
| `world.conflict_detected` | `subject`, `detail` |
| `world.state_restored` | `state_id`, `version` |
| `world.prediction_completed` | `status`, `provider` |
| `world.prediction_failed` | `status`, `error` |

* **No payload key shadows the reserved envelope kwargs** (`source`,
  `correlation_id`, `causation_id`): `Event.of`/`Event.child` raise on that, and
  `world.*` uses `observation_id`/`source_kind` instead of `source`. (This is the
  exact failure mode that once made `tool.selected` silently disappear.)
* `_announce_soon` in the application builds `Event.of(...)` with the correlation
  id, so a world event can be grouped with the request that caused it; `observe()`
  falls back to the observation's own `correlation_id` when the caller passed none.
* An observer that raises cannot break ingestion: `_announce` wraps the emit.
* `EventBus.emit` is the door that cannot raise, so a world event is never a new
  way for an observation to fail.

## 18. API Integration

Five routes, added in the five places the project requires (handler, `ApiRoute`,
`ROUTE_CONSUMERS`, regenerated `docs/API.md`, and tests):

| Route | Behaviour |
| --- | --- |
| `GET /world/status` | Versions, entities, policies, retention, telemetry counters and the honest prediction posture. No content. |
| `GET /world/state` | Bounded, **content-light** view: labels, statuses and boxes — no attribute values. Params `entity_id`, `limit`. |
| `POST /world/observe` | One observation or a batch. 422 when the body carries none of `entities`/`facts`/`observations`/`perception`/`scene`, and 422 when a single observation is REJECTED. A batch returns `{batch, version, state_id, reports}`. |
| `POST /world/query` | A structured question; 422 on an `INVALID` query (a question that names no entity/label/subject for a kind that needs one). An unrecognised `kind` is a **documented fallback, not an error**: it is answered as the safest question (`entities`) and the result's `kind` field names the question that actually ran. |
| `POST /world/predict` | A future state request; reports `model_unavailable` when nothing is wired. |

* **Contract intact**: `routes == ROUTE_CONSUMERS` at **215 = 215**, with the five
  `/world/*` paths present and missing/extra both empty; the five consumers are
  declared `no-render` (no web panel consumes them yet).
* `docs/API.md` regenerated and verified in sync
  (`scripts/generate_api_reference.py --check` → `docs/API.md: in sync`).
* The world summary also rides the existing telemetry surface: `GET /status`
  exposes `body["app"]["world_model"]` as a **flat, UI-safe slice** (counts,
  `prediction_provider`, `prediction_available`) and never a body or an attribute
  value; `_world_summary` returns `{"available": false, "reason": …}` when the
  world model is not attached, so "not wired" and "nothing happened yet" stay
  distinguishable.

## 19. Phase 21 Integration

Phase 21 was read as implemented, not as originally specified, and the real
interfaces are what the conversion uses:

* `observation_from_perception` accepts a `PerceptionResult` **or** a bare
  `SceneRepresentation` (the second branch was a real defect — see §25.2).
* Scene objects and their tracks/ids become observed entities with their ids as
  identity hints; `BBox` arithmetic is used for identity and geometry.
* A geometric relation the state has not stored is derived through Phase 21's
  `derive_relationships`, with the perception kind names mapped to world kind
  names in one table (`_PERCEPTION_KINDS`).
* **A failed look is a fact.** A `PerceptionResult` with `scene=None` carries an
  `OBSERVED` condition fact `perception_status = {status, reason}`, so "the
  perception pipeline ran and could not see" is recorded rather than throwing the
  look away — and `EPHEMERAL_CONDITIONS` stops it from lingering.
* Scene text is **not** copied by default (`include_text=True` is the explicit
  opt-in). A state store that hoards screens is a privacy surface with no user.
* `perception/spatial.py` is the one spatial authority; the world model never
  re-implements "left of".

## 20. Phase 20 Compatibility

The bridge is an **adapter only** (`world/agentic.py`), and it adds no state and
no loop:

* `attach_world_state(agent_state, world_state)` returns an `AgentState` whose
  newest `observations` row has `kind == "world.state"`, built through the
  existing `AgentState.advanced(observation=…)` — so the agent's own action and
  verification lists are untouched (`previous_actions`, `verification_results`,
  `active_errors` all stay empty).
* `step_index` **advances**: attaching an observation is an advance, not an
  action, and the tests assert the advance rather than asserting 0.
* `environment_state_for(world_state)` produces a plain mapping under a `world`
  key — `observed`, `world_id`, `state_id`, `version`, `entities`,
  `relationships`, `uncertainty` and `labels` — which a Phase 20 consumer can read
  without importing anything from this package.
* `observe_perception` / `world_observation` give an async caller (Phase 21's
  `IsolatedAsyncioTestCase` path) the same conversion without a second ingestion
  route.

## 21. Resource Governance

* **The gate is injected, not invented.** The application passes the SAME
  `PerceptionResourceGate` (built over the shared `HardwareMonitor`,
  `ModelManager` and profile) that Phase 21 uses, so a load decision and a resource
  report cannot disagree about the free memory they saw.
* **The gate is asked before the provider, never after.** A refusal produces
  `PredictionStatus.RESOURCE_BLOCKED`, the provider is never called, and the
  limitation says the state work is unaffected — which is true: ingestion, queries
  and reasoning have no gate because they load nothing.
* **Nothing in this phase loads, downloads or trains a model.** Ingestion,
  estimation, memory, queries, reasoning and the rule projection are pure
  computation; `automatic_model_loading`, `automatic_model_downloads`,
  `cuda_required` and `trains_anything` are all `false`.
* **Ceilings instead of growth.** `max_entities`, `max_snapshots`,
  `max_transitions`, `max_observation_refs`, `retention_seconds` and the memory's
  `prune()` keep a long session bounded, and `MAX_WORLD_ID`/`MAX_WORLD_ENTITIES`
  cap what a request can name.
* **Measured cost is small** for the read/query path (§24): a 200-entity query is
  ~0.3 ms, a single-entity lookup ~0.009 ms and a reasoning call ~0.33 ms. The
  real cost is **persisting a large world's snapshot history** (~118 ms for a
  10.2 MB payload at 200 entities × 32 snapshots) — see §26.

## 22. Privacy and Safety

* **No raw frames, ever.** The package never sees pixels: it consumes Phase 21's
  structured scene, and `stores_raw_frames` is `false` in `overview()`.
* **No observation bodies.** Temporal memory stores `EvidenceReference` rows
  (source, id, timestamp, kind) — never the observation content — and scene text
  is excluded unless a caller explicitly opts in.
* **No hidden reasoning.** The schema has no reasoning/trace field, and the API's
  flat status slice exposes counts only. `exposes_chain_of_thought` is `false`.
* **Two read surfaces with different shapes, both deliberate.** `GET /world/state`
  is content-light: labels, statuses, boxes and confidences, **no attribute
  values** (its own docstring says to ask `/world/query` for those). `POST
  /world/query` is the value-bearing surface: an entity record carries each
  attribute's value *with* its basis, confidence, observed-at and evidence, which
  is what makes a claim checkable. `GET /world/status` and the telemetry slice
  return counts and policy numbers only. Both routes are behind the same
  authentication as every other endpoint.
* **Nothing acts.** `action_execution` is `false`; the package cannot run a tool,
  a controller or a command. `observe()` is recording only.
* **Isolation is enforced by key.** One world per sanitized id; a payload that
  belongs to a different world is refused at restore by name.
* **A refusal is a result, not an exception.** A rejected observation, an
  unreadable store, a raising provider, a broken gate and an unparseable payload
  all become statuses and reasons, so a caller never has to catch a state error to
  keep working.
* **A store that fails never fails the work**: `save()` returns `False` and the
  state in memory is unaffected.

## 23. Tests and Usage Workflows

`tests/test_world_model.py` — **2,894 lines, 254 tests + 28 subtests, 29 test
classes** (plus 11 doubles),
following the project's `unittest.TestCase` style with a double only where an
external boundary exists (`_RecordingObserver`, `_RaisingObserver`, `_DenyingGate`,
`_AllowingGate`, `_BrokenGate`, `_RaisingProvider`, `_WrongTypeProvider`,
`_EmptyPredictionProvider`, `_ModelBackedProvider`, `_UnavailableProvider`,
`_FakePerception`).

| Workflow | Class | What it exercises |
| --- | --- | --- |
| A — Initial observation | `WorkflowAInitialObservationTests` | First `observe()` → accepted v1, entities/relationships added, the exact transition list, the inferred `entity_count` condition; an unobserved world reports version 0/`empty`. |
| B — State update | `WorkflowBStateUpdateTests` | Attribute change → one `attribute_changed` with previous/current; identical content → `DUPLICATE`, version unchanged; a 40 px move with a shared id → `entity_moved`; 4 px → nothing (threshold 8). |
| C — Entity continuity | `WorkflowCEntityContinuityTests` | Partial absence leaves `present`; the first complete absence → `not_observed`; the second → `MISSING` + `entity_confirmed_missing`; a different label in the same place → 2 entities; a same label 900 px away → 2 entities; TTL expiry keeps evidence; a fresh entity's `identity_confidence is None`. |
| D — Historical query | `WorkflowDHistoricalQueryTests` | `state_at` returns the retained version; a moment outside retention → `NOT_FOUND` naming "NOT substituted"; an unreadable moment → `NOT_FOUND`; a missing `since` → `INVALID`. |
| E — Conflicting observations | `WorkflowEConflictingObservationTests` | An older conflicting observation → `out_of_order` with `['conflicting','out_of_order']` uncertainty and a `state_conflict` transition, while the newer box and value are kept; a replay across clock domains → `stale` with only `['stale']`. |
| F — Restart and recovery | `WorkflowFRestartRecoveryTests` | Save/restore round-trips state id, version, snapshots and transitions; a damaged payload restores with `skipped_records == 3`; a foreign payload is refused by name; an older schema is tolerated and named; retention applies on restore. |
| G — Evidence query | `WorkflowGEvidenceQueryTests` | Evidence rows resolve to observations; an unresolved subject → `NOT_FOUND` rather than unrelated rows; uncertainty/stale/`diff` reads. |
| H — Prediction boundary | `WorkflowHPredictionBoundaryTests` | Default engine → `model_unavailable`, provider `none`; `RuleProjectionProvider` → `predicted`, `rule_based=True`, `confidence=None`, exact measured velocity and projected centre; horizon clamping to 300 s with a limitation; unknown target and single-version → `insufficient_evidence`. |
| I — Resource fallback | `WorkflowIResourceFallbackTests` | `_ModelBackedProvider` + `_DenyingGate` → `resource_blocked`, the provider is never called, the limitation says state work is unaffected; work continues afterwards. |

Also covered: the phase's own posture (`OverviewTests`), every enum and helper
(`ContractTests`, `TimeUtilTests`), ingestion and the Phase 21 conversion
(`IngestTests`, `IngestPerceptionTests`), estimation and identity
(`EstimationTests`), relationships (`RelationshipTests`), change detection
(`ChangeDetectionTests`), all 11 query kinds (`QuerySurfaceTests`), the 7 rules
plus **generated tests walking each rule's own `examples`**
(`ReasoningRuleGeneratedTests`), the capability table (`CapabilityTests`), the
engine's surfaces (`EngineSurfaceTests`), the 9 events
(`EventAnnouncementTests`), telemetry (`TelemetryTests`), the store (`StoreTests`),
both bridges (`AgenticBridgeTests`, `PerceptionBridgeTests`), `_observation_batch`
(`ObservationBatchTests`), and the workflows over real HTTP through
`create_app()` (`WorldApiTests`).

## 24. Performance Results

Measured on this machine in one process — **Windows 11 (10.0.26300), Python
3.13.0**. Every figure below was measured; nothing here is estimated. Read paths
use a world with **200 entities and 32 retained snapshots**.

| Operation | Mean | Median | p95 | Max | n |
| --- | --- | --- | --- | --- | --- |
| `observe()` — 10 entities | 2.369 ms | 2.211 | 3.215 | 5.883 | 60 |
| `observe()` — 50 entities | 8.064 ms | 7.886 | 9.581 | 10.958 | 60 |
| `observe()` — 200 entities | 12.873 ms | 12.815 | 14.233 | 16.117 | 60 |
| `query(entities, limit=200)` | 0.308 ms | 0.244 | 0.606 | 0.970 | 500 |
| `query(entity)` | 0.009 ms | 0.008 | 0.011 | 0.068 | 500 |
| `query(state_at)` | 0.068 ms | 0.017 | 0.049 | 8.281 | 200 |
| `reason(question)` | 0.331 ms | 0.260 | 0.623 | 4.977 | 500 |
| `changes(limit=200)` | 0.245 ms | 0.206 | 0.484 | 0.669 | 200 |
| `state_summary()` | 0.235 ms | 0.197 | 0.446 | 0.627 | 500 |
| `predict()` — rule projection | 0.019 ms | 0.016 | 0.034 | 0.158 | 500 |
| `predict()` — no provider | 0.020 ms | 0.015 | 0.036 | 0.296 | 500 |
| `save()` — 200 entities × 32 snapshots | 118.389 ms | 110.046 | 163.354 | 170.914 | 20 |
| `restore()` — same payload | 243.233 ms | 235.122 | 326.919 | 329.850 | 20 |

Memory, by `tracemalloc` peak while ingesting 40 observations:

| Entities | Peak | Current | Serialized payload | Retained |
| --- | --- | --- | --- | --- |
| 10 | 814.0 KiB | 756.7 KiB | 1,329.4 KiB | 32 snapshots / 20 transitions |
| 200 | 4,126.5 KiB | 3,815.7 KiB | 6,043.3 KiB | 32 snapshots / 20 transitions |

The stored file for the 200-entity world after 40 observations is **10.2 MB** —
snapshots are full states, not deltas, so storage grows with
entities × retained snapshots. That is the phase's dominant cost and it is bounded
by `max_snapshots`/`max_transitions`/`retention_seconds` (§26).

## 25. Failures Discovered and Fixed

Nine real defects were found by writing the workflows, the source-conversion tests
and an independent audit that drove every requirement question through the public
surface, fixed in the source, and pinned by new tests:

1. **`estimation.py::_conditions` counted the wrong entity list.** It counted
   `previous.entities`, so the `entity_count` condition was always one version
   stale. It now takes the new `entities` sequence and counts that
   (`_conditions(previous, entities, observation, *, timestamp, evidence)`).
2. **`ingest.py::observation_from_perception` raised `UnboundLocalError`** for a
   bare `SceneRepresentation`, because `source_id` was only assigned in the
   `PerceptionResult` branch. `source_id = scene.source_id` is now set in the scene
   branch, and `status`/`reason` defaults are hoisted so a bare scene cannot leak
   them.
3. **A failed look was rejected as "reports nothing".** A `PerceptionResult` with
   `scene=None` now carries an `OBSERVED` `perception_status` condition fact, and
   every successful scene carries the same fact — so "the pipeline ran and saw
   nothing" is recorded instead of discarded.
4. **A failed look could linger as a stale success.** `EPHEMERAL_CONDITIONS =
   frozenset({"perception_status"})` was added to `estimation.py`: ephemeral
   conditions are dropped on carry-forward and ignored in `_changed_conditions`.
5. **`engine.py::_ordering` had the staleness test inverted** (`age < 0`), so a
   future observation was flagged stale and a genuinely old one was not. It is now
   `age > 0 and lag > stale_after_seconds` (age = current − observation).
6. **`queries.py::_evidence` treated an unresolved subject as "no filter"**,
   returning unrelated rows for a claim that does not exist. An unresolved label
   now becomes the subject anchor, and the unfiltered evidence list is used only
   when an entity actually resolved.
7. **`engine.py::observe` ignored the observation's own `correlation_id`.** It is
   now `correlation_id or observation.correlation_id`, so an observation that
   carried its correlation still groups with the request that produced it.
8. **A ceiling was reported as a fact about the caller's data that was not true.**
   `ingest._bounded` folded three different things into one counter — entities past
   `MAX_INGESTED_ENTITIES`, facts past `MAX_INGESTED_FACTS`, and genuinely
   unreadable rows — and `validate` rendered the total as "N unusable entity
   row(s) dropped". A caller who sent 300 **valid** rows was told 172 of them were
   "unusable" (and a dropped *fact* was reported as an unreadable *entity*). The
   counters are now separate (`dropped_entities` = unreadable,
   `truncated_entities` / `truncated_facts` = past a ceiling) and the reason names
   the ceiling that stopped them, which is the §32 rule — report the cap, never
   dress it up as something else.
9. **The state ceiling was not reported at all.** `_bounded` retires *expired*
   entities to make room and records `metadata["retired_entities"]`, which is zero
   whenever the overage is entirely live entities — so a state could hold 480 live
   entities while `status()["policy"]["max_entities"]` said 256, with nothing
   anywhere saying so. Live facts are still never destroyed to satisfy a policy
   number (that property is deliberate and kept), but the overage is now returned,
   stamped on the state as `metadata["entities_over_ceiling"]` and published as
   `status()["entities_over_ceiling"]`, so the declared ceiling and the count
   beside it can never contradict each other silently.

## 26. Known Limitations

Stated plainly, because the phase's value depends on them being true:

* **No learned predictive model ships.** `world.prediction` is
  `PROVIDER_DEPENDENT`; the only available projection is a labelled rule
  extrapolation, off by default, requiring two retained versions with a measured
  displacement (§16).
* **Snapshots are full states, not deltas.** A large world's history is the
  dominant storage and persistence cost (10.2 MB / ~118 ms save at 200 entities ×
  32 snapshots). Bounded by `max_snapshots` (32), `max_transitions` (500),
  `max_observation_refs` (128) and `retention_seconds` (86400), plus `prune()`.
* **Identity is geometry and source ids, not recognition.** Two identical objects
  that swap places cannot be told apart; the best this layer does is keep the
  identity provisional and record `ambiguous_identity`.
* **A partial observation cannot prove absence.** An entity that disappears from a
  partial look stays `present` — correct, but it means absence needs a source that
  reports completeness.
* **Reasoning is 7 deterministic rules.** A question that matches none is answered
  `unmatched` with the available shapes; this is bounded reasoning over recorded
  state, not natural-language understanding.
* **Attributes are declared, not inferred.** The state stores what an observation
  said; it does not infer a schema, a unit or a type.
* **History is bounded.** A question about a moment outside retention is
  `NOT_FOUND`; the state is genuinely gone, not merely de-ranked.
* **Clock semantics are per-domain.** Out-of-order and stale are decided within a
  clock domain; a replay from a different domain is labelled stale rather than
  reinterpreted.
* **No cross-world queries.** Isolation is by world id, deliberately — a fact from
  one environment cannot be joined into another.
* **The two read routes differ, on purpose.** `/world/state` is content-light
  (labels, statuses, boxes — no values), while `/world/query` returns entity
  records *including* attribute values with their basis and evidence. A caller with
  API access can therefore read what was observed; the guards are authentication,
  the world id, the fact that observation **bodies** are never stored (only
  references) and that scene text is opt-in.
* **Live entities can exceed `max_entities`.** The ceiling retires expired history
  first and never discards a live entity, so a long stream of genuinely new
  entities grows the current state past its policy number. The overage is reported
  (`status()["entities_over_ceiling"]`), not hidden — but it is growth, not a hard
  stop. `MAX_WORLD_ENTITIES` (4096) bounds the *configured* setting, not the
  runtime state.
* **No UI panel consumes the five routes yet** — they are registered `no-render`.

## 27. Future Phases Deliberately Not Implemented

`world.overview()["deferred"]` names them, and `DEFERRED_PHASES` is the same tuple
in code:

* **Phase 23 — interactive learning and exploration environments.** No learning, no
  reward signal, no environment interaction.
* **Phase 24 — planning, reasoning and action policy.** No planner, no action
  policy, no goal representation beyond Phase 20's existing `AgentState` adapter.
* **Phase 25 — embodied and game agents.** No game agent, no embodiment.
* **Phase 26 — generalization, ARC and intelligence evaluation.** No ARC handling,
  no generalization benchmark.

Also deliberately absent: interactive learning, autonomous exploration, a new
planner, model training, model downloads and any autonomous action execution.
Phase 22 ends exactly where it was scoped: **observations → world state → temporal
memory → state transitions → state queries → bounded state reasoning → optional,
honestly reported prediction.**

## 28. Regression Results

**The full suite was run on the final, fixed tree.** Every test file is covered by
14 disjoint slices, so each run stays inside the tool's time budget and no two
slices share a test — a failure cannot hide behind another slice's pass.

| Run | Result |
| --- | --- |
| Full suite in 14 disjoint slices — all 110 files, 3,681 collected tests (`PYTHONPATH=src`, `NOVACONTROL_DISABLE_OLLAMA=1`) | **3,669 passed, 0 failed, 12 skipped, 2,146 subtests passed — 14/14 slices exit 0** |

3,669 passed + 12 skipped = 3,681 collected, so nothing is unaccounted for.
`test_world_model.py` contributes 254 passed + 28 subtests, `test_perception.py` 90.

One caution worth recording: slice 05 first ran alongside eight other slices and
reported **five failures**, all `409 "the resource estimate is unsafe"` out of
`test_preference.py` — those tests assert against the *host machine*, and nine
concurrent interpreters left too little free RAM for the resource governor to call
the load safe. Re-run on its own it is **280 passed, exit 0** (23 s). That is the
host-dependence the repository already documents, not a defect in this phase; the
lesson is that a parallel sweep re-runs resource-sensitive slices alone.

**The four CI checks**, as CI itself runs them:

| CI check | Status here |
| --- | --- |
| `test` (py3.13) — `python -m pytest tests/ -q` | **Verified** — the 14-slice full-suite run above; this machine's interpreter is 3.13.0 |
| `test` (py3.12) | **Not locally verifiable** — no 3.12 interpreter on this machine (`py -0` lists only 3.13 and 3.10; no `/c/Python312`). The leg runs the identical command as the 3.13 leg, and the sources are 3.12-compatible by syntax (`requires-python = ">=3.12"`; the only construct an older interpreter rejects is a PEP 701 f-string backslash, which 3.12 introduced). Runtime differences are *not* covered by that argument, and are reported as unverified. |
| `docs-sync` — `generate_api_reference.py --check` | **Verified** — `docs/API.md: in sync` (exit 0) |
| `typecheck` — `mypy src` and `mypy src --platform win32` | **Verified** — `Success: no issues found in 365 source files` (both, exit 0) |

For continuity, the three-group sweep this audit began with — it ran on the tree as
it stood *before* the §25.8–9 fixes, which is why the slice run above supersedes it:

| Group | Result | Time |
| --- | --- | --- |
| 1 (37 files, incl. `test_world_model.py`) | **1399 passed, 5 skipped, 761 subtests passed** | 5:05:31 |
| 2 (37 files) | **1124 passed, 556 subtests passed** | 0:55:10 |
| 3 (36 files, incl. `test_perception.py`) | **1143 passed, 7 skipped, 829 subtests passed** | 0:30:25 |
| **Total** | **3666 passed, 12 skipped, 2146 subtests passed — all three groups exit 0** | |

**Post-fix targeted runs (the suites that touch what the two fixes changed):**

| Check | Result |
| --- | --- |
| `tests/test_world_model.py` (complete) | **254 passed, 28 subtests passed** (2:51) — three of the tests are the new pins for §25.8–9, and two existing expectations were corrected from the conflation to the separated counters |
| `test_telemetry.py test_web_platform.py test_api.py test_route_shape_contract.py test_api_reference_sync.py test_events.py test_perception.py` | **207 passed, 563 subtests passed** (5:28) |
| `tests/test_application.py` | **32 passed, 12 subtests passed** |
| `ruff check src/novacontrol/world tests/test_world_model.py` | **All checks passed** (exit 0) |
| `mypy src` and `mypy src --platform win32` | **Success: no issues found in 365 source files** (both, exit 0) |
| `scripts/generate_api_reference.py --check` | **`docs/API.md: in sync`** (exit 0) |

Targeted suites from earlier verification, also green on that tree:
`test_world_model.py` (251 passed + 28 subtests — the count *before* the three new
pins of §25.8–9, now 254), `test_api.py
test_route_shape_contract.py test_api_reference_sync.py test_events.py
test_config.py` (55 passed + 305 subtests), `test_telemetry.py test_perception.py`
(113 passed), `test_web_platform.py` (45 passed + 258 subtests).

Static checks:

* `python -m mypy src` → **Success: no issues found in 365 source files**.
* `python -m mypy src --platform win32` → **Success: no issues found in 365 source files**.
* `ruff check src/novacontrol/world tests/test_world_model.py` → **All checks passed**.
  (The repository does not use `ruff format` repo-wide — 334 files would be
  reformatted at HEAD — so formatting was hand-matched instead.)
* `scripts/generate_api_reference.py --check` → **`docs/API.md: in sync`**.
* Route/consumer contract: **215 routes == 215 consumers**, missing `[]`, extra `[]`.

## 29. Documentation Updated

* `docs/PHASE22_REPORT.md` — this report (30 sections).
* `README.md` — a Phase 22 row in the Current Status table beside Phase 21, stating
  what it does, the 12 classified capability rows, the 5 routes, the 9 events, the
  `world:` config section, the measured numbers, and the honest prediction posture.
* `docs/STATUS.md` — `Current phase:` moved to Phase 22 and a
  "Completed World Model, Memory & State Reasoning (Phase 22)" section added after
  the Phase 21 section.
* `docs/DEVELOPMENT_LOG.md` — a Phase 22 entry recording what was built, the nine
  defects found and fixed, and the verification numbers.
* `AGENTS.md` — Phase 22 operational learnings for future sessions: the package's
  one-door/one-reconciler/one-read-layer structure, the honesty rules as enforced
  behavior, the `EPHEMERAL_CONDITIONS` trap, the inverted-staleness trap, the
  unresolved-subject trap, the store and world-id rules, the 215-route contract,
  the measured cost profile, and the audit lesson itself — driving the requirement
  *questions* against the real engine and the real routes found both ceiling
  defects that 251 green tests could not, and a probe must isolate `data_dir`
  (`create_app()` inside a `TestClient` writes the real `data/` on shutdown).
* `docs/API.md` — regenerated to 215 routes.

## 30. Final Verdict

**PASS.**

Every capability the phase asked for is implemented and exercised end to end, and
each is classified honestly:

| Capability | Classification | Basis |
| --- | --- | --- |
| `world.observe` | **IMPLEMENTED** | Normalized ingestion with duplicate, ordering and rejection semantics; workflows A/B/C/E. |
| `world.perception_ingest` | **IMPLEMENTED** | Real Phase 21 `PerceptionResult`/`SceneRepresentation` conversion, including the failed-look fact. |
| `world.entity_tracking` | **IMPLEMENTED** | Hint → IoU/distance → provisional identity, four-state life cycle, measured movement. |
| `world.relationships` | **IMPLEMENTED** | Durable relations with status and evidence, geometry delegated to Phase 21. |
| `world.state_transitions` | **IMPLEMENTED** | 9 transition kinds, each with previous/current and evidence. |
| `world.temporal_memory` | **IMPLEMENTED** | Bounded snapshots, transitions and observation references, with retention and `prune()`. |
| `world.persistence` | **IMPLEMENTED** | Shared `JsonStateStore`, per-world key, honest save/restore reports; live availability requires a wired store. |
| `world.change_detection` | **IMPLEMENTED** | Threshold-based differences, read from the transition log so it cannot disagree. |
| `world.state_query` | **IMPLEMENTED** | 11 query kinds, four statuses, real bounds, never substituted. |
| `world.state_reasoning` | **IMPLEMENTED** | 7 named deterministic rules, each with limitations; no hidden reasoning. |
| `world.rule_projection` | **IMPLEMENTED** | Constant-velocity projection, labelled `rule_based`, `confidence=None`, opt-in. |
| `world.prediction` | **PROVIDER_DEPENDENT** | No model ships; default `model_unavailable` with the reason stated; any provider can be wired. |
| — | **MOCK: none** | Nothing in this phase is a placeholder; every implemented row is exercised against the real engine. |
| — | **UNAVAILABLE: none** | No world capability is unavailable on this machine — the layer is pure computation plus the shared store. |
| Phases 23–26 | **DEFERRED** | Named in `overview()["deferred"]` and `DEFERRED_PHASES`; not started. |

The nine defects in §25 were real failures surfaced by real workflows and by an
independent audit of every requirement question, and each was fixed in the source —
not worked around in tests. Two existing expectations were corrected because they
had pinned a defect (a ceiling reported as "unusable" rows); the replacements assert
both counters and the reason text, so they are stronger than what they replaced.

On the final, fixed tree the **whole suite is green — 3,669 passed, 0 failed,
12 skipped, 2,146 subtests passed, 14/14 disjoint slices exit 0** (3,669 + 12 =
3,681 collected); the complete world suite is **254 passed / 28 subtests**, the
post-fix targeted suites are **207 passed / 563 subtests** and the application suite
**32 passed / 12 subtests**, both mypy views are clean over **365 files**, ruff is
clean over the new package and its tests, the API reference is in sync, and the
route/consumer contract holds at **215 = 215**. Of the four CI checks, three are
verified locally and the `test` (py3.12) leg could not be — no 3.12 interpreter is
installed on this machine; §28 says so plainly rather than implying otherwise.

What Phase 22 delivers is exactly what it scoped: **a reliable, bounded,
uncertainty-aware state-and-memory foundation**, with the prediction boundary honest
and empty. It is a foundation Phase 23 can build on rather than a claim about
intelligence NovaControl does not have.
