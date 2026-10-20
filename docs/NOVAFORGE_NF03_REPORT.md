# NovaForge NF-03 — Evidence Timeline & Replay

**Verdict: PASS WITH LIMITATIONS.**

NF-03 records what a session actually did as an ordered, bounded, persisted evidence timeline, links each
event to the frame/observation/telemetry/intervention it names, and lets a person inspect that evidence
through a read-only replay surface. Nothing in the replay path can start, resume, stop, drive or send
input to a session; there is no action or verification stage in this phase, and the API, the panel and
this report say so instead of inventing one.

NF-04, NF-05 and NF-06 were **not started** (see §12).

> **UI correction, 2026-10-20.** The sidebar `Evidence NF-03` destination this phase added has been
> **removed**: it opened the same `novaforgePanel` as the `NovaForge` entry, so the phase lives only in its
> **Evidence Timeline** tab. The tab strip was also made to wrap, because with the sidebar entry gone an
> off-screen tab would have been the only route to the timeline. See
> [NOVAFORGE_VERIFICATION.md](NOVAFORGE_VERIFICATION.md) §8.3.

---

## 1. What was built

| Area | Delivered |
| --- | --- |
| Event model | `novacontrol/novaforge/timeline.py`: typed, validated events with a unique id, per-session `seq`, wall-clock label, monotonic elapsed, source, payload, bounded evidence references and schema version. An unknown type is refused, never filed under a guessed category. |
| Ordering | Sequence assigned as the first step of a synchronous append (no `await` between assignment and append, so two coroutines on the application's single event loop cannot observe the same number); ordering is `seq`, never the wall clock. `elapsed_ms` uses `time.monotonic()` from the session's origin. |
| Integrity | Duplicate `event_id` ingestion is idempotent; concurrent appends produce unique contiguous sequences; the log is bounded at 240 events per session with the dropped count reported. |
| Persistence | One bounded snapshot per session in the shared `JsonStateStore`, written through the manager's existing throttled persistence and verified by reading the snapshot back when the session closes. A store that drops a write is reported as `failed` (plus an error event), not hidden. |
| Evidence links | Bounded references (frame seq, observation index **and timestamp**, intervention index **and timestamp**, telemetry/session/event refs), resolved at read time against the session's own record. A missing frame becomes `available: false` with the retention reason; the event stays inspectable. |
| Replay | `timeline_payload`, `timeline_event`, `compare_sessions` are GET-only read paths; the replay payload carries explicit `read_only`/`can_*: false` flags. |
| Classification | Evidence-backed categories (`capture_failure`, `target_loss`, `timeout`, `guardian_cancellation`, `persistence_failure`, `unexpected_exception`, `unknown`), each with the event references that justify it and a `direct` vs inferred flag. No success is ever claimed. |
| Comparison | Two sessions side by side over 17 rows (state, end reason, duration/active/paused, frames, stale, failures, gaps, median/p95, target losses/changes, errors, interventions, actions, verification). A figure that was not measured is `None` with a reason; an action/verification row is `available: false` because NF-03 has no such stage. |
| API | `GET /novaforge/sessions/{id}/timeline` (limit/after_seq/type/category/source/severity filters), `GET …/timeline/{seq}`, `GET /novaforge/timeline/compare?left=&right=`. Registered in the handler, `ApiSurface`, `route_consumers`, `docs/API.md` and tests. |
| UI | A sixth Forge tab, **Evidence Timeline**, plus a now-enabled sidebar **Evidence (NF-03)** destination: session selector, category/source/severity filters, chronological list, event inspector with payload and evidence status, authenticated frame viewer, Prev/Next, Load more, failure classification card, two-session comparison card, read-only banner, the applicable NF-03 limitations, and explicit loading/empty/unavailable/auth-error/persistence-error states. |

---

## 2. Timeline event schema

`TimelineEvent` (`TIMELINE_VERSION = 1`, `EVENT_VERSION = 1`):

```json
{
  "version": 1,
  "event_id": "<session_id>-<seq:06d>",
  "session_id": "<32 hex>",
  "seq": 1,
  "type": "frame.captured",
  "category": "capture",
  "severity": "info",
  "source": "capture",
  "at": "2026-10-17T17:59:34.862238+00:00",
  "elapsed_ms": 1234.5,
  "payload": {"frame_seq": 1, "width": 1000, "height": 600},
  "evidence": [{"kind": "frame", "session_id": "…", "seq": 1}],
  "correlation_id": "",
  "derived": false
}
```

The vocabulary is one table, `EVENT_SPECS` (type → category, default severity): lifecycle
(`session.created/started/pausing/paused/resumed/stopping/stopped/failed/reconciled`), target
(`validated/lost/changed/unreadable`), capture (`frame.captured/frame.rejected/capture.failed`),
`observation.recorded`, `telemetry.snapshot`, `guardian.intervention`, `error.recorded`,
`input.cleanup`, `timeline.persistence_failed`. There is deliberately **no** `action.*` or
`verification.*` type: NF-03 has no action or verification stage, so none is emitted.

Payloads are bounded: strings at 300 characters, at most 20 items per dict/list, depth 2. Evidence
references are bounded at 8 per event. A persisted row that does not parse to a known type, a positive
`seq` and a valid session id is skipped (not repaired), so one bad row cannot take a whole timeline
down or be made to look recorded.

---

## 3. Ordering guarantees

* **Sequence is the order.** `TimelineLog.append` is synchronous and assigns `seq = next_seq` before
  returning, with no `await` in between, so on the application's single event loop two coroutines
  cannot observe the same number (pinned by a concurrent-append test). Every read sorts by `seq`; the
  wall-clock `at` is a label only.
* **Monotonic elapsed.** `elapsed_ms` is `(time.monotonic() - origin) * 1000`. An event with no
  recoverable monotonic origin (a session restored after a restart, or a derived event) reports
  `elapsed_ms: null` rather than a fabricated duration, and the payload's `limitations` says so.
* **Concurrency.** Appends from concurrent tasks produce unique, contiguous sequences. Duplicate
  ingestion of the same `event_id` is ignored (idempotent), which is what makes two subscribers or a
  retried write harmless.
* **Late events.** Nothing reorders history: an event appended late (for example a reconciliation
  fact after boot) gets the next sequence and is the newest event, because that is when it was
  recorded. A derived timeline is the exception and says so: its order follows the record's timestamps,
  which can tie, so every event is marked `derived: true`.
* **Clock changes.** A backward wall-clock jump changes only `at`; ordering and elapsed are unaffected.

`ORDERING_NOTE` is returned with every page and rendered in the panel.

---

## 4. Persistence, restart and failure behavior

* Each session's log is stored under the namespace `novaforge_timeline_<session_id>` in the shared
  `JsonStateStore`. The namespace is only constructed for a generated 32-hex session id; an arbitrary
  id cannot name a file.
* Writes use the manager's existing throttled path (2 s ticks, immediate lifecycle persistence), so the
  timeline does not add an unthrottled write to every frame.
* When a session closes (stop/cancel/emergency stop/failure), the manager **reads the snapshot back**
  and compares its highest stored `seq` with the newest event in memory. Success is recorded as
  `persistence.state: "verified"`; a mismatch records an error on the session, a
  `timeline.persistence_failed` event, `persistence.state: "failed"` with expected/stored sequences and
  a bounded final write. This exists because `JsonStateStore` deliberately does not raise when it drops
  a snapshot: a successful `write` call is not proof that the newest evidence is on disk.
* With no store available the timeline is `not_persisted` (memory only) and says why.
* On boot, stored logs are restored by `TimelineLog.from_dict`; a malformed event is skipped, and a
  snapshot written for a different session is not attached. A session that predates the log (or lost
  its namespace) gets a **derived** timeline reconstructed from its persisted record, explicitly
  labelled — never presented as a recording.
* Eviction of an old session (the existing `max_sessions_kept` bound) removes its log and overwrites the
  stored namespace with a `cleared` marker, so deleted evidence cannot be resurrected by a later read.
  Completed NF-01/NF-02 session records are unchanged by NF-03; their timelines are derived.

---

## 5. Evidence linking and retention

* References are **bounded identifiers, never paths or image payloads**. Frame refs carry the session
  and `seq`; observation and intervention refs carry the index **and the recorded timestamp**.
* Frame resolution goes through the same confined `frame_path` used by the frame route: the file must
  live under the NovaForge frame root and must exist. A pruned frame resolves `available: false` with a
  reason naming retention; the event remains readable and the panel keeps the inspector usable with no
  image.
* Observation resolution matches the timestamp first. This was a real defect found during the final
  verification: `SessionRecord.record_observation` keeps the newest 30 entries and numbers new ones from
  the current list length, so a long session can hold several observations with `index: 31`. Resolving
  by index alone returned the **oldest retained** match — a different observation than the event named.
  Refs now carry `at`; an index-only ref that matches more than one retained observation reports
  `ambiguous` instead of substituting one. Intervention refs had the same reindexing problem and use
  the same timestamp rule.
* Telemetry and session refs resolve to their existing read routes; event refs are evidence-in-timeline.
* Frames are never loaded into memory by retrieval: the page returns URLs, and the panel fetches one
  image with the API token when an event is selected.
* Provenance is preserved: each event keeps its own timestamp/source, and the UI distinguishes a
  recorded timeline from a derived one, plus a directly recorded classification from an inference.

---

## 6. Replay safety

Replay is a set of **read-only GET routes** and a client that only calls them. Specifically:

* `timeline.py` contains no call to `start`, `pause`, `resume`, `stop`, `cancel` or `emergency_stop`, no
  input release, and no subprocess (pinned by `ReplaySafetyTests`).
* The three HTTP handlers in `api/app.py` call only `timeline_payload`, `timeline_event` and
  `compare_sessions`; a static test pins that no lifecycle method appears in their block.
* `POST` to any replay route is 405, and an HTTP test asserts a stopped session's state and the
  active-session slot are unchanged after attempting it.
* The panel's replay module has no POST and no `/start|/pause|/resume|/stop|/cancel|/emergency-stop`
  string (pinned); every request is `requestJson(path)` and frame images are fetched with
  `fetch(frame.url, { headers: authHeaders() })` — an `<img src>` cannot carry the token.
* The payload itself advertises `read_only: true` and `can_start/can_resume/can_stop/
  can_execute_action/can_send_input: false`; the panel renders the banner from those flags rather than
  assuming safety from the absence of a button. If a flag ever came back true, the panel shows a
  "Replay boundary NOT confirmed" warning.

---

## 7. Comparison and failure classification

* Every classification entry carries `direct` (stated by the record) or an inference, a detail and the
  event references that justify it. `unknown` is used for an error recorded at a location NF-03 does
  not classify; `outcome: no_failure_recorded` explicitly says that is not success.
* `completion.verified` is always false in NF-03 (no objective verifier exists), and `claimed` reports
  whether the record somehow holds `COMPLETED`. Limits and stops are never treated as success.
* Comparison rows are typed: a missing figure is `None` plus a reason, `delta` is `None` unless both
  sides are numeric, and action/verification rows are `available: false` with the reason. One measured
  p95 against one unmeasured side is reported as available with the unmeasured side `None` and the
  p95's own reason preserved (this was corrected during verification; the row previously dropped its
  explanation when the other side existed).

---

## 8. API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/novaforge/sessions/{session_id}/timeline` | `limit` (1–500, default 100), `after_seq`, `types`, `categories`, `sources`, `severities` (comma-separated). Unknown filter values are 422; an unknown session is 404. Returns events, page info (`total`, `next_after_seq`, `has_more`, `first_seq`, `last_seq`), retention, persistence policy, ordering note, classification and replay flags. |
| GET | `/novaforge/sessions/{session_id}/timeline/{seq}` | One event with its evidence resolved. Unknown seq/session is 404. |
| GET | `/novaforge/timeline/compare?left=&right=` | Two persisted sessions compared; unknown id is 404. |

All three are registered as authenticated in `ApiSurface`, declared in `route_consumers.py`, generated
into `docs/API.md` and pinned by tests. Classification is computed from the **whole** timeline, so a
filtered page can never change the diagnosis.

---

## 9. UI

`index.html` gains a sixth Forge tab (`#forgeTimelineView`) and the sidebar **Evidence** entry is now a
working destination (Benchmarks/Evaluation/Forge Coach remain disabled). `novaforge.js` gains a replay
module with these behaviours:

* session selector populated from `/novaforge/sessions` (keeps the user's choice, falls back to the
  live session, then the newest), a second selector for comparison;
* category/source/severity filters whose options are learned from returned events (no duplicated
  vocabulary table in the client) and whose changes reload from sequence 0;
* the list renders `#seq`, wall-clock time, `+elapsed`, source, type, a one-line summary, a `derived`
  badge and evidence status; severity colours the row edge;
* the inspector renders the event's fields, bounded payload, every evidence reference with
  available/unavailable/ambiguous state and reason, and the frame viewer when a frame ref resolves;
* Prev/Next move through the loaded page (position `n / m`), Load more appends the next page;
* a stale session/filter response is discarded by a request token, and a stale frame fetch by a second
  token (both pinned behaviourally);
* loading, empty ("no session recorded"), unavailable, auth-error (adds "set the API token…") and
  persistence-failed states are distinct; a `failed`/`not_persisted` policy is shown as such, and a
  derived timeline explains why its events are reconstructed;
* classification and comparison cards render from the backend payloads only.

---

## 10. Files changed

New:

* `src/novacontrol/novaforge/timeline.py` — event model, log, query/validation, pagination, filtering,
  evidence reference builders, derived timelines, classification, comparison. The second verification
  pass removed the unused `to_summary` helper and the no-op `summary` parameter, and bounded payload
  depth and unrepresentable numbers.
* `tests/test_novaforge_timeline.py` — 42 tests (model, ordering, persistence, evidence, replay
  safety, schema compatibility, manager integration).
* `scripts/benchmark_novaforge_timeline.py` — measures append/persistence/retrieval and a real recorded
  session.
* `docs/NOVAFORGE_NF03_REPORT.md` — this report.

Modified:

* `src/novacontrol/novaforge/manager.py` — timeline creation/restore, one recording door per fact,
  throttled persistence, close-time read-back verification, eviction cleanup, evidence resolution,
  `timeline_payload` / `timeline_event` / `compare_sessions`.
* `src/novacontrol/novaforge/__init__.py` — package exports and docstring.
* `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py` — the three routes in all
  registration places.
* `src/novacontrol/web/static/index.html`, `nc-os.css`, `js/novaforge.js` — replay tab, sidebar
  destination, styles, renderers.
* `tests/test_novaforge_api.py` — 8 HTTP tests (`NovaForgeTimelineApiTests`) for timeline/compare/replay
  safety/path-traversal/registration.
* `tests/test_novaforge_ui.py` — 15 new timeline UI tests (pure logic + DOM-rendered), one roadmap-card
  honesty test, and the planned-destination count updated (NF-03 is implemented).
* `docs/API.md` — regenerated.
* `docs/STATUS.md`, `README.md` — NF-03 status and measurements; the second pass also added the
  missing `DEVELOPMENT_LOG.md` §66 entry (the phase's README row and log entry did not exist before it,
  so the earlier version of this section over-claimed them).
* `nc-os.css` — also **deleted** a redundant Forge-local `prefers-reduced-motion` block; it shadowed
  the app-wide block for `test_web_platform.py`'s extractor, and the app-wide `*` rule already disables
  the same motion.

---

## 11. Verification

Measured on this machine (Windows, Python 3.13) on 2026-10-17. A second verification pass re-ran the
phase and integration files **after** the fixes it made; results marked *(latest)* are from that pass,
the rest are from the first pass and are kept so nothing is presented as fresher than it is.

| Check | Result |
| --- | --- |
| `pytest tests/test_novaforge_timeline.py tests/test_novaforge_ui.py -q` | **83 passed in 4.97 s** (exit 0) *(latest)* |
| `pytest tests/test_novaforge.py tests/test_novaforge_timeline.py tests/test_novaforge_ui.py tests/test_novaforge_api.py -q` | **187 passed in 408.97 s** (exit 0) *(latest)* |
| `pytest tests/test_route_shape_contract.py tests/test_web_platform.py -q` | **63 passed, 416 subtests in 264.86 s** (exit 0) *(latest)* |
| `pytest tests/test_web_api.py -q -k "static or novaforge or marker or cache or index or asset or panel or ui"` | **13 passed, 55 subtests in 174.55 s** (exit 0) *(latest)* — the subset a static-asset edit could affect |
| `pytest tests/test_novaforge_timeline.py -q` (first pass) | **37 passed in 3.96 s** (exit 0) |
| `pytest tests/test_novaforge.py tests/test_novaforge_timeline.py tests/test_novaforge_ui.py -q` (first pass) | **149 passed in 16.45 s** (exit 0) |
| `pytest tests/test_novaforge_api.py -q` (first pass) | **31 passed in 393.94 s** (exit 0); the timeline class alone was re-run after the final manager edit — **8 passed in 113.80 s** (exit 0) |
| `pytest tests/test_web_platform.py -q` (first pass) | **45 passed, 326 subtests in 1.10 s** (exit 0) |
| `pytest tests/test_web_api.py -q` (first pass) | **75 passed, 72 subtests in 957.49 s** (exit 0) |
| `pytest tests/test_route_shape_contract.py -q` (first pass) | **17 passed, 90 subtests** (exit 0) |
| `mypy src` / `mypy src --platform win32` | **clean over 420 files**, both views (exit 0) *(latest)* |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (exit 0) *(latest)* |
| `node --check` on every `static/js/*.js` + `static/app.js` | all pass *(latest)* |
| Live panel check | real static files + real `NovaForgeManager` behind a deterministic desktop double; see below |

The full repository suite was **not** run. This is the NF-03-relevant set plus the two API/route
contract files and both mypy views CI checks. The second pass ran a targeted subset of
`test_web_api.py` (13 passed / 55 subtests) rather than that file's full 957 s run, because only the
static asset surface changed.

### Live panel verification

A preview server (`127.0.0.1:8011`, registered in the Preview tab) served the real application with the
shipped static files; only NovaForge's desktop seam was replaced by the deterministic `FakeDesktop`
double (the same one the tests use), because a real capture would record the user's screen. Actions
taken and observed:

1. Started a session through the real `/novaforge/sessions` route, recorded 92 events, stopped it
   (`STOPPED`, cleanup ok); the API returned `source: recorded`, `persistence: verified`,
   `classification.outcome: no_failure_recorded`.
2. Opened the sidebar **Evidence** destination: it activated `#forgeTimelineView` with 92 rows, the
   read-only banner, meta (`recorded event log`, `verified — the snapshot read back…`,
   `92 retained · cap 240`) and the classification card (`no failure recorded`, objective `NOT
   verified`).
3. Selected a `frame.captured` event: the detail showed its payload and evidence, the frame loaded as
   an authenticated blob at its real size (1000 × 600) with the note `Frame 83 · captured …`, and an
   older frame reported `2 evidence unavailable` with the retention reason — no substitute image.
4. Prev/Next moved `87 / 92 → 88 / 92 → 87 / 92`; the capture filter reduced the list to 83 events;
   a second session was recorded and the comparison card rendered 17 rows with real measured values
   (`Frames captured 83 / 70 / −13`, duration delta `−2.02`).
5. A screenshot confirmed the sidebar entry, toolbar, filters and timeline rows render as intended.
   One redundant `prefers-reduced-motion` block was removed during this pass (see §10).
6. **Second pass, after the fixes:** the preview server was restarted (so the changed manager code was
   live), a fresh session was recorded (74 events, `persistence: verified`) and the Evidence tab was
   reloaded. The meta now reads `Events 74 of 74 event(s) shown` and lists only the applicable
   limitations (the derived-order caveat is gone for a recorded timeline); the roadmap card reads
   `Phase Roadmap` with NF-02/NF-03 implemented and NF-04–NF-06 planned; the browser-side
   `NovaForgeLogic.timelinePageLabel({count: 40, total: 240}, 140)` returned `140 of 240 event(s)`.

No real game was launched and no gameplay input was sent at any point.

### Performance (measured, `scripts/benchmark_novaforge_timeline.py`)

Two runs of the same script on the same machine; the spread between them is machine load, not code.
Numbers are whatever was measured, not an average.

| Measurement | First pass | Second pass |
| --- | --- | --- |
| Append (240 events) | 7.389 µs/event (1.8 ms) | **10.285 µs/event** (2.5 ms) |
| Snapshot write (1 / 60 / 120 / 240) | 0.993 / 1.538 / 2.020 / 2.844 ms (max 3.218) | 1.315 / 1.646 / 2.951 / **4.078 ms** (max 5.294) |
| Snapshot size | 935 B / 42,609 B / 85,135 B / 170,575 B (~710 B per event) | same |
| Read + restore (1 / 60 / 120 / 240) | 1.615 / 15.338 / 16.245 / 18.818 ms | 1.571 / 16.646 / 20.962 / **20.869 ms** |
| Pure retrieval (240 events) | classify 0.029, page of 100 0.014, filter 0.016 ms | classify 0.029, page 0.014, filter **0.017 ms** |
| Real recorded session (20 frames, 29 events) | 1.299 s wall; page 5.389 ms (31,339 B); single event 0.011 ms; compare 0.259 ms; restart 5.696 ms | 1.342 s wall; page **7.516 ms** (31,517 B); filtered 7.342 ms; single event 0.015 ms; compare 0.205 ms; restart **7.714 ms**; persistence verified |

The retrieval cost is dominated by resolving evidence and rendering the payload, not by filtering or
ordering. A full 240-event session stores ~170 KB per session; with the existing 40-session cap that is
bounded, and NF-03 did not raise frame retention or add a storage dependency.

### Defects found and fixed during verification

1. **Observation evidence could resolve to the wrong observation.** After the record's 30-entry
   observation list fills, several observations share an index; index-only resolution returned the
   oldest retained match. Refs now carry the recording timestamp and an ambiguous index-only ref is
   reported unavailable. Same fix applied to intervention refs (their bounded list reindexes).
   Regression tests: `test_an_observation_reference_resolves_to_the_observation_it_named`,
   `test_an_intervention_reference_survives_the_bounded_list_reindexing`.
2. **A comparison row lost its reason when only one side was unmeasured** (one p95 measured, the other
   withheld). The row now keeps the p95 reason while leaving the unmeasured side `None`.
3. **A Forge-local reduced-motion block shadowed the app-wide one** for the web-platform CSS pin. It was
   redundant (the `*` rule already covers it) and has been deleted.
4. The planned-destination pin asserted four disabled entries; NF-03's implementation makes it three,
   and the test now also pins that no "Planned for NF-03" title remains.

Second verification pass (same day, after a fresh review of the phase requirements):

5. **A timeline load could raise out of the manager's constructor.** JSON parses an arbitrarily long
   integer exactly and `float()` refuses one that does not fit, and `_bounded` bounded a list's width
   but not its depth — so a corrupted or hand-edited snapshot could raise `OverflowError` or
   `RecursionError` while `NovaForgeManager.__post_init__` restored logs, taking boot down. `_finite`
   now returns `None` for an unrepresentable number and both container branches stop at depth 2; an
   otherwise well-formed event is kept, with only the unusable value replaced. Regression:
   `test_an_unrepresentable_number_or_deep_nesting_cannot_crash_a_load`.
6. **An ambiguous intervention reference reported the wrong reason.** When an `at`-bearing reference
   matched more than one recorded intervention, the resolver fell through to "has no timestamp" — a
   false statement about a reference that carried one. It now reports the ambiguity and the count, and
   still refuses to substitute an entry. Regression:
   `test_an_ambiguous_intervention_reference_is_reported_not_substituted`.
7. **A failed shutdown stop path could lose the session's final events.** `shutdown()` suppressed an
   exception from `stop()` and returned; the dirty timeline was never flushed. It now flushes the dirty
   logs in a `finally`, so a failed cleanup is not how the end of the evidence disappears. Regression:
   `test_shutdown_flushes_evidence_when_the_stop_path_fails`.
8. **The Forge roadmap card contradicted the implementation**: it still listed NF-03 as planned,
   described it as "per-action replay with visual proof" (a stage NF-03 does not have) and said "NF-03
   through NF-06 are not implemented". The card now marks NF-02/NF-03 implemented and says NF-04–NF-06
   are not. Regression: `test_the_roadmap_card_does_not_claim_nf03_is_unimplemented`.
9. **A loaded timeline's event count showed the newest page's size**, so Load more read "40 of 240"
   while 140 events were displayed. `timelinePageLabel(page, loaded)` now reports what the panel
   actually holds; pinned behaviorally in the logic harness
   (`test_the_loaded_count_is_reported_after_loading_more`).
10. **A recorded timeline listed the derived-order limitation it does not need**, and the panel did not
    render the limitations at all. `timeline_payload` now includes only the limitations that apply (the
    derived caveat only for derived timelines) and `renderTimelineMeta` renders them, so the panel says
    which pipeline stages the phase does not have. Pinned by
    `test_a_recorded_timeline_lists_only_the_limitations_that_apply` and
    `test_the_panel_states_which_pipeline_stages_were_unavailable`.
11. A corrupt frame file (present but not a PNG) is reported by its own reference and never swapped for
    another frame; the panel already showed the load failure. Pinned by
    `test_a_corrupt_frame_is_reported_by_its_own_reference`.
12. Dead code removed: the unused `TimelineEvent.to_summary` helper and the no-op `summary` parameter
    of `_resolve_event`.

---

## 12. Scope confirmation

NF-04 (Benchmark Lab), NF-05 (Controlled Game Evaluation) and NF-06 (Forge Coach) were **not started**:
no benchmark lab UI or routes, no evaluation workflow, no coach, no autonomous gameplay, no action
execution, no historical action re-execution, no game launching, no online multiplayer automation, no
anti-cheat bypass, no second brain/session manager/event bus. NF-03 adds one module to the existing
`novaforge` subsystem, three GET routes, and one panel tab.

---

## 13. Known limitations and unverified items

* **No action or verification stages exist**, so the timeline contains none and comparison reports both
  as `available: false`; the UI says this rather than showing empty action counts.
* **Derived timelines** (sessions recorded before NF-03) order by wall-clock timestamps that can tie and
  carry `elapsed_ms: null`; they are labelled `derived`.
* **Bounded retention**: after 240 events in one session the oldest events are dropped (reported as
  `retention.dropped`); the panel surfaces the count.
* **Frame retention** means old events can outlive their images; the reference explains it, but the
  image is gone by design (no retention increase was made).
* **Observation indexes are still non-unique in the NF-01 record**; NF-03 compensates with timestamps,
  but an index-only legacy ref cannot be resolved. A future NF-01 change to globally unique observation
  ids would remove the ambiguity at the source.
* **Derived timelines omit evidence-bearing observations as their own rows.** A derived session's
  `observation.recorded` events are emitted only for observations recorded WITHOUT visual evidence;
  an observation that has a frame is represented by its `frame.captured` row and that row's frame
  reference. The observation-level summary/latency for those entries is on the NF-01 record, not in the
  reconstructed timeline.
* **Evidence resolution probes frame existence (`Path.is_file`) while building a page.** It is bounded
  by the page size (≤ 500 events; ~7.5 ms for a 100-event page in the latest run) and matches the
  existing NovaForge read routes, but it is synchronous work inside an async handler and was not moved
  to a worker thread in this phase.
* **The UI was verified in a real browser against a deterministic capture double**, not against a live
  game or a real desktop window; no real gameplay was performed.
* **The full repository suite was not run** (the suite is ~2,254 tests and exceeds one tool window);
  only the files listed in §11 ran. The live preview ran on port 8011 because 8001/8002 are occupied by
  older session servers on this host; those were not stopped.
* **Comparison is observational**: it reports measured differences, not a verdict about which session
  was “better”, and there is no statistical claim.
