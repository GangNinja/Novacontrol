# NovaForge NF-17 — Objective and Progress Understanding

**Phase:** NF-17
**Scope:** A typed objective model, evidence-backed objective discovery, progress
tracking, completion/failure verification, bounded history, NovaForge integration,
a panel card and an offline evaluation. **No gameplay action is planned or sent.**
**Verdict:** **PASS WITH LIMITATIONS** (see §15)
**Live game evaluation:** `NOT_TESTED` (see §11)

---

## 1. Initial repository state

The tree already contained the NF-15 unified observation pipeline (validated frames,
session identity, pause/resume/stop and a bounded observation history) and the NF-16
game-state reader (a `GameState` built from one validated frame, with HUD readings,
region detections, change tracking, unknown bookkeeping and per-component
measurements). NF-07/NF-08 gameplay objectives existed as a **verification** registry
(`novaforge/objective.py`: `ObjectiveDefinition`, `ObjectiveRegistry`, verifiers) —
that machinery verifies an objective a person states; nothing interpreted what the
screen asks for.

Working tree state at the start of the phase: uncommitted NF-15/NF-16 work, a green
NF-16 suite, and no objective-understanding module, route, panel card or evaluation.

## 2. NF-16 dependency status

`docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md` reports **PASS WITH
LIMITATIONS**, with recorded-frame latency measurements and a labelled fixture
evaluation. NF-16 therefore supplies the required input:

* `GameState` with `observation` identity (`session_id`, `observation_id`, `seq`, `at`),
* profile-matched `HudReading`s with five statuses (`recognized`, `absent`,
  `ambiguous`, `unreadable`, `unsupported`),
* an explicit `unknown` list and per-reading `reason` strings.

NF-16 does not, however, ship a profile for any commercial game's mission panel. The
dependency is therefore satisfied at the **interface** level and satisfied for a
**declared fixture layout** only. NF-17 consumes states, never frames, so no capture,
OCR or detection code was needed or added.

## 3. Gap analysis (pre-implementation)

| Requirement | State before NF-17 | Gap |
| --- | --- | --- |
| Typed objective record | only `ObjectiveDefinition` (a person's stated objective) | no interpretation record, statuses, provenance or history |
| Objective discovery | none | nothing read objective text |
| Progress tracking | none | no counters, resets or start values |
| Dependencies | none | no representation at all |
| Completion/failure rules | verifiers for stated objectives | no screen-evidence rules |
| Bounded history | session timelines exist (NF-03) | no objective transitions |
| Integration | `GameStateService` publishes `novaforge.state.analyzed` | no consumer hook |
| API surface | state routes only | no objective routes |
| UI | state card only | nothing shows objectives |
| Evaluation | NF-16 fixture + recorded frames | nothing to score objective claims |

The one deliberate constraint taken from the phase text: **no confidence numbers**.
No component measures a calibrated probability, so `confidence` is `None` throughout
and the panel never renders one.

## 4. Files inspected and modified

**Inspected (preflight):**

* `docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md`
* `docs/NOVAFORGE_NF15_UNIFIED_GAME_OBSERVATION_REPORT.md`
* `docs/NOVAFORGE_NF14_GAME_EMBEDDING_COMPATIBILITY_REPORT.md`
* `docs/NOVAFORGE_NF13A_EMBEDDED_GAME_HOST_REPORT.md`
* `src/novacontrol/novaforge/{state,state_service,observer,objective,manager,guardian,models,games,timeline,benchmark_lab,evaluation,coach}.py`
* `src/novacontrol/perception/{models,providers,frames}.py`
* `src/novacontrol/application.py`, `src/novacontrol/api/{app,models,route_consumers}.py`
* `src/novacontrol/web/static/{index.html,nc-os.css,js/novaforge.js}`
* `scripts/novaforge_nf16_state_eval.py`, `docs/API.md`
* `tests/{test_novaforge_state,test_novaforge_observation,test_novaforge_ui,test_novaforge,test_novaforge_gameplay,test_web_api,test_web_platform,test_route_shape_contract,test_application}.py`

**Created:**

* `src/novacontrol/novaforge/objectives.py` — model, profiles, discovery, transitions
* `src/novacontrol/novaforge/objective_service.py` — bounded consumer of states
* `tests/test_novaforge_objectives.py` — 61 focused tests
* `scripts/novaforge_nf17_objective_eval.py` — labelled offline evaluation
* `data/novaforge_nf17_objective_eval.json` — generated results (runtime data)
* this report

**Modified:**

* `src/novacontrol/novaforge/state.py` — the declared `nc-forge-mission@1` perception
  profile (fixture layout) and its provenance
* `src/novacontrol/novaforge/state_service.py` — the `on_state` consumer hook
* `src/novacontrol/novaforge/manager.py` — wires the objective service, renames it
  `objective_state`, stops it on shutdown
* `src/novacontrol/novaforge/__init__.py` — exports
* `src/novacontrol/api/app.py` — three routes
* `src/novacontrol/api/models.py`, `src/novacontrol/api/route_consumers.py`, `docs/API.md`
* `src/novacontrol/web/static/index.html`, `js/novaforge.js` — the objectives card
* `tests/test_novaforge_state.py` — profile lookup by id (defect 5, §14)

## 5. Objective data model

`novaforge/objectives.py`, all frozen dataclasses with construction-time validation:

* **`UnderstoodObjective`** — `objective_id`, `description`, `session_id`, `game`,
  `source`, `source_text`, `category`, `status`, `explicit`, `progress`,
  `completion_conditions`, `failure_conditions`, `dependencies`, `evidence`,
  `first_seen`, `last_updated`, `reason`, `superseded_by`, `revision`, `confidence`
  (always `None`). A superseded record must name its successor; a description is
  capped at 300 characters; a revision starts at 1.
* **`ProgressRecord`** — `kind` (`numeric|stage|qualitative|none`), `text`, `value`,
  `target`, `start`, `fraction`, `observation_id`, `at`, `resets`, `reason`.
  `fraction` exists only when a start, a value and a target are all known and the
  target is ahead of the start; otherwise it is `None`, never a zero that reads like
  "no progress".
* **`ObjectiveEvidenceRef`** — `kind` (`objective_text|progress|completion_text|failure_text|counter_target|dependency|session`),
  a human `summary`, and the `observation_id`/`session_id`/`at` it came from.
* **`ObjectiveDependency`** — `requires`, `source` (`declared` only; `inferred` is
  refused), `satisfied`, `reason`.
* **`ObjectiveEvent`** — `kind`, `at`, `session_id`, `objective_id`, `before`, `after`,
  `reason`, `evidence`.
* **`ObjectiveState`** — `session_id`, `game`, `observation_id`, `at`, `sequence`,
  `objectives`, `events`, `diagnostics`, `reasons`, `limitations`, `profile_id`,
  `schema_version`.
* **`ObjectiveRule` / `ObjectiveProfile` / `ObjectiveProfileRegistry`** — rules name
  HUD element ids from the matching NF-16 profile (never pixels), with targets,
  completion/failure message lists, declared dependencies and an `inferred` flag.
  `problems()` reports duplicate ids, unknown prerequisites, self-dependencies and
  dependency cycles; the registry records a broken profile as a rejection instead of
  raising.

Statuses: `unknown`, `discovered`, `active`, `in_progress`, `completed`, `failed`,
`blocked`, `superseded`, `not_verifiable`. Only `completed` and `failed` are terminal.

## 6. Discovery and progress-tracking behaviour

**Discovery.** With a profile matched, each rule is applied to the state's readings:
the `objective_field` supplies the objective text, `counter_field` supplies progress,
and messages are searched for declared completion/failure texts. A rule whose field is
missing is reported and not applied; a field that is declared but held nothing is
skipped as "no objective is shown"; a field that could not be read produces `UNKNOWN`
with the reading's own reason. Without a profile, only lines that **name themselves**
(`Objective:`, `Mission:`, `Quest:`, `Task:`, `Tutorial:` followed by a separator) are
read — an arbitrary instruction or disclaimer is not an objective. Identical generic
lines collapse to one objective; distinct ones become separate objectives.

**Progress.** A numeric counter produces a `ProgressRecord` with `start` pinned to the
first observed value, a `fraction` computed by one function
(`progress_fraction`) and only when it is meaningful, and a `resets` counter bumped on
every observed decrease. A repeated value emits no event. A counter that is
`absent`/`ambiguous`/`unreadable` claims no progress and carries the reading's reason
verbatim. No progress number is ever invented for a bar, checkpoint or stage.

## 7. Completion and failure verification rules

* **Completion** requires either a declared completion message read on screen, or a
  declared counter reaching its declared target. The evidence kind is recorded
  (`completion_text` / `counter_target`) with its field, text, value and observation.
* **Failure** requires a declared failure message.
* **Both at once** is a conflict: a diagnostic is recorded and **no verdict** is
  claimed.
* **Disappearance is not completion.** An objective that stops being displayed becomes
  `not_verifiable` with that reason and an `unverifiable` event.
* **A contradicted verdict is corrected.** Terminal verdicts are kept while nothing
  contradicts them; if the objective is displayed again, a `corrected` event moves it
  back to `active` and says why.
* **A changed objective in the same declared slot** supersedes the old one, naming the
  replacement; the old record stays in the list with its history intact.
* **A session change does not merge**: another session's live objectives are
  superseded with an event, and a terminal verdict from the previous session is simply
  not carried.

## 8. Objective history and dependencies

History lives in `ObjectiveService`: a bounded `history` deque (32 readings) and an
`events` deque (64 transitions), persisted in the existing JSON state store under the
namespace `novaforge_objectives`. Each history row names the observation, session,
sequence, profile and per-objective status/progress; each event names the objective,
the before/after status, the reason and the evidence. The current picture is
deliberately **not** restored on boot (a stale objective must not look current): the
panel reports `restored` and shows history, with the current state left untested.

Dependencies are represented only where a rule declares them, satisfied only by a
`completed` objective **in the same reading's session**; an unsatisfied prerequisite
sets `blocked` (with a `blocked` event) and the block is lifted with an `activated`
event once its prerequisite completes. A declared prerequisite that produces no
objective is a diagnostic, not a silent pass.

## 9. Integration with NovaForge

* **One input**: `GameStateService` calls an injected `on_state` **after** a state is
  stored and published; the consumer is awaited inside its own guard, so a failure in
  objective interpretation can never fail a reading.
* **One direction**: the objective service never captures, perceives or acts. It has no
  path to input, policy or the desktop controller.
* **Ordering and dedup**: the same `(session, observation_id)` is counted once
  (`duplicates`), an observation not newer than the newest in its session is ignored
  with `observation_out_of_order`, and nothing is re-applied.
* **Stale targets cannot reach it**: NF-16 already refuses a reading whose target
  changed mid-perception (`observation_superseded`), so an obsolete target's state
  never becomes an objective.
* **Cancellation/shutdown**: `ObjectiveService.shutdown()` stops accepting states, and
  `NovaForgeManager.shutdown()` awaits it before the state service and sessions close.
* **Panel**: the objectives card shows the game, session, observation, matched
  profile, current objectives (with source, explicit/inferred, progress,
  `progress last read` when a retained value is older than the reading, unmet
  prerequisites and evidence summaries), superseded count, diagnostics, restored
  history and the transition log. It counts CURRENT objectives only, so its
  figures cannot disagree with the list beside them.
* **Events**: `novaforge.objectives.updated` carries session/observation/sequence,
  profile, event kinds, counts and per-objective status + progress — **no raw screen
  text**. Publishing is guarded; a lost event cannot fail an interpretation.
* **Errors**: a raising interpreter is recorded as `last_error`
  (`interpretation_failed`) and the reading continues.

## 10. Resource and latency measurements

Interpretation is pure Python over an existing state: no model, no image, no extra
capture, no new queue. Measured by `scripts/novaforge_nf17_objective_eval.py`
(11 interpretations, same process):

| Metric | Value |
| --- | --- |
| Average interpretation | **0.30 ms** |
| Maximum interpretation | **0.72 ms** |
| Model load time | `None` — no model is loaded |
| Bounded history | 32 readings / 64 events |
| Additional captures | 0 |

No claim of real-time performance is made: these numbers measure interpretation only,
not capture or OCR, and they are from a machine where NF-16's OCR path is the shipped
one.

## 11. Automated test commands and exact results

All run from the repository root with `PYTHONPATH=src`:

```
python -m pytest tests/test_novaforge_objectives.py -q
61 passed

python -m pytest tests/test_novaforge_state.py -q
57 passed

python -m pytest tests/test_novaforge_state.py tests/test_novaforge_ui.py tests/test_novaforge_observation.py tests/test_route_shape_contract.py -q
264 passed, 165 subtests passed

python -m pytest tests/test_web_api.py tests/test_web_platform.py -q
120 passed, 457 subtests passed

python -m pytest tests/test_novaforge.py tests/test_novaforge_gameplay.py tests/test_application.py -q
162 passed, 22 subtests passed

python -m mypy src
Success: no issues found in 444 source files

python -m mypy src --platform win32
Success: no issues found in 444 source files

python scripts/generate_api_reference.py --check
docs\API.md: in sync
```

The focused file covers: model validation (statuses, revisions, confidence range,
description length, progress fractions, reset counts, declared-only dependencies,
profile cycles/self/missing/duplicate rules, rejection-not-crash, duplicate ids),
discovery (profile field, generic self-naming lines, ignored ordinary text, multiple
objectives, unreadable region, empty region), progress (start/fraction, repeats,
resets, unreadable and absent counters), outcomes (counter target, completion text,
failure text, conflict, disappearance, correction, kept verdict, slot supersession,
session change, unverifiable), dependencies (blocked → lifted, missing prerequisite
diagnostic), persistence across a restart (history, events and counters), the service
(dedup, out-of-order, invalid identity, a non-state object, broken interpreter,
publish shape, publish failure, shutdown, bounded history/reset, latency, honesty
limitations), state integration (successful delivery, raising consumer, async
consumer), HTTP (status/profile/reset/served page/declared surface) and the panel's
pure functions executed in Node (including current-only counts, source,
prerequisites and the `progress last read` marker).

## 12. Offline evaluation results

`PYTHONPATH=src python scripts/novaforge_nf17_objective_eval.py` over a **labelled
sequence of NF-16 states** for `nc-forge-mission@1` (expectations written before the
run; results in `data/novaforge_nf17_objective_eval.json`):

| Metric | Result |
| --- | --- |
| Checks passed | **32 / 32** (accuracy 1.0) |
| Discovery precision | **1.0** |
| Discovery recall | **1.0** |
| Progress-state accuracy | **1.0** (8/8) |
| Completion-verification accuracy | **1.0** (13/13) |
| False completions | **0** (rate 0.0 over 4 non-completion guards) |
| Unknown/ambiguous handling | **1.0** (3/3) |
| Service guarantees | **7 / 7** (dedup, single application, one history row, latency measured, no confidence) |
| Interpretation latency | avg 0.30 ms, max 0.72 ms |

The evaluation covers a completion sequence (1→2→3 of 3), an explicit failure, an
ambiguous counter, a disappeared objective, a conflicting completion/failure banner,
an ordinary-text precision guard and a generic self-naming line. Because the fixture
is generated and labelled by this repository, the figures are meaningful **for this
set only** — they are not an accuracy claim about any commercial game.

## 13. Live-test results

**`NOT_TESTED`.** No game was launched, focused, driven or closed; no input was
synthesized; no process was killed; the disconnected WD My Passport 2TB drive was not
touched. Live evaluation is a Scope J safety decision made by a person against a real
target, and this phase sends no input regardless.

## 14. Defects found and fixed during verification

1. **The objective service silently replaced the NF-07 objective registry.**
   `NovaForgeManager` declared `objectives` twice (the gameplay `ObjectiveRegistry` and
   the new `ObjectiveService`); the second declaration won and `self.objectives` was
   overwritten, so `available_kinds()` / `verifier_for()` / `unavailable()` would have
   raised on any gameplay-objective call. Renamed the new service to
   `objective_state`, updated the three routes and the shutdown path. `shutdown()` also
   previously called `ObjectiveRegistry.shutdown()` (which does not exist) and never
   stopped the objective service.
2. **Newly discovered objectives dropped their declared dependencies**, so a
   prerequisite never blocked anything (`_new_objective` omitted the field). Declared
   dependencies are now carried from the first reading; pinned by the blocked → lifted
   test.
3. **An unreadable counter lost its reason.** The objective said "could not be read
   (ambiguous)" without the reading's own explanation; the reading's `reason` is now
   included (Scope E: record *why* a field could not be interpreted).
4. **Two mypy errors in `interpret`** (a non-narrowed `Optional` and a loop variable
   reused with two types) — control flow restructured; both mypy views are clean.
5. **Tests selected built-in profiles by list index.** Adding the mission profile
   repointed two NF-16 tests at a different target; they now select by `profile_id`.
   (Test-robustness defect, not a product defect.)
6. **The mission profile's provenance claimed generated frames** while no evaluation
   script existed. The script now exists, and the provenance states that the labelled
   *states* are generated (NF-17 consumes states, not pixels).
7. **The service wrote counters it never read, and did not persist two of them.**
   `_persist` stored `received/ignored/duplicates/failed`, but `_load` restored only
   history and events; worse, the duplicate and out-of-order branches returned before
   persisting, so after a restart the status reported "0 read" beside a full history.
   Counts are now restored, both branches persist, and a restart test pins the equality.
8. **The panel could present a retained counter as a current measurement.** A counter
   obscured in the latest frame keeps its last-known value (with a reason), but the row
   showed the number with no age; rows now add `progress last read` when the value is
   older than the reading.
9. **The panel omitted required fields and could disagree with itself.** Per-objective
   source, declared prerequisites and the game were missing, and the fact rows counted
   superseded records while the list showed only current ones. All are shown now, with
   a separate `Superseded` fact; pinned by the Node logic tests.
10. **A state Analyze left the objectives card on the previous reading**, so two cards
    could describe different frames. `analyzeGameState` now repaints both; a static
    test pins the refresh.
11. **`observe` could raise on an object that is not a state**, contradicting its own
    "never raises" contract. It now records `invalid_observation` and returns; pinned.

## 15. Known limitations

* **One target, and it is a fixture.** `nc-forge-mission@1` is a declared layout for
  the evaluation scenario; no commercial game has a profile, and its HUD is not read
  by OCR in this phase's evaluation.
* **No OCR-to-objective end-to-end measurement.** The evaluation starts from states, so
  the effect of NF-16 OCR errors on objective text is unmeasured (NF-16 reports its own
  field statuses only).
* **One numeric counter per rule.** Bars, timers, checkpoints, quest stages and
  multi-step missions are not measured.
* **No confidence values.** Nothing measures a calibrated probability, so no number is
  reported; only evidence kinds and reasons.
* **Dependencies are declared only.** No sequence is inferred from screen order, and a
  game with parallel or optional branches is not modelled beyond what a rule states.
* **A missing objective is not a failure.** Only self-naming generic text is read, so a
  game that prints instructions without a label shows "no objective" — honestly.
* **The current picture does not survive a restart** (history, events and counters
  do), so a stale objective can never look current; the panel reports the restored
  history and leaves the current picture untested.
* **A retained progress value is last-known, not live.** When a counter cannot be
  read, the previous value is kept and the panel marks it `progress last read`; the
  figures are never silently refreshed from the reading.
* **No long-term learning or strategy**: no memory across sessions, no planning.
* The panel shows the 8 newest history events and the current objectives; nothing more
  is rendered.

## 16. Remaining defects and risks

* **Risk: profile drift.** A rule names NF-16 HUD element ids; if a perception profile
  is edited without its objective rules, the interpreter reports the missing field
  instead of guessing — safe, but a silent coverage loss for that target.
* **Risk: OCR misreading an objective line** produces a different `objective_id`
  (the id is a digest of source + text), which legitimately looks like a new objective.
  The supersession event records that the text changed, but a human must decide
  whether it was a misread. No cross-read text similarity is used.
* **Risk: counter resets.** A decrease is counted as a reset (or a misread) rather than
  smoothed; a genuinely alternating counter (e.g. a gauge) would accumulate resets.
* **Untested requirement:** "reject results from obsolete *targets*" is satisfied by
  NF-16 refusing a superseded reading, not by a second check in NF-17; that composition
  is asserted in NF-16's tests, not re-asserted here against a live observer.
* **Untested requirement:** the panel's objectives card was verified through the served
  page, the DOM-free logic tests and the Node execution of its pure functions, not
  against a live capture session in a browser.

## 17. Verdict

**PASS WITH LIMITATIONS.**

Implemented and verified: the typed objective model; profile-driven and generic
discovery; progress tracking with start values, fractions, resets and refused numbers;
declared dependencies with blocked/lifted transitions; conservative completion and
failure verification with conflict, disappearance, correction and supersession rules;
bounded persisted history and events; state-service integration that cannot fail a
reading; three documented routes; a panel card that shows objectives, progress,
diagnostics, evidence-backed reasons and history; 61 focused tests; 32/32 offline
checks; both type-check views; and an in-sync API reference.

Not verified, and explicitly not claimed: understanding a commercial game's
objectives, OCR end-to-end accuracy for the mission layout, bars/checkpoints/stages,
calibrated confidence, and any live game evaluation (`NOT_TESTED`). A targeted
follow-up phase should add a second, real-HUD objective profile captured from a
lightweight tested game, plus a labelled OCR-level evaluation set before any claim of
objective understanding beyond the fixture.
