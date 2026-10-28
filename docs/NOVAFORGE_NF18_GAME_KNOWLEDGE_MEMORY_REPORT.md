# NovaForge NF-18 — Game-Specific Knowledge and Memory

**Phase:** NF-18
**Scope:** a typed, evidence-first game-knowledge model; durable game-scoped
storage over the existing memory subsystem; explicit verification,
contradiction and freshness rules; retrieval API and a panel card; focused tests
and an offline fixture evaluation. **No gameplay action is planned or sent.**
**Verdict:** **PASS WITH LIMITATIONS** (see §13)
**Live game knowledge acquisition:** `NOT_TESTED` (see §11)

---

## 1. Initial repository state

- Branch `main`, NF-17's objective-understanding phase present and verified
  (`docs/NOVAFORGE_NF17_OBJECTIVE_PROGRESS_REPORT.md`).
- NovaForge already had: NF-15 unified observation, NF-16 game-state reading,
  NF-17 objective interpretation, the shared `EventBus`, the `JsonStateStore`
  snapshots and the memory subsystem (`MemoryManager`, `SqliteMemoryStore`).
- The memory subsystem stored namespaced `namespace/key/value` rows but had no
  typed game knowledge, no evidence model, no verification transitions, no
  contradiction record and no game-scoped retrieval surface.
- The NovaForge panel had Game State and Objective & Progress cards; there was
  no knowledge card, no `/novaforge/knowledge*` route and no offline knowledge
  evaluation.
- Pre-existing working-tree changes from earlier phases were left untouched. No
  commit, push, reset, stash or broad refactor was performed.

## 2. NF-16 and NF-17 dependency status

| Dependency | What NF-18 needs | Status |
|---|---|---|
| NF-16 game state | `GameState` with a game identity, session/observation ids, per-field `RECOGNIZED`/`ABSENT`/`AMBIGUOUS`/`UNREADABLE`/`UNSUPPORTED` status, profile version and resolution | **Available and verified.** `tests/test_novaforge_state.py` (57 tests) passes; `state_candidates` reads only `RECOGNIZED` fields. |
| NF-17 objectives | `ObjectiveState` with game, session, description, category, explicit/inferred, status, reasons and outcome evidence; outcomes and unestablished objectives | **Available and verified.** `tests/test_novaforge_objectives.py` (61 tests) passes; outcome/question/pattern candidates come from this interpretation. |

Both are consumed through **one path**: `NovaForgeManager._consume_state`.
NF-18 never captures a frame, never re-reads the observer and never asks for a
second perception pass.

## 3. Gap analysis (before implementation)

| Requirement | Pre-NF-18 state | Gap closed |
|---|---|---|
| Typed knowledge categories | None | `KnowledgeCategory` with 9 declared categories |
| Verification status | None | `KnowledgeStatus` + a transition table that raises on illegal moves |
| Evidence traceability | None | `KnowledgeEvidence` with source, method, session/observation/objective ids, fingerprint and effect flag |
| Persistence | Namespaced memory rows existed | One namespace per game over the **same** SQLite memory store; no new DB |
| Contradiction handling | None | A differing claim is a second record; the earlier one becomes `CONTRADICTED` with a reason |
| Freshness | None | Version-bound records stale on a **known** version change; age only reports due revalidation |
| Session memory separation | None | `SessionMemory` is bounded, in-memory and has no persistence path |
| Retrieval | None | Game-scoped `records`, `verified_controls`, `unresolved_questions`, `summary` |
| Events | Bus existed | `novaforge.knowledge.updated` on every status change |
| API | None | 4 routes, `ApiSurface`, consumer declarations, regenerated `docs/API.md` |
| UI | None | Read-only Game Knowledge card in the NovaForge panel |
| Tests / evaluation | None | 64 focused tests and a 51-check offline evaluation |

## 4. Files inspected and modified

**Inspected (not all modified):** NF-12/NF-14/NF-15/NF-16/NF-17 reports;
`memory/{manager,models,stores}.py`; `novaforge/{manager,state,state_service,
objective,objective_service}.py`; `api/{app,models,route_consumers}.py`;
`web/static/index.html`, `js/novaforge.js`, `js/state.js`;
`persistence/json_store.py`; `core/events.py`; existing NovaForge tests.

**Created**

- `src/novacontrol/novaforge/knowledge.py` — model, evidence, transitions,
  candidate builders, session memory, API payload validation.
- `src/novacontrol/novaforge/knowledge_service.py` — persistence, verification,
  contradiction, freshness, retrieval, events, bounds, status.
- `tests/test_novaforge_knowledge.py` — 64 tests (model, service, integration,
  API, Node-driven panel logic, panel static checks).
- `scripts/novaforge_nf18_knowledge_eval.py` — deterministic offline evaluation.
- `docs/NOVAFORGE_NF18_GAME_KNOWLEDGE_MEMORY_REPORT.md` — this report.

**Modified**

- `src/novacontrol/novaforge/manager.py` — the memory seam, knowledge service,
  `_consume_state` ingestion, session gate, explicit-record and revalidation
  methods, knowledge status/records, shutdown.
- `src/novacontrol/application.py` — passes the application's `MemoryManager`
  into NovaForge (one persistence path).
- `src/novacontrol/api/app.py` — four `/novaforge/knowledge*` routes.
- `src/novacontrol/api/models.py` — `ApiSurface` rows.
- `src/novacontrol/api/route_consumers.py` — consumer declarations.
- `src/novacontrol/web/static/index.html` — the Game Knowledge card.
- `src/novacontrol/web/static/js/novaforge.js` — card state, pure row/fact/hint
  functions, scoped loader, refresh after a state reading.
- `docs/API.md` — regenerated (346 routes; `--check` in sync).

**Evaluation artifact (gitignored runtime data):**
`data/novaforge_nf18_knowledge_eval.json`.

## 5. Knowledge models and persistence architecture

### 5.1 Record

`KnowledgeRecord` carries: `knowledge_id`, `game`, `game_version`, `category`,
`key`, structured `content`, `status`, bounded `evidence`, `provenance`,
`revalidation` policy, `created_at`, `updated_at`, `last_verified_at`, `reason`,
`revision`, `contradicts`, `content_digest` and a bounded `history` of status
changes (at/from/to/reason, max 12).

Categories: `control`, `hud`, `mechanic`, `objective_pattern`,
`interaction_rule`, `compatibility`, `situation`, `outcome`, `question`.

Statuses: `unverified`, `provisional`, `verified`, `contradicted`, `stale`.

Identity: `sha256(game|category|key|content|version)` shortened to 12 hex
characters. Same game + same claim + same configuration ⇒ same record, new
evidence. Any difference in the claim ⇒ a different id, which is what allows a
contradiction to be preserved rather than overwritten.

### 5.2 Persistence

- The **existing** memory subsystem is used: `MemoryManager` over
  `SqliteMemoryStore(data_dir/memory.sqlite3)`. No new database or service.
- Namespace `game_knowledge:<sha12(casefold(game))>` holds one row per record,
  keyed by `knowledge_id`; `game_knowledge_index/games` lists known games.
- Every row carries `schema_version: "nf18.1"`; the table itself is created by
  the existing `create table if not exists` path. There is no second migration
  mechanism.
- Reads are lazy per game and cached; a restart re-reads from SQLite. Store
  failures are reported as `store_unreadable` and answered as "unknown", never
  raised.
- The cap is 200 records per game (`MAX_RECORDS_PER_GAME`). At the cap a **new**
  claim is refused with `record_limit`; an existing record still accepts new
  evidence. Nothing is silently evicted.
- Evidence is capped at 8 references per record and de-duplicated by
  independent key/fingerprint. Session memory is never an input.

### 5.3 Session memory

`SessionMemory` keeps the most recent 20 observations, objective readings and
failures per session, clears when the session id changes, and exposes
`persisted: False`. `NovaForgeManager._consume_state` updates it alongside the
knowledge ingestion, but the service has no reference to it. Tests assert it is
cleared on a new session, bounded, and absent from a fresh manager.

## 6. Evidence and provenance rules

- Each `KnowledgeEvidence` records source (`observation`, `state`, `objective`,
  `experiment`, `profile`, `configuration`, `session`, `manual`), verification
  method (`observed`, `inferred`, `experiment`, `declared`, `none`), a
  human-readable summary, timestamp, game/version, session id, observation id,
  objective id, detail, and a deterministic fingerprint.
- `independent_key` is observation id → objective id → session id → fingerprint:
  the same observation handed over repeatedly is **one** observation.
- A control mapping cannot be recorded from an input alone:
  `control_candidate` raises unless at least one evidence reference has
  `verified=True` (the expected EFFECT occurred), and the service refuses to
  promote a control record without such evidence.
- A record with no evidence is stored `unverified` and cannot be verified.
- Ingested HUD records include the resolution the field was read at, and name
  their NF-16 observation and profile provenance. NF-17-derived records name the
  objective id and mark inferred evidence as inferred.
- Evidence carries a game identity, and it must be **this** game. Evidence that
  names a different game is refused (`evidence_game_mismatch`) by both
  `record()` and `revalidate()`; evidence that names no game is accepted,
  because a declared profile or a manual note has no observation to attribute.

## 7. Verification and contradiction handling

Only these transitions are permitted (anything else raises):

```
unverified   → provisional | contradicted | stale
provisional  → verified | contradicted | stale
verified     → contradicted | stale
contradicted → provisional | stale
stale        → provisional | verified | contradicted
```

Promotion to `verified` requires **all** of:

1. status is `provisional`;
2. at least `min_verified_evidence` (default 2) independent evidence references;
3. no unresolved `contradicts` link;
4. for `control`, at least one evidence reference with the effect observed;
5. the category is not `question` (a question is the record of NOT knowing).

On a differing claim in the same `(category, key, game_version)` slot:

- the new claim is written FIRST, then the earlier record moves to
  `contradicted` with a reason and a history entry — a claim is never demoted
  before its challenger exists;
- both records are kept, including their original content and evidence;
- the new record carries `contradicts: [old id]` and is **never** auto-verified
  even if it later accumulates more evidence;
- a later correction happens only through explicit revalidation.

Revalidation accepts **one** record by id, requires fresh independent evidence
from the current session, and applies `contradicted|stale → provisional` and then
the normal promotion rule. A record that is already provisional or verified is
reported as not needing revalidation; the call does not change it.

Every status change publishes `novaforge.knowledge.updated` with the id, game,
category, key, before/after and reason. A publish failure never fails the write.

## 8. Freshness and revalidation behaviour

- **Version-bound** records (HUD fields and control mappings) are marked `stale`
  when a record for the same game reports a **known, different**
  `game_version`. The record and its evidence are kept. Manual records are never
  staled this way.
- An **unknown** version never marks anything stale.
- Age alone marks nothing stale. `summary()` reports records older than
  `revalidate_after_days` (default 30) as `due_for_revalidation`, with the
  record key, status and age. `manual` records are not included.
- Revalidation is explicit, per record, and never rebuilds the knowledge base.
- Limitation: `game_version` is the perception-profile version NF-16 records, not
  a store/platform build. A profile version bump therefore represents a
  configuration change even when the installed game did not change.

## 9. Retrieval, events and integration

### 9.1 Service surface

- `record(candidate)` — accept/refuse one candidate; never raises.
- `record_state(state)` / `record_objectives(interpretation)` — candidate builders.
- `records(game=…)` — filters: category, status, key, free-text term (key,
  category, content, provenance, version, evidence summaries and ids), limit;
  sorted verified-first, then provisional, then weaker states.
- `verified_controls(game)`, `unresolved_questions(game)`, `summary(game=…)`,
  `status()`, `find(id, game=…)`, `revalidate(id, evidence, game=…)`, `shutdown()`.
- `NovaForgeManager` exposes `knowledge_status`, `knowledge_records`,
  `record_knowledge`, `revalidate_knowledge`; `shutdown()` stops the service
  before the observer/state services.

### 9.2 HTTP

| Method | Route | Behaviour |
|---|---|---|
| GET | `/novaforge/knowledge` | status, per-game summary, due list, session memory (query `game`) |
| GET | `/novaforge/knowledge/records` | records for one game; `game` required (422 otherwise), `category`, `status`, `q`, `limit` |
| POST | `/novaforge/knowledge` | validate and record one candidate; invalid category/content/evidence ⇒ 422 |
| POST | `/novaforge/knowledge/{knowledge_id}/revalidate` | fresh-evidence revalidation; unknown id or no new evidence ⇒ 200 with `revalidated: false` and the reason |

All four are registered in `ApiSurface`, declared in `route_consumers.py` and
documented in the regenerated `docs/API.md`.

### 9.3 UI

The NovaForge panel gains a **read-only** Game Knowledge card:

- a game field (blank uses the game of the last validated state);
- record rows: category, key, status, independent-observation count, version,
  reason, "needs revalidation", last update and the last status transition;
- open questions, records due for revalidation, and bounded per-record history;
- facts: game, record counts by status, open questions, revalidation period,
  session-memory counts marked **not persisted**, persistence backend and last
  error;
- the card repaints after every state reading (the reading itself ingests the
  knowledge), on the Refresh button and on tab focus.

The card performs no writes: the Node-driven and static tests pin that it uses
only the scoped GET routes and that no click records an observation.

## 10. Automated tests — commands and exact results

All run on Windows with `PYTHONPATH=src` (the project's test setup; pytest adds
the test directory itself).

These are the exact results of the final verification pass, after the fixes
recorded in §13.1.

| Command | Result |
|---|---|
| `python -m pytest tests/test_novaforge_knowledge.py -q` | **64 passed** |
| `python -m pytest tests/test_novaforge_objectives.py tests/test_novaforge_state.py tests/test_novaforge_observation.py -q` | **167 passed** |
| `python -m pytest tests/test_novaforge_api.py tests/test_novaforge_ui.py tests/test_route_shape_contract.py -q` | **189 passed, 165 subtests passed** |
| `python -m pytest tests/test_web_api.py tests/test_application.py -q` | **109 passed, 87 subtests passed** |
| `python -m pytest tests/test_novaforge.py -q` | **77 passed** |
| `python -m mypy src` | **Success, 446 source files** |
| `python -m mypy src --platform win32` | **Success, 446 source files** |
| `python scripts/generate_api_reference.py --check` | **in sync** (346 routes) |
| `node --check src/novacontrol/web/static/js/novaforge.js` | **clean** |

Focused coverage includes: record/evidence validation and the full transition
table; unknown categories/statuses; game association; missing evidence; candidate
id stability; payload validation; promotion after two independent observations;
duplicate observations not counting; contradiction preservation; input-only
control mappings never verifying; effect-based verification; questions never
verifying; version staleness (and manual records not staling); age-only reporting;
revalidation success/refusal; game isolation and all retrieval filters; evidence
search; restart persistence; unreadable/failed stores; record/evidence caps;
obsolete-session refusal and shutdown; foreign-game evidence refusal (record and
revalidate); a challenge written before its rival is demoted; state and objective
candidate building; manager ingestion, session-memory failures and
stopped-observation refusal; HTTP validation and contradiction visibility; panel
DOM ids, Node-driven rows/facts/hints/history and read-only behaviour.

Not run: the full repository suite (not requested; the phase and its direct
dependencies were run instead).

**Live HTTP check (implementation pass, not repeated in the verification pass):**
a temporary server on port 8001 served `/` with the new card markers and UI stamp
`UI v98d29255`; `GET /novaforge/knowledge` returned the empty status,
`POST /novaforge/knowledge` stored a provisional record,
`GET /novaforge/knowledge/records?game=…` returned it, and the server was then
stopped. The two rows that check wrote into the runtime memory database were
deleted afterwards, so no test knowledge remains in the user's store. The
verification pass exercised the same routes through the TestClient suites above,
which build the real application and its routes rather than a stand-in.

## 11. Offline evaluation results

Command:

```
PYTHONPATH=src python scripts/novaforge_nf18_knowledge_eval.py
```

Fixture: the `nc-forge-mission@1` state sequence (1280×720) plus explicit
control-mapping candidates. No pixels, no game launch, no input.

| Group | Checks | Result |
|---|---|---|
| Association | 9 | 9/9 |
| Traceability | 3 | 3/3 |
| Retrieval | 8 | 8/8 |
| Contradictions | 12 | 12/12 |
| Freshness | 9 | 9/9 |
| Persistence | 2 | 2/2 |
| Session separation | 4 | 4/4 |
| Bounds | 4 | 4/4 |
| **Total** | **51** | **51/51 (accuracy 1.0)** |

- **False facts:** 0 verified records without the required evidence; 0 verified
  questions; 0 false completion claims (outcomes are never promoted by this
  phase on their own).
- **Latency of `record()` (SQLite writes included):** final run average
  **15.618 ms**, max **36.703 ms**, 8 samples. Earlier runs measured 16–19 ms
  averages; the spread is the store and this host, not the model. Panel reads are
  separate and cheap, and ingestion happens once per validated state, not per
  frame.
- **Persistence:** every record and its status survived a service restart; the
  game index survived too.
- **Freshness:** a known version change staled the version-bound HUD and control
  records, left the manual note verified, and 40 simulated days marked nothing
  stale while reporting one record due.
- **Live game evaluation:** `NOT_TESTED`. No game was launched, focused,
  captured or driven; this phase sends no input, and live acquisition needs a
  person-authorized target.

The evaluation does **not** show that NovaForge has learned a commercial game.
It shows the storage, evidence, retrieval and verification rules behave as
specified on a declared fixture.

## 12. Known limitations, remaining defects and risks

### Limitations (by design or dependency)

1. **No live or commercial-game evidence.** All evaluation is offline on the
   declared fixture; no HUD of a real title was read.
2. **Automatic producers are partial.** HUD, objective-pattern, outcome and
   question records are produced from NF-16/NF-17. `mechanic`,
   `interaction_rule`, `compatibility` and `situation` records can be stored and
   retrieved, but only through an explicit API/CLI candidate; no detector emits
   them yet.
3. **Game identity is the game string** NF-16 read (or a caller supplied). Two
   installs sharing a title string share a namespace; executable path or store
   id are not part of the key.
4. **`game_version` is the perception-profile version**, not the game's build.
   It is the only version this build can observe; the report does not claim
   store-version awareness.
5. **Resolution is recorded, not identity-scoped.** HUD content includes the
   resolution, but a resolution change does not split the record or mark it
   stale. Only a profile version change does.
6. **Evidence and history are bounded** at 8 references and 12 transitions per
   record. Once the evidence cap is reached, newly added references are dropped
   from the stored tuple; `independent_evidence` then counts what was kept, not
   everything ever seen.
7. **Counters are per-process.** `received/duplicates/rejected/promoted/
   contradicted/stale/failed` reset on restart; the records do not.
8. **Status is not a probability.** There is no calibrated confidence and none
   is displayed.
9. **Revalidation is API-only.** The card shows which records are due but cannot
   revalidate them, because a revalidation must carry fresh evidence.
10. **Retrieval is lexical**, not semantic. It is scoped and deterministic; it is
    not a search engine.
11. **No cross-game reuse.** An identical control mapping in two games is two
    records; reuse is not claimed.
12. **No deletion surface.** Contradicted/stale records remain (bounded at 200
    per game) so history is not lost; clearing knowledge is not exposed.
13. **One SQLite writer, one file.** Knowledge writes add roughly 15–37 ms per
    record on the final run (16–43 ms observed across runs) to the state-reading
    path on this host.
14. **Independent evidence trusts the supplied observation id.** Two distinct
    ids for the same physical frame would count as two observations; the service
    cannot detect a lying caller.
15. **Objective-derived records use the MANUAL revalidation policy.**
    `ObjectiveState` carries no version, so objective patterns, outcomes and
    questions cannot be version-staled the way HUD and control records are; a
    known profile-version change only stales the version-bound records.

### Remaining defects / risks

- The game index and the record row are written separately. A failure between
  them leaves a record that a direct `records(game=…)` call can still read, but
  that `summary()`/`find(id)` would miss until the game is indexed again. This
  is caught as `index_unreadable`/store errors in diagnostics, but there is no
  reconciliation pass.
- Contradiction detection requires the **same** `game_version`. A claim with an
  unknown version and one with a known version coexist without being marked as
  contradictory.
- A promotion that is computed and then fails to persist does not increment
  `promoted` (fixed in this phase), but the record's local cache is not mutated
  either; the next read still sees the stored state.
- The memory store has no cross-row transaction. Contradiction handling now
  writes the challenger first, so a failure can no longer demote a claim whose
  challenger was never stored; it can still leave the challenge stored while the
  demotion and the game-index write did not land (`record()` reports
  `record_failed` and counts a failure). There is no rollback pass.
- `revalidate()` clears the record's `contradicts` link once fresh evidence has
  been accepted. Both records remain, and the previously contradicted claim's
  history still names the conflict, but the link on the revalidated record is
  not preserved.
- `summary()` reads every record of the selected game(s) to count statuses; at
  the 200-record cap this is still small, but it is a full scan, not an
  aggregate query.
- The panel lists one game at a time and does not offer a global record browser;
  the API does.

## 13. Final verdict

### 13.1 Verification pass — defects found and fixed

The phase was re-audited against its objectives before this verdict. Four
defects were found and fixed, and the report was updated with the exact results
above.

1. **Session memory claimed to keep failures but nothing recorded one.**
   `SessionMemory.note_failure` had no caller, while this report says failures
   are kept per session. `NovaForgeManager._consume_state` now records an
   `objective_interpretation_failed` and a `knowledge_ingest_failed` entry in
   session memory (never as knowledge), and the interpreter-failure test pins
   the entry and its reason.
2. **Evidence could name a different game and still support a record.**
   `record()` and `revalidate()` now refuse evidence whose `game` names another
   game (`evidence_game_mismatch`); evidence that names no game remains valid.
   The rule immediately caught the evaluation fixture itself, whose bounds
   candidates labelled their evidence with the main fixture game instead of the
   bounds game; the fixture was corrected. Pinned by a service test and three
   evaluation checks.
3. **A claim could be demoted before its challenger was stored.** The
   contradiction path demoted the incumbent first and persisted the challenge
   second, so a store failure in between left a `contradicted` record whose
   challenger did not exist. The challenge is now written first; a failure during
   the demotion leaves a visible challenge (with its `contradicts` link) and an
   incumbent still at the status its own evidence supports. Pinned by a
   failing-store test.
4. **The service's key-length guard was never exercised.** One test built the
   over-long candidate at the call site, so the model's constructor raised and
   `record()` was never reached. The test now asserts both refusals: the model
   refuses to build it, and the service refuses an unvalidated object handed to
   it directly.

Dead code found in the same pass was deleted rather than left in place: NF-18 had
added an `ObjectiveService.current` property that nothing in `src/`, `tests/` or
`scripts/` consumed, so it was removed and the report no longer claims that file
as modified.

Residual risks carried forward are listed in §12: no live evidence, no cross-row
transaction, the challenge-first write not being rolled back, and an evidence cap
that drops newly added references once full.

**PASS WITH LIMITATIONS.**

Verified functionality:

- typed game knowledge with evidence, provenance and bounded per-record history;
- legal status transitions enforced in code, illegal ones refused;
- promotion only on declared independent evidence (and observed effect for
  controls); questions never verified;
- contradictions preserved with both claims and explicit reasons;
- known version changes stale version-bound records; age only reports;
- per-record revalidation with fresh evidence;
- game-scoped retrieval, filters and summary; no cross-game leakage;
- persistence across a service restart over the existing SQLite memory store;
- session memory bounded, transient and provably separate;
- event publication on status changes, with failures swallowed;
- API, `ApiSurface`, consumer declarations and regenerated docs in sync;
- read-only panel card that repaints after a state reading;
- 64 focused tests, 167 dependency-suite tests, 189 API/UI/contract tests, 109
  web/application tests, 77 manager tests, both mypy views clean and
  `docs/API.md` in sync; offline evaluation 51/51 with zero false facts.

Unverified or not implemented, and stated as such:

- live/commercial-game knowledge acquisition and any real-game control mapping
  (`NOT_TESTED`);
- automatic detection of mechanics, interaction rules, compatibility limits and
  situations;
- resolution/config-scoped identity and cross-game reuse;
- calibrated confidence, semantic retrieval, a deletion/reconciliation surface.

The knowledge foundation is in place; the evidence bar is real, and the system
still cannot claim to have *learned a game* — only to have recorded and
retrieved what validated observations established.
