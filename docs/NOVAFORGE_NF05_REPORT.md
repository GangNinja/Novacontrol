# NovaForge NF-05 — Controlled Game Evaluation

**Verdict: PASS WITH LIMITATIONS.**

NF-05 adds a controlled, repeatable, **baseline-first** way to evaluate NovaControl's *actual* gameplay
capabilities against compatible environments — and the honest result of that work in this build is a
negative one: **no environment is controllable and no objective verifier exists**, so every runnable
evaluation is `OBSERVATION_ONLY`, no gameplay input was ever sent, and no report claims a game was played,
won or scored.

NF-06 (Forge Coach) was **not** started. No training, no policy improvement, no game launch, no
multiplayer automation, no anti-cheat bypass.

> **UI correction, 2026-10-20.** The sidebar `Evaluation NF-05` destination this phase added has been
> **removed**: it opened the same `novaforgePanel` as the `NovaForge` entry, so the lab lives only in its
> **Game Evaluation** tab. The tab strip was also made to wrap, because with the sidebar entry gone an
> off-screen tab would have been the only route to it. See
> [NOVAFORGE_VERIFICATION.md](NOVAFORGE_VERIFICATION.md) §8.3.

> Report path: `docs/NOVAFORGE_NF05_REPORT.md`

---

## 1. What was actually evaluated

Real gameplay control was **not** exercised, by design and by capability:

| Environment | Version | Status | Modes | Objective verifier | Launched by NF-05 |
|---|---|---|---|---|---|
| `disposable-window` — Disposable Test Window | 1 | `OBSERVATION_ONLY` | `observation_only` | none (stated reason) | never |
| `offline-game-window` — Operator-Provided Offline Game Window | 1 | `OBSERVATION_ONLY` | `observation_only` | none (stated reason) | never |
| `gameplay-objective` — Gameplay Objective Evaluation | 1 | `UNSUPPORTED` | — | none | never |

The registry's own summary says it plainly: `runnable_count: 2`, `observation_only_count: 2`,
`controllable_count: 0`, and "no entry is controllable or objectively verifiable in this build".

Two real, live observation-only evaluations were run against the operator's own open **Notepad** window
(`start.ps1 - Notepad`, handle `1049688`, pid `32468`) — see §9. No game window was opened, prepared or
driven, because NF-05 does not launch anything and there was no game window already open on this
machine to observe. The environment evaluated is therefore a **disposable window**, never a game, and
that is recorded in every artifact of the run.

## 2. Architecture

NF-05 composes NF-01…NF-04 rather than adding a parallel stack:

```
   registry (games.py)            evaluation record (evaluation.py)        NF-04 (benchmark_lab.py)      NF-01 (manager.py)
 ┌───────────────────────┐      ┌──────────────────────────────┐        ┌───────────────────────┐   ┌────────────────────┐ │ GameRecord            │      │ NovaForgeEvaluationLab       │        │ NovaForgeBenchmarkLab │   │ NovaForgeManager   │
 │  status / modes       │─────▶│  preflight (14 checks)      │───────▶│  one active run       │──▶│ one session slot   │
 │  capabilities         │      │  baseline-first start        │        │  bounded observation  │   │ Guardian + cleanup │
 │  offline declaration  │      │  evaluation record + report  │        │  telemetry/metrics    │   │ timeline (NF-03)   │
 │  objective verifier   │      │  persistence (60 cap)        │        │  persistence (60 cap) │   │ frames + telemetry │
 └───────────────────────┘      └──────────────────────────────┘        └───────────────────────┘   └────────────────────┘
        JSON in API answers            JSON over HTTP (9 routes)              elaborates, does not duplicate
```

* **No second runner.** `NovaForgeEvaluationLab.start()` validates, then calls the NF-04 benchmark lab;
  the evaluation record is a *compatibility and baseline record around one benchmark run*.
* **No second lifecycle.** Benchmark status → evaluation status is one table
  (`completed/failed/aborted/invalid`), and cancellation calls `benchmark.cancel()`, which calls the
  session's own stop path — so input cleanup and the Guardian intervention record still happen.
* **No second evidence store.** The report links to the NF-04 benchmark record, the NF-01 session, the
  NF-03 timeline and the NF-02 telemetry. It stores references and gaps, never frames or a parallel log.
* **No second event bus.** `novaforge.evaluation.started` is published through the injected seam the
  other NovaForge phases already use.

## 3. Compatibility registry (`novaforge/games.py`)

`GameRecord` is a frozen, versioned record with validation (`validate()` returns the reasons a record is
internally inconsistent; the catalog reports them rather than serving a broken entry):

* identity — `game_id`, integer `version ≥ 1`, `display_name`;
* `status` from one vocabulary: `SUPPORTED`, `SUPPORTED_WITH_LIMITATIONS`, `OBSERVATION_ONLY`,
  `UNSUPPORTED`, `NOT_EVALUATED`, each with a mandatory `status_reason`;
* capabilities — `launch`, `window_detection`, `capture`, `input`, `objective_verification` (each
  `{available, state, reason, detail}`), **launch is refused for every entry by validation**;
* requirements — `supported_systems`, `display_requirements`, `required_interfaces`;
* scenario compatibility — `benchmark.scenarios` (resolved against the NF-04 catalog; an unknown or
  unsupported scenario is a validation problem, not a runtime surprise);
* `offline` classification with the evidence basis for the declaration, and
  `confirmation_required` where the operator must assert it;
* `known_limitations`, rendered in the panel and the report.

The rules the validation enforces are the phase's honesty rules:

* a `SUPPORTED*` entry must declare the `control` mode **and** have an available input path **and** an
  available objective verifier — "capture alone is not control";
* an `OBSERVATION_ONLY` entry may not declare `control`;
* a `UNSUPPORTED`/`NOT_EVALUATED` entry may not declare any evaluation mode and must state its reason;
* an unavailable objective verifier must state why.

## 4. Preflight and readiness (`readiness_checks`)

`POST /novaforge/evaluations/preflight` starts nothing and judges **14 checks** against the live window,
each returning `{id, label, status, required, evidence, reason}` with `status ∈ pass | warn | fail |
unavailable`. Measured live for an observation-only run: `compatibility`, `evaluation_mode`,
`target_identity`, `target_uniqueness`, `capture`, `observation_source`,
`objective_verification` (optional, `unavailable`), `input_capability` (optional), `guardian_controls`,
`input_cleanup` (optional), `benchmark_configuration`, `evidence_persistence`, `offline_eligibility`,
`conflicts` — 13 pass / 0 fail / 1 unavailable — with the composed NF-04 benchmark preflight (target
geometry, budget/interval validity, exclusive-slot conflicts) inside `benchmark_configuration` and
`conflicts`, and the target geometry, handle and pid returned alongside the rows.

Two distinctions are load-bearing:

* **A capability that does not exist is `unavailable`, not `failed`.** The objective verifier reads
  `unavailable · not required` for an observation-only evaluation (its reason travels with it), and
  `input_capability` passes as optional with the evidence "observation-only: no input is sent, so none is
  required". Neither is ever silently green or silently blocking for a run that cannot use it.
* **A required check that fails refuses the run.** `ready` is false, the blocking check ids are listed,
  and `start` re-runs the same assessment server-side. There is no override in the UI and none in the
  API; the only way to "get a result" past a failed check is to fix the condition.

## 5. Evaluation lifecycle

`POST /novaforge/evaluations/preflight` → review → **explicit confirmation** (`confirm: true`, and the
UI's own checkbox) → `POST /novaforge/evaluations`:

1. the game, scenario and **configuration** are validated (budget 1–300 s, interval 0.05–5 s, notes
   ≤ 500 chars, scenario's minimum frames — all enforced by the benchmark lab; booleans and fractional
   seconds are refused, not coerced);
2. the pinned target identity is enforced (a caller-supplied `expected_handle`/`expected_pid` wins over
   the freshly measured one, so the window the operator validated is the window that is measured);
3. the baseline is selected — explicit (`baseline_id`) or the **oldest completed valid** evaluation of
   the same game/scenario/version;
4. one active evaluation is allowed (checked under a lock against the evaluation lab, the benchmark lab
   and the session manager) — a second attempt is a **409 conflict** and is also *retained* as an
   `invalid` attempt with the reason;
5. the run proceeds through the NF-04 benchmark lab and ends in exactly one terminal status:
   `completed` (data complete), `failed`, `aborted` (cancelled / deadline / restart), `invalid`
   (refused or insufficient data), or `unsupported` (a registered environment whose compatibility
   forbids running it).

`COMPLETED` is data completeness. The record's `objective` block is always
`{available: false, verified: false, achieved: null, reason: …}`; the report carries
`objective_verified: false` and `real_gameplay_control_exercised: false` as literals, not as computed
optimism.

**Refusals are evidence.** A readiness failure for a *known* environment creates a retained `invalid`
(or `unsupported`) record with its blocking reasons before raising, so the history shows an attempted
evaluation instead of pretending it never happened. Failed, aborted, invalid and unsupported runs are
all retained and all excluded from baselines by the same rule.

## 6. Baseline-first methodology

* The **first** completed, valid evaluation of a game/scenario/version is the baseline (`role:
  baseline`); later evaluations are `candidate`s.
* An explicit baseline must be `completed`, `valid`, and belong to the **same environment and
  scenario/version** — otherwise the start is refused with that reason.
* Comparison is NF-04's own comparison: scenario/version, budget, interval, window title and geometry,
  operating system and mixed configurations must match, or the pair is **refused** with the reasons
  instead of producing a delta.
* The verdict is `not_established` unless a comparison genuinely establishes one; NF-05 invokes no model
  and no policy, so no model can be declared superior.
* A single run per side has no variability and the payload says so.

Baseline records carry: scenario and version, configuration (budget, interval, target title, offline
declaration), model/policy identity (**unavailable, with the reason**), observation and input
capabilities, budgets, verification method (none, with the reason), telemetry references, run status,
errors, evidence links, and sample count. Where the starting state cannot be reproduced (there is no in-
game reset in this build), the baseline is reported as **limited, with that reason** — the registry's
own limitations list travels into every record and report.

## 7. Safety and execution boundaries

* **Nothing is launched.** In-app launch of a game is absent from the registry *and refused by
  validation*; the operator opens the window.
* **One session slot.** An evaluation cannot overlap another evaluation, another benchmark run, or a
  Mission Control session.
* **Guardian is the session's.** Limits, emergency stop and `release_all_inputs` are NF-01/NF-02
  machinery; the preflight checks that the manager actually exposes `stop`/`cancel`/`emergency_stop`
  and input cleanup, and refuses to run when they are missing.
* **No input is sent at all.** `input.available` is false for every runnable entry, and
  `input_capability` is reported as *not required* for the observation-only mode. The Guardian's own
  `input_safety_decision()` boundary remains the only path a future phase may use.
* **Target identity, not title.** The check requires a unique title match, a non-zero handle and a pid,
  and honours the caller's pinned identity; a title now owned by a different window is refused.
* **Cancellation is the session's stop path** — bounded, idempotent (`already_stopped` once terminal),
  and it still records input cleanup and the intervention.
* **Only offline/single-player targets.** `offline-game-window` requires the operator's explicit
  declaration; the classification is reported as *declared, not probed*.
* No online multiplayer, no anti-cheat bypass, no access-control circumvention, no new input capability.

## 8. Evidence, persistence and reporting

`GET /novaforge/evaluations/{id}/report` links, per evaluation: game/version and compatibility status,
scenario and version, evaluation/benchmark/session ids, timestamps and terminal status, target-window
identity, the readiness verdict with counts, the model/policy unavailability, benchmark and telemetry
references, the NF-03 timeline reference, the Guardian/classification data, errors, limitations,
`missing_evidence` (each absent artifact named), the baseline comparison, and the literal
`objective_verified: false` / `real_gameplay_control_exercised: false`.

Persistence is one namespace (`novaforge_evaluations`, cap 60) in the shared `JsonStateStore`, written
on every terminal transition with a **verified read-back** (`persistence.state: verified|failed`); a
stored `running` record is recovered after a restart as `aborted` (never resurrected as active), an
unrecognised status becomes `invalid` with the reason, and a scenario version that is not an integer
becomes `invalid`. No store at all is reported as `not_persisted` rather than assumed.

## 9. API, UI and live verification

Nine authenticated routes in **all five** registration places (`api/app.py`, `api/models.py`'s
`ApiSurface`, `api/route_consumers.py`, regenerated `docs/API.md`, and tests):

```
GET  /novaforge/games                          GET  /novaforge/games/{game_id}
GET  /novaforge/evaluations                    GET  /novaforge/evaluations/baseline
POST /novaforge/evaluations/preflight          POST /novaforge/evaluations
GET  /novaforge/evaluations/{evaluation_id}    POST /novaforge/evaluations/{id}/cancel
GET  /novaforge/evaluations/{id}/report
```

The Forge panel gains an eighth tab, **Game Evaluation**, reached from an enabled sidebar destination
(Forge Coach remains disabled and labelled NF-06): environment registry with compatibility detail and
limitations, scenario/target/budget/interval/notes, the offline declaration when the entry requires it,
baseline selection, the readiness checklist with evidence and failure reasons, Validate → confirm →
Start, Cancel, the live card with an Evidence-Timeline link, history with every retained attempt and its
reason, and the per-game report with the comparison table and JSON export.

**Live verification** (server restarted on `127.0.0.1:8001`, real desktop, real Notepad window):

* registry live: `2 runnable · 2 observation-only · 0 controllable`; `gameplay-objective` renders
  `UNSUPPORTED` with its reason and **zero** scenarios;
* selecting `offline-game-window` surfaces the offline declaration; selecting `disposable-window` hides
  it;
* **Validate Readiness passed** with real evidence — `Target identity: start.ps1 - Notepad · handle
  1049688 · pid 32468`, `Objective verifier: unavailable · not required`, 13 pass / 0 fail / 1
  unavailable;
* **Start was disabled until the confirmation checkbox was ticked** (verified before and after);
* two 3 s evaluations completed against the Notepad window: the first recorded `baseline role: baseline`,
  the second `candidate` with `Compatible with the baseline · Not established` and a measured table
  (fresh frames 4 / 4 / 0; frame yield 0.5 / 0.5 / 0; longest frame gap 908.9 ms / 890.6 ms / −18.3 ms),
  each row stating that a difference is a measurement, not an established one;
* the report read `Objective verified — no`, `Gameplay control exercised — no`;
* **Open Evidence Timeline** switched to the Evidence Timeline tab with the evaluation's session
  selected and its events loaded (`session.created`, `target.validated`, …);
* after a page reload and a server restart the history still listed both evaluations with their
  statuses and reasons — restart persistence observed at the surface the operator uses, not only in a
  unit test;
* the browser console held **no** errors; the screenshots were reviewed, not asserted;
* **no game was launched and no gameplay input was sent** at any point.

Two stale uvicorn processes left by earlier phases were still listening on 8001/8002 serving a route
table from *before* the NF-05 routes existed (the new HTML was visible while `/novaforge/games` was
404 — the old-UI trap in API form). They were stopped and the check was repeated against a fresh
server; the operational note belongs in any handover: **restart the server after adding routes.**

**Live re-verification (this verification pass, `127.0.0.1:8001`).** The trap fired a second time,
and this time it *masked a fix*: port 8001 was still held by the session server from the earlier
pass, which served the pre-fix `games.py`, so the panel showed the old readiness reasons (defect 3 in
§11) as if the fix had never landed. `netstat` named the listener, it was stopped, `Uvicorn running
on http://127.0.0.1:8001` was confirmed for the new process, and every check below was repeated
against it. **Restart the server after any change — and prove which process answered.**

Against the fresh server, with the operator's own open Notepad window as the target:

* registry live: `2 runnable · 2 observation-only · 0 controllable`;
* **readiness passed** with real evidence — `Target identity: start.ps1 - Notepad · handle 1049688 ·
  pid 32468`, `Objective verifier: unavailable · not required`, `Readiness: 13 pass · 0 warn · 0 fail ·
  1 unavailable`, all fourteen rows rendered in order;
* **the refused configuration is now honest live** (budget 0): the panel reads "This evaluation
  cannot start as configured:", `Target identity` says *"The target could not be measured because the
  request was refused first: Duration must be between 1 and 300 seconds."*, `Target uniqueness` reads
  `unavailable · Window uniqueness was not judged: this request did not reach the target window.`, and
  Start stays disabled;
* **Start was disabled until the confirmation checkbox was ticked** (verified before and after);
* a 2 s run ended `invalid · excluded` — "Only 2 fresh frame(s) were recorded; this scenario requires
  at least 4" — which is the honest ending, not a fabricated completion;
* a 6 s run (and a later 5 s run) ended `completed · data complete` with `Mode observation_only`,
  `Baseline role candidate`, `Objective: not evaluated`, and the comparison **refused as a result**:
  `Baseline incompatible — The runs used a different execution budget (3 vs 6); their measurements are
  not on the same basis.` (NF-04's fairness rule firing on live data rather than in a unit test);
* the run card's **Open Evidence Timeline** link switched to the Evidence Timeline tab with that
  session selected and its **15 recorded events** loaded (`session.created`;
  `target.validated: start.ps1 - Notepad`; `session.started`; …), `Source: recorded event log`,
  `Persistence: verified — the snapshot read back with its newest event`;
* the report rendered `Objective verified — no`, the missing-evidence list, the comparison table
  (`No delta: the runs are not compatible`) and enabled JSON export;
* the browser console held **no errors** (only `/system/telemetry` and `/health` polling, all 200);
* after the restart the history still listed the earlier pass's evaluations with their statuses and
  reasons — restart persistence at the surface the operator uses;
* **no game was launched and no gameplay input was sent** at any point.

## 10. Files changed (NF-05)

New:

* `src/novacontrol/novaforge/games.py` — compatibility registry, readiness checks, report helpers.
* `src/novacontrol/novaforge/evaluation.py` — the evaluation lab: preflight, baseline-first start,
  synchronization, cancel/shutdown, persistence, report.
* `tests/test_novaforge_evaluation.py` (52 tests at the end of the verification pass),
  `tests/test_novaforge_evaluation_api.py` (13).
* `scripts/benchmark_novaforge_evaluation.py` — the overhead measurement in §12.
* `docs/NOVAFORGE_NF05_REPORT.md` — this report.

Modified:

* `src/novacontrol/novaforge/__init__.py` — NF-05 exports.
* `src/novacontrol/application.py` — `NovaForgeEvaluationLab` wired beside the benchmark lab (no new
  runner) and shut down on `stop()`.
* `src/novacontrol/api/app.py`, `src/novacontrol/api/models.py`, `src/novacontrol/api/route_consumers.py`,
  `docs/API.md` — the nine routes in every registration place.
* `src/novacontrol/web/static/index.html` — sidebar destination, eighth Forge tab, the panel, roadmap
  card updated (NF-05 implemented; NF-06 still disabled).
* `src/novacontrol/web/static/js/novaforge.js` — the panel logic and its pure predicates
  (`evaluationCanStart`, `evaluationStatusText`, `readinessStateText`, `evaluationBaselineText`,
  `evaluationComparisonText`).
* `src/novacontrol/web/static/nc-os.css` — `[hidden] { display: none !important; }` (see §11).
* `tests/test_novaforge_ui.py` — **17 NF-05 panel tests** (`NovaForgeEvaluationUiTests`: 13 logic,
  static-contract and route pins, driving the shared Node predicate driver; `NovaForgeEvaluationRendererDomTests`:
  4 DOM-harness tests over the real `render-sevaluation` paths) inside a 67-test file.
* `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md`, `README.md` — status, log and table entries.

Nothing in NF-01…NF-04 was reset, reverted, stashed or overwritten; no commit or push was made.

## 11. Defects found during verification, and the fix for each

1. **A required-looking control that was not required.** `.field { display: flex }` silently defeats the
   `hidden` attribute, so the "Offline declaration" checkbox — only meaningful for an entry that
   requires it — stayed on screen for `disposable-window` while the DOM reported `hidden: true`
   (measured live: `hidden=true`, `computed display=flex`). Fixed with one global
   `[hidden] { display: none !important; }` rule (and the now-redundant `.command-palette[hidden]` rule
   deleted). Re-verified live: the declaration row and the closed command palette both compute
   `display: none`, and no test regressed.
2. **Two mypy errors in the new module** (a union-typed `summary` and a missing return annotation on
   `_scenarios_for`). Fixed properly — an annotation and a `list[BenchmarkScenario]` return type — and
   both mypy views are clean; no suppression was added.
3. **A readiness row that blamed a window it never measured.** When a request was refused for another
   reason first (an out-of-range budget, say), the benchmark preflight never enumerated the target, and
   the `target_identity` row said the window "was not uniquely identified with a handle and process id"
   — blaming the window for a measurement that did not happen — while `target_uniqueness` *passed*,
   claiming "one title match required" about a check that never ran. Both rows now report what actually
   happened: `target_identity` names the refusing error ("The target could not be measured because the
   request was refused first: …") and `target_uniqueness` is `unavailable` ("Window uniqueness was not
   judged: this request did not reach the target window"). Pinned by
   `test_a_refused_configuration_does_not_blame_the_window` and
   `test_two_processes_sharing_a_title_are_refused_end_to_end`.
4. **A refused `control` attempt recorded as an `observation_only` one.** The record's `mode` was
   hard-coded, so an API caller who asked for a control evaluation — refused, as it always must be, for
   the missing input path and objective verifier — left a history row and a report saying
   `observation_only`. A retained record must describe the attempt that was made, so it now carries the
   requested mode when that is a mode this build knows (a value that is not a mode is not stored as one;
   the refusal reason already names the caller's mistake). Pinned by
   `test_a_refused_control_attempt_is_recorded_as_a_control_attempt`, and re-checked through the real
   `create_app()` routes by the `test_unsupported_start_is_refused_and_retained` path.
5. **A report-drift correction, not a code fix.** §2 said "preflight (17 checks)" while §4 and §12 said
   14, and the fourteen-check list had no test behind it. The prose now says 14 everywhere, and the set,
   its order and its counts are pinned by `test_the_readiness_check_set_is_the_declared_fourteen` — the
   next drift fails a test instead of surviving in two files.

All five fixes are covered by real checks rather than by a weakened assertion. Two further report claims
that had no test behind them were pinned in this pass: the three persistence states
(`verified` / `failed` / `not_persisted`) in `test_a_persistence_state_is_measured_never_assumed`, and
the retention cap in `test_the_retention_cap_drops_the_oldest_and_says_so`.

## 12. Performance findings (`scripts/benchmark_novaforge_evaluation.py`)

Deterministic 320×200 PNG capture seam, 2 s budget, 0.5 s interval, 14 checks in preflight:

| Figure | Measured |
|---|---|
| Preflight (14-check assessment + benchmark preflight + baseline lookup) | **0.198 ms** |
| Evaluation wall time, 3 runs (start → terminal, 2 s budget) | **2139.95 / 2182.30 (median) / 2204.94 ms** |
| Session overrun against the budget | **50–100 ms** |
| `get` one evaluation | **0.001 ms** |
| `history(limit=20)` | **0.017 ms** |
| `baseline(...)` | **0.053 ms** |
| `report(...)` (with timeline + benchmark evidence) | **0.101 ms** |
| Restart read (`history` on a re-constructed lab) | **0.018 ms** |
| Persisted snapshot, 3 evaluations | **56,903 B** (~19 KB each; ≈1.1 MB at the 60-run cap) |
| Restart recovery | 3 evaluations restored; no `running` record to recover |
| Report truthfulness | `objective_verified: false`, `real_gameplay_control_exercised: false`, 2 named evidence gaps, 12 limitations |

Overhead is not the constraint: the evaluation layer adds **sub-millisecond** work around a run whose
cost is the session's own budget, and adding it never touched the observation loop (the collection and
finalize costs are NF-04's, unchanged). The 50–100 ms overrun is the session's own capture-cadence slack,
not evaluation bookkeeping.

**This is not a claim of real-time gameplay readiness.** The measured workload is a window-cropped
observation loop on a desktop window; nothing here establishes frame-rate competence for a game, and a
single environment on a single machine cannot support a claim of generalized gaming ability.

## 13. Tests and checks (exact results)

Everything below was re-run on the **final tree** (after the last fix and test edit) during the
verification pass; the earlier pass's numbers are kept in §11's history only where a defect needed
retracing.

| Command | Result |
|---|---|
| `pytest tests/test_novaforge_evaluation.py tests/test_novaforge_ui.py -q` | **119 passed / 45 subtests** in 37.40 s |
| `pytest tests/test_novaforge_evaluation_api.py tests/test_novaforge_benchmark_api.py -q` | **28 passed** in 391.45 s (real `create_app()`; 13 + 15) |
| `pytest tests/test_novaforge.py tests/test_novaforge_timeline.py tests/test_novaforge_benchmark.py tests/test_web_platform.py -q` | **217 passed / 441 subtests** in 27.28 s (NF-01…NF-04 regression + platform) |
| `pytest tests/test_novaforge_api.py tests/test_route_shape_contract.py -q` | **49 passed / 90 subtests** in 658.19 s (NF-01…NF-04 HTTP surface + route/consumer contract) |
| `python -m mypy src` / `python -m mypy src --platform win32` | **clean, 424 files each** |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| `node --check` on `app.js` + all 12 `js/*.js` | all pass |
| Live server on `127.0.0.1:8001` (fresh process) | §9: registry, readiness, refusal, two runs, report, restart history |

Added in this pass: 4 tests (`test_a_refused_control_attempt_is_recorded_as_a_control_attempt`,
`test_the_readiness_check_set_is_the_declared_fourteen`,
`test_the_retention_cap_drops_the_oldest_and_says_so`,
`test_a_persistence_state_is_measured_never_assumed`).

**NovaForge total: 350 tests** — 74 (`test_novaforge.py`) + 42 (timeline) + 56 (benchmark) + 67 (UI) +
52 (evaluation) + 31 + 15 + 13 (HTTP). Skips: none in any file above; the Node-driven UI tests are
skipped cleanly only where `node` is absent (Node is present here, and all of them ran).

**Not run:** the full repository suite (not requested, and `tests/test_web_api.py` alone exceeds the
600 s tool limit in one invocation). Nothing was skipped or deselected within the NF-05 scope. The
NF-05 HTTP files are slow for a reason that has nothing to do with NF-05: each `create_app()` probes a
local Ollama port that nothing is listening on, and on this host a refused connect costs ~1.5 s per
probe (~14 s per construction, measured with `cProfile`), so 28 HTTP tests take ~6.5 min. Their
correctness is unaffected; the observation is recorded here so it is not mistaken for a hang.

## 14. Failing / skipped / unverified requirements

* **Real gameplay input — NOT EXERCISED.** No input path exists in this build; every runnable evaluation
  is observation-only by validation. This is the phase's central limitation, not an oversight.
* **Objective verification — UNAVAILABLE.** No objective verifier exists or was implemented, so no
  evaluation reports an achieved objective. `gameplay-objective` is a catalog record stating exactly
  that.
* **Game launching — NOT IMPLEMENTED** (out of scope by instruction and refused by validation).
* **No game was evaluated.** No compatible game window was already open on this machine, and NF-05 may
  not open one; the live runs observed a disposable Notepad window, which every artifact says plainly.
* **One run at a time**, retention 60 evaluations, capture is primary-display and window-cropped, p95
  needs 20 latency samples (NF-04's own rules), a bench host with no `node` would skip the panel tests,
  and the full repository suite was not run in one pass.
* **A short run can end `invalid` for too few fresh frames** (observed live: a 2 s run at a 0.5 s
  interval recorded 2 frames against a 4-frame minimum). That is NF-04's scenario requirement doing its
  job — the record says so and is excluded from aggregates — but it means a 2 s budget is not
  sufficient for `observation-baseline` on a live desktop; use the scenario's own default (10 s) unless
  the operator is deliberately testing the exclusion path.
* **The performance figures in §12 are the earlier pass's measurements and were not re-measured in this
  pass** (no capture seam was instrumented this time); they are kept because nothing on the measured path
  changed. The panel's DOM behaviour is covered by the Node-driven tests rather than by screenshot
  comparison, and no screenshot was used as evidence for a numeric claim.
* **Two report claims from the previous pass had no test behind them** (the three persistence states and
  the retention cap); both now do. They were implemented correctly, so this is a coverage gap that was
  closed rather than a defect.

## 15. Known limitations

1. Capability, not ambition: `controllable_count` is 0 and will stay 0 until a decision pipeline, a
   Guardian-enforced gameplay input path and an objective verifier exist — the three prerequisites this
   report refuses to fake.
2. Compatibility claims are registry declarations validated for internal consistency, not measured
   against a real game; the only *measured* claim is the observation of a window.
3. The offline/single-player classification is operator-declared and explicitly not probed.
4. Comparison inherits NF-04's fairness rules and can therefore refuse a pair for a configuration
   difference; that refusal is a result, not an error.
5. Baselines are per game/scenario/version; a baseline recorded on a different configuration, geometry
   or operating system is reported as incompatible rather than pooled.
6. NF-06 remains disabled; no training or policy improvement was added, and the "improvement" half of the
   loop is deliberately unbuilt.

## 16. Confirmation

* NF-05 implemented and verified; **NF-06 not started**.
* No game launched, no gameplay input sent, no objective claimed.
* No second session manager, benchmark system, event bus, telemetry layer, evidence store or input
  subsystem was created.
* All NF-01…NF-04 changes preserved; nothing was reset, cleaned, stashed, reverted, committed or pushed.
