# NovaForge NF-16 — Game State Understanding

**Phase:** NF-16
**Date:** 2026-10-25
**Verdict:** **PASS WITH LIMITATIONS** (see §17)
**Dependency:** NF-15 unified observation — satisfied (§2)

---

## 1. Initial repository state

The working tree already contained NF-15 (unified game observation) and the
perception stack it builds on, all uncommitted alongside earlier phases:

* `src/novacontrol/novaforge/observer.py` — `GameObserver`: validated frame
  capture with identity pinning, `status()` / `last_success` / `frame_path()`,
  pausing, and a per-session observation sequence.
* `src/novacontrol/perception/` — the Phase 21 pipeline: `PerceptionEngine`,
  `PerceptionRequest`/`PerceptionMode.FAST`, Windows OCR (`text-file+windows-ocr`),
  classical connected-component region detection, `SceneRepresentation`,
  `OcrText`, `DetectedObject`, `BBox`.
* `src/novacontrol/vision/ocr.py` — `OcrWord` (per-word text + pixels + line).
* Model/hardware management (`models/manager.py`, `telemetry/hardware.py`),
  the event bus (`core/events.py`), the JSON state store.
* `web/static/js/novaforge.js` + `index.html` — the NovaForge panel with the
  NF-15 observation card (start/pause/resume/stop/observe-once) and the
  `window.NovaForgeLogic` seam the UI tests evaluate in Node.
* `data/novaforge/frames/**` — 680 real captured frames from disposable
  harness windows (2880x1800, 1832x718, 1040x440), used for offline evaluation.

No game-state representation, HUD reader, entity typing, temporal state
comparison, state API or state UI existed.

## 2. NF-15 dependency status

**Satisfied.** `GameObserver` exposes exactly the interface a state reading
needs and refuses to invent one:

| Need | Provided by |
| --- | --- |
| newest *validated* frame | `status()["last_success"]` (observation id, seq, at, width, height) |
| frame file | `frame_path()` |
| session + target identity | `status()["session_id"]`, `status()["target"]` (`handle`, `pid`, `start_time`) |
| staleness | `last_success["at"]` compared with the wall clock |

`tests/test_novaforge_observation.py` passes (49 tests + subtests), so the
interface is usable without fabricating observations. The state service consumes
it as a `ObservationSource` protocol, which is also how tests inject a fake.

## 3. Gap analysis (pre-implementation)

| NF-16 objective | State before | Gap |
| --- | --- | --- |
| A structured game-state model | none | new module |
| B HUD/interface understanding | OCR returns lines only; no regions, no per-field status, no profiles | extraction layer over a declared region |
| C visual entity detection | classical regions + OCR words exist; no typed entity records, no "unknown" category | mapping + explicit `unknown` kind |
| D temporal tracking | NF-15 tracks observation ids only | state-to-state diff with session/profile guards |
| E confidence/uncertainty | perception reports "no confidence measured" | explicit `None` + heuristic coverage + per-field reasons |
| F game-specific profiles | none | versioned profile registry with title-token match and resolution refusal |
| G resource/latency control | preview-bounded detection, one engine per process | one reading at a time, worker thread, timeout, bounded history |
| H integration | no state service, no routes | service in `NovaForgeManager`, 4 routes, typed events |
| I UI | observation card only | one "Game State Understanding" card |
| J tests | none | 57 state tests + 7 renderer tests + 1 perception regression |
| K evaluation | none | `scripts/novaforge_nf16_state_eval.py` |

## 4. Files inspected and modified

**Inspected (read, not changed):** `docs/NOVAFORGE_NF15_…REPORT.md`,
`…NF14…`, `…NF13A…`, `…NF12…`, `NOVAFORGE_UNIVERSAL_CAPTURE_REPORT.md`,
`novaforge/observer.py`, `novaforge/observation.py`, `perception/{engine,models,providers,frames}.py`,
`vision/{ocr,manager,multimodal}.py`, `core/events.py`, `application.py`,
`api/{app,models,route_consumers}.py`, `web/static/js/{novaforge,dom,state,render-utils}.js`,
`index.html`, `nc-os.css`, `tests/{test_novaforge_observation,test_novaforge_ui,test_perception,test_web_api,test_web_platform}.py`,
and the captured frames under `data/novaforge/frames/`.

**Created**

| File | Purpose |
| --- | --- |
| `src/novacontrol/novaforge/state.py` | model, profiles, extraction, change detection, state builder |
| `src/novacontrol/novaforge/state_service.py` | `GameStateService`: bounded reading, staleness/supersession, events, persistence |
| `tests/test_novaforge_state.py` | model, profiles, extraction, tracking, service, HTTP |
| `scripts/novaforge_nf16_state_eval.py` | offline fixture + recorded-frame evaluation |
| `data/novaforge_nf16_state_eval.json` | generated evaluation record |
| `docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md` | this report |

**Modified**

| File | Change |
| --- | --- |
| `novaforge/manager.py` | construct `GameStateService` on the live observer; shut it down with the manager |
| `novaforge/__init__.py` | export the state model, profiles, registry, service |
| `api/app.py` | 4 routes (`GET /novaforge/state`, `POST /novaforge/state/analyze`, `POST /novaforge/state/reset`, `GET /novaforge/state/profiles`) |
| `api/models.py` | 4 `ApiSurface` rows |
| `api/route_consumers.py` | 4 render consumers (NovaForge panel) |
| `docs/API.md` | regenerated (`--check` in sync) |
| `web/static/index.html` | one "Game State Understanding" card |
| `web/static/js/novaforge.js` | card renderer + controls + `stateFactRows`/`stateHudRows` on `NovaForgeLogic` |
| `web/static/nc-os.css` | card/row styles |
| `perception/engine.py` | **defect fix**: line 0 is a line (see §15 D1) |
| `tests/test_perception.py` | regression pin for line grouping |
| `tests/test_novaforge_ui.py` | `StateCardLogicTests` (7 tests, real `novaforge.js` in Node) |

## 5. State model and architecture

```
NF-15 GameObserver ──(validated frame + status)──▶ GameStateService
                                                     │
                                    injected seam ───┤ PerceptionEngine (FAST: OCR + regions, no VLM)
                                                     ▼
                                   SceneRepresentation ──▶ build_game_state() (pure) ──▶ GameState
                                                     │                                    │
                                        event bus ◀──┘                          history (bounded)
```

Everything typed is a frozen dataclass with `to_dict()` (the repository
convention), not Pydantic:

| Record | Holds |
| --- | --- |
| `ObservationRef` | `observation_id`, `session_id`, `game`, `seq`, `at`, `width`/`height`, `backend`, `frame_id` |
| `GameState` | `state_id`, `game`, `observation`, `sequence`, `timestamp`, `status`, `hud`, `text`, `entities`, `entity_status/reason`, `interface`, `changes`, `tracking`, `unknown`, `unsupported`, `profile`, `confidence`(None), `uncertainty`(None), `quality`, `latency`, `reasons`, `limitations` |
| `HudReading` | `element_id`, `kind`, `label`, `box`, `profile_id`, `status`, original `text`, parsed `value`, `reason` |
| `StateEntity` | `entity_id`, `kind` (`unknown` only), `box`, `confidence`(None), `overlaps_hud`, `source` |
| `InterfaceState` | prompts/menus/dialogs/notifications read from declared regions, plus `menus_visible` |
| `StateChange` | `kind`, `field_name`, `before`, `after`, `detail`, `inferred`, `observation_id` |
| `UnknownField` | `field_name`, `kind`, `status`, `reason` |
| `Tracking` | `compared`, `previous_state_id`, `previous_observation_id`, `dropped_observations`, `reason` |
| `GameProfile` / `FieldSpec` / `ProfileMatch` / `ProfileRegistry` | game identity, version, provenance, reference size, regions, thresholds, limitations; deterministic match; rejected profiles recorded |

The four separations Scope A demands are structural: `text` is the generic
reading, `hud` is recognized interface elements, `profile` is what the game
allows to be presumed, and `changes`/`interface`/`quality` are derived.

`build_game_state()` is a pure function of `(scene, observation, profile_match,
previous)`, so everything about extraction is testable without OCR or a desktop.

## 6. OCR and HUD capabilities

Extraction fills **only** declared profile regions, and every declared field
answers with exactly one of five statuses:

| Status | Meaning | Evidence kept |
| --- | --- | --- |
| `recognized` | text read and (for number fields) parsed | `text`, `value`, `box` |
| `absent` | region looked at, nothing recognized there | reason says "not proof the element is absent" |
| `unreadable` | no reading could be attempted (no OCR engine, no frame size) | reason |
| `ambiguous` | text read but not usable (too short, not a number) | original `text`, reason |
| `unsupported` | the element type has no measurement in this build (`health`, `map`, `progress`) | reason |

* **Word-level geometry first, line-level fallback.** `_placed_words()` reads the
  per-word text detections the OCR provider already publishes
  (`metadata.kind == "text"`), assigns a word to a region by its box centre, and
  groups by the reader's line index. A scene with only line records falls back
  to lines, with the known limitation in §10.
* **Numbers are parsed, never guessed.** `parse_number` accepts a value the text
  *fully* states (`786`, `1,234`, `87%`); `"LEVEL 26"` is `ambiguous` rather than
  `26`, because the region was declared to hold a number and choosing a substring
  would invent which part the field meant.
* **Three shipped profiles**, each with provenance, reference resolution and
  limitations; commercial titles deliberately have none:
  `nc-forge-tick@1` (1832x718 audit windows: header, numeric counter),
  `nc-forge-harness@1` (1040x440: title, state, prompt, note),
  `game-menu@1` (2880x1800: title, menu entries).
* **A title match is not enough.** A profile that declares a resolution and is
  matched at a different frame size is refused with
  `resolution_not_declared` — its regions would measure the wrong pixels.
* OCR text is preserved in `GameState.text` (line, text, bbox, reading order,
  source) so a panel can show what the reader actually saw.

## 7. Entity-detection capabilities

Entity detection reuses what the shipped perception path already produces:
text detections and classical connected-component regions. Each becomes a
`StateEntity` with:

* `kind` **always `unknown`** — the detector is geometry, not semantics, and
  naming a "health bar" or an "enemy" from a component would be a fabricated
  classification;
* exact `box`, `source`, `overlaps_hud` (whether it sits inside a HUD region), and
  the frame/observation identity carried by the scene;
* `confidence: None`, because connected components measure no confidence.

`entity_status` distinguishes `detected`, `none` (detection ran, nothing found)
from `unavailable` (detection could not run, with a reason). No-detection is a
result, not an empty list pretending to be one. The builder never treats pixel
differences as an event; the only entity changes it reports are
`entity_status_changed` and `entity_count_changed`, and the latter's detail
states it counts detected regions, not game objects.

## 8. Temporal tracking behaviour

Comparison happens only when the previous state belongs to the **same session**
and the **same profile**; otherwise `Tracking` records
`"the previous state belongs to a different session"` (or the profile change)
and `changes` stays empty. No cross-session comparison exists.

Change kinds: `hud_value_changed`, `hud_text_changed`, `hud_status_changed`,
`hud_disappeared`, `entity_status_changed`, `entity_count_changed`. Each carries
`before`, `after`, `inferred: False`, the `observation_id` it was seen in, and a
human sentence.

* **Dropped frames**: `dropped_observations = seq - previous.seq - 1`, reported
  rather than smoothed; irregular intervals never produce a fabricated change.
* **No identity claim**: there is no "the same enemy moved" statement — the model
  records that a count changed. Object identity across frames is untracked
  (tracking evidence does not exist in this build).
* **Reset**: `POST /novaforge/state/reset` drops the comparison baseline and keeps
  history; a new observation session also restarts the count.
* **Uncertainty**: every non-recognized field is an `UnknownField` with a reason,
  so a state made of guesses cannot look like one made of readings.

## 9. Confidence and uncertainty handling

* `GameState.confidence` and `.uncertainty` are **`None`**: no component on this
  machine measures a calibrated probability.
* `quality` is explicitly heuristic — `{heuristic: true, declared, recognized,
  coverage, notes}` — and the note says it is a count of declared fields read,
  not a confidence.
* Component-level evidence is preserved: per-field `status`, original `text`,
  `box`, `profile_id`, and the field's `reason`.
* `unknown` lists every field that was not recognized with why; `unsupported`
  lists element types this build cannot measure and each profile's declared
  limitations.
* Thresholds are data: `max_age_seconds`, `analyze_timeout`, `max_states`,
  `max_entities`, per-field `min_chars`, and the profile itself are constructor
  arguments. `DEFAULT_MIN_CHARS` is the only hard default.
* Nothing promotes a low-confidence result: `ambiguous` is never a `value`.

## 10. Resource and latency measurements

Design: one `PerceptionEngine` per service (OCR initialised once), one reading at
a time (`analysis_in_progress` refusal), a worker thread with
`analyze_timeout` (default 10 s), bounded history (`max_states`, default 32),
bounded entity count (32), and no image queue — the service reads the observer's
one newest frame and drops it after the pass.

Measured offline (`scripts/novaforge_nf16_state_eval.py --frames 3`, Windows
host, CPU only, no model loaded):

| Figure | Value |
| --- | --- |
| frames read | 4 / 4 |
| declared fields recognized | 10 / 10 |
| first analysis (includes engine construction) | 1542.0 ms |
| steady-state average | 1215.3 ms |
| max | 1542.0 ms |

(Measured after the second verification pass, §15.1; the first run recorded
1727.5 / 1174.8 ms, so expect a few hundred ms of run-to-run noise.)
| model load | `null` — no model is loaded; OCR + region detection only |

Because `model_load_ms` is `None` with a note rather than `0`, "no model" cannot
be mistaken for "a model loaded instantly". These are **not** real-time figures
and are not claimed as such: ~1.2 s per 1832x718 frame on CPU is a reading
cadence, not a frame loop.

## 11. Automated test commands and exact results

```
PYTHONPATH=src python -m pytest tests/test_novaforge_state.py -q
  57 passed

PYTHONPATH=src python -m pytest tests/test_novaforge_ui.py -q
  140 passed (7 of them StateCardLogicTests, real novaforge.js in Node)

PYTHONPATH=src python -m pytest tests/test_perception.py -q
  91 passed (includes TextLineGroupingTests)

PYTHONPATH=src python -m pytest tests/test_novaforge_observation.py \
  tests/test_novaforge_api.py tests/test_route_shape_contract.py \
  tests/test_agentcore.py tests/test_novaforge_vision.py -q
  187 passed, 90 subtests passed

PYTHONPATH=src python -m pytest tests/test_web_api.py tests/test_web_platform.py -q \
  -k "asset or ui_version or static or render_chat or envelope"
  11 passed, 41 subtests passed

PYTHONPATH=src python -m mypy src
  Success: no issues found in 442 source files

PYTHONPATH=src python -m mypy src --platform win32
  Success: no issues found in 442 source files

PYTHONPATH=src python scripts/generate_api_reference.py --check
  docs/API.md: in sync
```

The full suite (~2,254 tests) was **not** run: NF-16 changed one shared module
(`perception/engine.py`) and every consumer test of it — `test_perception`,
`test_agentcore`, `test_novaforge_vision`, `test_novaforge_observation`,
`test_novaforge_api`, `test_route_shape_contract` — was run.

Coverage of Scope J:

* models: valid/invalid values, duplicate ids, out-of-frame regions, unknown and
  missing fields, `confidence is None`, observation/session references.
* OCR/interface: recognized text, empty region, ambiguous text, non-numeric
  counter, missing OCR engine, unmeasured kinds, unparseable frame size.
* entities: detections, none, unavailable, HUD overlap, cap.
* tracking: value/text/status changes, disappearance, dropped observations,
  session change, profile change, reset.
* integration: valid observation processed, stale frame refused, superseded
  observation refused, concurrent analyze refused, shutdown, store round-trip,
  bounded history, HTTP status/analyze/reset/profiles, surface declaration.
* UI: the renderer distinguishes all five field statuses, never prints a zero for
  a missing field, and separates measured from unmeasured latency.

## 12. Offline evaluation results

**Fixture evaluation (deterministic, ground truth exists): 20/20 checks,
accuracy 1.0.** Checks cover: counter read as 786.0; header text; 786→782 is one
`hud_value_changed` naming both ends; an unchanged frame reports no change; a
region that stops reading becomes `hud_status_changed` + `absent` with no value;
`"LEVEL 26"` is `ambiguous` with the text preserved; absent/ambiguous fields are
in `unknown` with reasons; a new session is not compared; an unknown title
presumes no HUD (`partial`, never `ok`); a dropped observation is counted.

**Recorded-frame pass (real OCR, no ground truth): 4/4 frames analyzed, 10/10
declared fields recognized.**

| Frame | State | Fields |
| --- | --- | --- |
| `312f…/000001.png` (1832x718) | ok | header `NC FORGE AUDIT`, counter `786` |
| `312f…/000002.png` | ok | header `NC FORGE AUDIT`, counter `787` |
| `312f…/000003.png` | ok | header `NC FORGE AUDIT`, counter `788` |
| `1955…/000001.png` (1040x440) | ok | title `NC FORGE HARNESS`, state `- idle`, prompt `WAITING FOR INPUT`, note read |

Reported as **`null`, deliberately**: OCR accuracy, HUD element accuracy, entity
precision/recall. These frames have no labelled expectations, so the only
measurements are latency, statuses, and the text that was actually read. No
precision/recall figure is printed because no labelled set exists.

## 13. Live-test results

**`NOT_TESTED`.** No game was launched, focused, driven or killed during this
phase, and no frame was captured from a live target. Live evaluation is a Scope K
safety decision made by a person against a real target (memory gate, no duplicate
instances, no user-owned process, no disconnected drive, no synthesized input).
Everything above was measured on recorded frames and deterministic fixtures.

## 14. Known limitations and untested game types

1. **Line-granularity readers lose values.** If a reader reports only lines (no
   per-word boxes) and a line holds a label plus a value, a region covering only
   the value sees nothing; the field is `absent` even though the text was read.
   NF-16's evaluation hit exactly this and the word-level path fixed it for this
   build's OCR; a future reader without word boxes will hit it again.
2. **HUD reading is text, not graphics.** No bar fill, meter, colour, icon or
   minimap position is measured; `health`, `map` and `progress` are `unsupported`
   by design.
3. **Profiles are harnesses, not games.** There is no commercial-game profile.
   The mechanism is demonstrated on three disposable/fixture windows at one
   resolution each; a new resolution is a refusal, not a stretch.
4. **Entities are geometry.** No semantic detection, so `kind` is always
   `unknown` and entity precision/recall cannot be measured.
5. **No identity tracking.** Entities are not associated across frames.
6. **No objective/health/score inference.** Explicitly NF-17 territory.
7. **Untested game types:** any real title, any HUD with animated bars, any
   non-Latin UI, any resolution other than the three declared, multi-monitor and
   DPI-scaled capture paths for this feature.
8. **Latency** (~1.2 s/frame) is adequate for periodic readings, not for a
   perception loop, and was measured on one machine.
9. **No configurable image resizing (deviation from Scope G).** Every profile's
   regions are declared in the frame's own pixels, and the recorded-frame
   evaluation showed full resolution is what makes the counter readable; a
   resize knob would either move those regions or require a second coordinate
   space, so none is offered. `status.limitations` states this, and the service
   reads frames at capture resolution.

## 15. Defects found and fixed during verification

All four were found by *evaluation*, not by reading the code, and each has a
regression pin.

**D1 — `perception/engine.py` merged line 0 into other lines** (pre-existing,
shared with every perception consumer).

`grouped.setdefault(word.line if word.line else index, ...)` treated line index
`0` as "no line", so each word of the first line was keyed by its own index and
absorbed words from a later line with the same index. Observed before the fix on
`312f…/000001.png`:

```
'NC'                (75,24,30,17)
'FORGE TICK 786'    (114,24,1076,244)   ← union of a title word and the readout
'AUDIT x'           (192,20,1587,21)    ← union of a title word and the close button
```

After: `'NC FORGE AUDIT'` and `'TICK 786'`, each with its own line's box. Pinned
by `TextLineGroupingTests` in `tests/test_perception.py`.

**D2 — the shipped tick profile could never match its own frames.**
`nc-forge-tick@1` required the token `tick`, but the captured 1832x718 windows are
titled **`NC FORGE AUDIT`** (the tick is the readout). The profile was
unreachable for the provenance it declares. Tokens narrowed to
`("nc", "forge", "audit")`; pinned by
`test_the_tick_profile_matches_the_window_its_regions_were_measured_from`.

**D3 — a region ignored a value inside a labelled line.** Assignment used the
line's union centre, so `TICK 786` (centre x≈915) fell outside the counter region
(940–1260) and the counter read `absent` although `786` had been recognized
exactly. Fixed by reading the per-word text detections the OCR provider already
publishes (`_placed_words()`), keeping the line path as fallback. After the fix
the three audit frames read `786`, `787`, `788` as numbers. Pinned by
`test_a_value_inside_a_labelled_line_is_read_from_the_word_box` (which also
asserts the line-only path still reports `absent`, honestly).

**D4 — overlapping declared regions read the same words twice.** The harness
profile's `title` (60..360) overlapped `state` (305..425), so both read
`- idle`; `game-menu@1`'s title/menu overlapped by 7 px. Regions are now abutting
and an invariant test
(`test_shipped_regions_never_overlap_each_other`) fails any future profile whose
declared regions overlap.

### 15.1 Second verification pass (post-report audit)

A full re-audit against Scopes A–L after the report was first written found and
fixed these; each has a test unless noted.

**D5 — refusals left no diagnostic.** Only `_fail` results were stored, so a
stale frame, missing frame, unknown profile or superseded result was invisible
the moment the page reloaded (Scope I asks for diagnostics that distinguish
those). The service now records `last_refusal` (code, reason, timestamp) without
counting it in `failed`, clears it on the next success or failure, and the card
shows a **Last refusal** fact row. Covered by a service test, an HTTP test and
the renderer test.

**D6 — the refusal sentence was overwritten by the refresh.**
`analyzeGameState` set the hint from the refusal and then called
`loadGameState()`, which repainted the hint from the stored status — so the user
saw the previous state's sentence instead of the reason. The refresh now happens
first and the refusal is written after it.

**D7 — `reset()` kept a restored baseline.** `reset()` cleared the current state
but not the persisted `previous` record, so `/novaforge/state` kept advertising a
prior state the reader no longer held. Both are cleared now; the reset test
asserts it.

**D8 — dead and misleading code removed.** `HudReading.source` was the literal
`"ocr"` (and empty on absent/ambiguous readings); it now carries the reader that
actually produced the text (`ocr:windows-ocr`, a fixture's `ocr:fake`, …) or stays
empty when no reader produced text — Scope E's traceability, honestly. Removed:
an always-`UNAVAILABLE` ternary in the entity branch, an unreachable branch in
`_status_for` (the required-field check already returned), the unused
`replace_state`, `game_state_summary`, `set_profile` and `STATE_EVENTS`, and the
now-unused `dataclasses.replace` import.

**D9 — cancellation was untested.** Scope J lists it beside shutdown; a test now
cancels the awaiting caller mid-reading and asserts the shielded worker finishes
and a later reading is accepted rather than the service staying wedged.

Re-run after the fixes: phase tests **288 passed** (57 state, 140 panel, 91
perception), the dependency/integration set unchanged at **187 passed** plus 90
subtests, both mypy views clean, `docs/API.md` in sync (no route changed), and
the offline evaluation still 20/20 fixtures and 4/4 frames with 10/10 fields.

## 16. Remaining defects and risks

1. **Line-only readers** (§14.1) are a known read failure mode for label+value
   lines; the state says `absent` (not `unrecognized-value`), which is honest but
   not maximally informative.
2. **`state` reads a suffix.** The harness profile's `state` field is the suffix
   of the window title (`- idle`), not a game state; its own limitations tuple
   says so, but a reader may over-read it.
3. **No automated CI coverage of the evaluation script.** It is a manual run that
   writes `data/novaforge_nf16_state_eval.json`; a CI run would need the recorded
   frames (they are in the tree) and Windows OCR on the runner (not available on
   the Ubuntu CI image), so the recorded-frame pass is author-machine evidence.
4. **`data/novaforge_nf16_state_eval.json` is generated** and recorded here so the
   numbers are reviewable; regenerate with the script rather than editing it.
5. **Risk — profile drift:** regions measured off one frame of one resolution can
   silently mis-read after a UI change. The resolution refusal and the
   `min_chars`/number-parse statuses surface this, but a profile is only as good
   as its provenance line.
6. **Risk — the state is a snapshot.** Consecutive readings are compared, but a
   dropped or paused observation makes the compared interval unknown; it is
   reported as a count, never as a duration.
7. **`last_refusal` is in memory only.** It survives a page reload (it is served
   by `/novaforge/state`) but not a process restart, by choice: it is a
   diagnostic, not part of the record.
8. **A succeeded reading clears the refusal**, so the card cannot show "refused,
   then read" at the same time; the event stream carries both, the card shows the
   latest answer.

## 17. Verdict

**PASS WITH LIMITATIONS.**

Delivered and verified: a typed, uncertainty-aware game-state model over NF-15's
validated observations; region-based HUD extraction with five distinct field
answers; a word/line assignment that reads what the shipped OCR actually saw; a
bounded, resource-aware state service with staleness, supersession, timeout and
shutdown handling; state-to-state change tracking with session/profile guards and
dropped-frame accounting; a versioned perception-profile mechanism with
deterministic matching and resolution refusal; four authenticated API routes,
events, and one diagnostic UI card; 57 state tests, 7 renderer tests, 1
perception regression, two clean mypy views; a 20/20 fixture evaluation and a
4/4-frame real-OCR evaluation with 10/10 declared fields recognized at ~1.2 s per
frame.

Not delivered, and not claimed: semantic entity recognition, bar/health/map
measurement, cross-frame entity identity, any commercial-game profile, and live
game verification (`NOT_TESTED`). Four defects — one of them in the shared
perception engine, affecting every consumer — were found by the evaluation and
fixed, each now pinned by a test.
