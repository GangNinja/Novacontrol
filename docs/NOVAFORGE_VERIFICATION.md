# NovaForge NF-01 … NF-06 — Independent Milestone Verification

**Date:** 2026-10-19
**Tree:** `main` at `ec03e87`, working tree **dirty** (six phase reports, the phase code and earlier audits'
fixes are uncommitted, see §0 and §12).
**Host:** Windows 11, Python 3.13.0 (`.venv`), Node v24.19.0, no local model runtime.
**Verdict:** **PASS WITH LIMITATIONS for NF-01 … NF-06.** Every capability the six phase reports claim is
implemented and evidenced below; the capabilities that do not exist (real gameplay input, an objective
verifier, a game launch, model-backed coaching) are reported **UNAVAILABLE**, never approximated. One
integration-level defect was found, reproduced, fixed and covered by new regression tests (§8).

**Gameplay go/no-go: NO-GO** — see §11. Observation, measurement, evidence and experiment machinery is
ready; *playing* a game is not possible in this build and nothing here may claim it.

> This is the cross-phase audit. The six phase reports (`docs/NOVAFORGE_NF0x_REPORT.md`) remain the detailed
> records; this document checks their claims against the tree, the API, the UI and a live run.

---

## 0. Baseline recorded before any change

| Item | Recorded value |
|---|---|
| Branch | `main` |
| HEAD | `ec03e87` (14 commits, first 2026-09-05) |
| Working tree | **dirty** — 24 modified tracked files (NovaForge source is committed; the phase reports, `docs/STATUS.md` additions, earlier-audit fixes and this audit's two source edits are not) plus 10 untracked docs |
| Environment | Windows 11; Python 3.13.0; Node v24.19.0; Playwright absent; no Ollama listening |
| Running processes at audit start | stale servers on `127.0.0.1:8001`, `:8017`, `:8123` from earlier sessions; stale suite runners `run_suite.sh` (18 Oct) and `run2.sh` (05:37 19 Oct) still looping |
| Baseline tree state | `git status --porcelain` captured; **nothing was reset, stashed, cleaned, committed or pushed** during this audit |

The stale servers were discovered to answer with a pre-fix build (the first "fresh" 8001 process had failed to
bind behind one), and the stale runners were concurrently executing the same test files. Both hazards are
recorded in §1.2; the live pass (§7) and the final suite numbers were produced **after** killing them.

---

## 1. What was executed

All commands ran from the repository root with `PYTHONPATH=src` using `.venv/Scripts/python.exe`.

| # | Check | Exact command | Result |
|---|---|---|---|
| 1 | Whole test suite, file by file (one 600 s tool ceiling per call) | `python -m pytest tests/<file> -q -p no:cacheprovider` for all **126** files | **Sweep A (before the new regression tests): 126 files, 4,527 passed, 3,488 subtests, 0 failed, 12 skipped**; every file's final single-process run exited 0 |
| 2 | New regression tests | `pytest tests/test_evaluation.py::ConcurrentStoreWriteTests tests/test_audit.py::ConcurrentAuditWriteTests -q` | **4 passed in 15.8 s** |
| 3 | NovaForge suites | `pytest tests/test_novaforge*.py -q` | **425 passed / 183 subtests** in 10 files |
| 4 | NovaForge collection | `pytest <10 novaforge files> --collect-only -q` | **425 collected in 16.8 s** |
| 5 | Types, both CI views | `python -m mypy src` / `python -m mypy src --platform win32` | **no issues, 426 files** each |
| 6 | API reference sync | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| 7 | Front-end syntax | `node --check` on `app.js` + 12 `js/*.js` | all pass |
| 8 | Live application (fresh process on 8001, `NOVACONTROL_DISABLE_OLLAMA=1`) | `/tmp/nf_audit/live_probe.py` — 25 HTTP steps | **25/25 ok**, exit 0, no exception in the server log |
| 9 | Live browser panel (same fresh server) | Forge tab → Forge Coach → `ANALYSE EVIDENCE` | DOM shows `ANALYSED · 4 FINDING(S)`, 3 recommendations; **zero console messages**, all requests 200 |
| 10 | Route-contract timing (the two tests an earlier report implied do not finish) | each test run individually | **46.1 s** and **28.0 s**, both pass; whole file **18 passed / 314 s** |

Durations are dominated by full-application boot (every `create_app()` probes the absent Ollama port):
`test_web_api.py` 1,026 s, `test_application.py` 526 s, `test_embodied.py` 482 s, `test_novaforge_api.py`
395 s. No number here is estimated.

### 1.1 Skips (12, pre-existing and honest)

`browser_search_extraction` (2) plus five Playwright/desktop E2E modules skipped at module scope:
`browser_token_reuse_e2e` 1, `guided_click_e2e` 2, `reduced_motion_e2e` 2, `tab_focus_walk_e2e` 2,
`telemetry_e2e` 3. Playwright is not installed; nothing was deselected with `-k`/`--deselect`.

### 1.2 Measurement hazards found and corrected

* **Concurrent suites.** The two stale runners were executing the same files while this audit ran. On
  Windows two processes writing the evaluation store collided:
  `ModuleLifecycleError: Module 'evaluation' failed during start: [WinError 32] … 'evaluations.jsonl.tmp' -> 'evaluations.jsonl'`.
  That is a real defect (§8.1), not a test artefact. After killing the runners every affected file was
  re-run single-process and passed: `test_vision_manager.py` **72 passed / 14 subtests** (the contaminated
  run had 2 failures), `test_web_api.py` **75 passed / 72 subtests**, `test_web_platform.py` **45 passed /
  356 subtests**, `test_evaluation.py` **108 passed / 6 subtests**.
* **A stale server answered the port.** The first 8001 process for this audit failed with
  `[Errno 10048]`; the old 18 Oct server was listening. All stale servers were killed and the entire live
  pass was repeated against a genuinely fresh process (uvicorn pid 33800).
* **Inflated durations** from the stale runners (`test_preference.py` 60,833 s, `test_training.py`
  41,397 s across host sleep) are **not** used anywhere in this report.

---

## 2. Requirement verification

Every row names the source report section, the implementation that answers it, the evidence, and the
verdict. "Live" means the fresh-server pass in §7; test names are in `tests/`.

### NF-01 — Gameplay Workspace & Session Foundation (source: NF-01 report §1–§5)

| Requirement | Source | Implementation | Verdict | Evidence |
|---|---|---|---|---|
| Mission fields validated, blanks refused, no silent defaults | §1 | `novaforge/models.py` `MissionConfig` | PASS | `MissionValidationTests`; live `POST /novaforge/mission` `valid: true` |
| Target identity pinned (title, handle, pid, bounds, DPI) | §1 | `models.py` `TargetWindow`; `manager.start` (`manager.py:714`) | PASS | `SessionLifecycleTests`; live pin handle 20515186 / pid 14568 |
| Nine states, one legal-move table; illegal move **raises** | §1 | `models.py` `SESSION_TRANSITIONS` / `SessionRecord.transition` | PASS | `StateMachineTests` |
| `COMPLETED` unreachable without a verifier; time limit ends `STOPPED` + "NOT verified" | §1 | `manager._finalize` (`manager.py:1934`) | PASS | `StateMachineTests`, `SessionLifecycleTests`; live stop `verification.supported: false` |
| Action budget stored, reported **not enforced** | §1 | `models.py`; `guardian.py` limits | PASS | `GuardianSafetyTests`; live `/novaforge/status` limitations |
| One observation task; only `fresh` frames recorded; window followed; pause on loss/change | §2 | `manager._tick` (`manager.py:1709`) | PASS | `SessionLifecycleTests`; live 6 fresh frames at ~300 ms |
| Persistence (40 sessions), boot reconciliation closes a stale `RUNNING` as `STOPPED` | §3 | `manager` restore path; `JsonStateStore` | PASS | `SessionLifecycleTests`; 16 restored records live |
| Start asks the shared `PermissionManager`; unusable gate = refusal | §3 | `manager.start` | PASS | `FakePermissions` tests; live start through `app.risk` |
| Frame path confined to frame root; served `image/png` with never-cache | §3 | `manager.frame_path`; `api/app.py` | PASS | `FrameToolingTests`; live 200 `image/png`, no-store trio |
| 12 routes registered in all five places | §4 | `api/app.py`, `api/models.py`, `api/route_consumers.py`, `docs/API.md` | PASS | `NovaForgeApiTests`; `generate_api_reference.py --check` in sync |
| Panel: Mission / Live / History; stages labelled `Not available in NF-01` | §5 | `web/static/js/novaforge.js` | PASS | panel contract + DOM tests; live DOM snapshot |
| Five sidebar entries disabled for future phases | §5 | `index.html` | PASS (superseded) | true at NF-01; NF-03…NF-06 later enabled them by design |

### NF-02 — Forge Telemetry & Guardian (source: NF-02 report §1)

| Requirement | Source | Implementation | Verdict | Evidence |
|---|---|---|---|---|
| One snapshot per session, measured where each fact occurs | §1 | `novaforge/telemetry.py:97` `TelemetrySnapshot` | PASS | `TelemetryAggregationTests`; live `frames 7`, median 328.224 ms |
| p95 only at ≥20 samples; otherwise `null` **with reason** | §1 | `telemetry.py` latency window | PASS | `TelemetryAggregationTests`; live `p95_ms: null`, reason names 7 samples |
| Actions `null` + reason, never zero | §1 | `telemetry.py` action block | PASS | `TelemetryAggregationTests`; live `/novaforge/status` |
| Resources `available: false` per subsystem with reason | §1 | `telemetry.py` resources | PASS | `TelemetryAggregationTests` |
| Guardian payload: safety, target state, limits, controls, cleanup, guarantees | §1 | `guardian.py` payload (:240) | PASS | `GuardianSafetyTests`; live `GET …/telemetry` |
| `input_safety_decision` refuses; `INPUT_PATH_AVAILABLE = False`; validation persisted | §1 | `guardian.py:30`, `:74` | PASS | `GuardianSafetyTests` |
| Cancel / emergency stop bounded and idempotent; input released once | §1 | `manager._abort`/`_finalize` | PASS | `GuardianSafetyTests` |
| Session cap bounds disk; eviction removes frames | §1 | `manager` eviction | PASS | `TelemetryAggregationTests` |
| 3 routes authenticated and registered | §1 | `api/app.py`, `api/models.py`, `api/route_consumers.py` | PASS | `NovaForgeApiTests`; API sync |
| Telemetry + Guardian tabs; `0` is a value, `null` is `unavailable` | §1 | `novaforge.js` `metricText` | PASS | `NovaForgePanelContractTests`, DOM tests; live tabs |

### NF-03 — Evidence Timeline & Replay (source: NF-03 report §1–§4)

| Requirement | Source | Implementation | Verdict | Evidence |
|---|---|---|---|---|
| Typed events validated against one vocabulary; unknown refused | §2 | `novaforge/timeline.py` `EVENT_SPECS` | PASS | `TimelineModelTests` |
| Ordering by `seq`; monotonic `elapsed_ms`; concurrent appends unique/contiguous | §3 | `timeline.py:368` `TimelineLog.append` | PASS | `TimelineModelTests` |
| Duplicate `event_id` idempotent | §3 | `timeline.py` ingestion | PASS | `TimelineModelTests` |
| Bounded at 240 events with dropped count | §1 | `timeline.py` retention | PASS | `TimelineModelTests` |
| Persistence verified by read-back; a dropped write is `failed` + event | §4 | `manager` finalize; `timeline` | PASS | `DroppingStore` tests; live session finalize |
| Evidence refs bounded and resolved at read time; missing frame `available: false` | §2 | `timeline.py` refs | PASS | `TimelineModelTests` |
| Replay GET-only, `can_*: false` | §1 | `api/app.py` timeline routes | PASS | `ReplaySafetyTests` |
| Classification categories cite their events; no success claimed | §1 | `timeline.py` classification | PASS | `TimelineModelTests` |
| Comparison 17 rows; unmeasured = `None` + reason | §1 | `timeline.compare_sessions` | PASS | `TimelineManagerTests`; UI comparison test |
| 3 routes + Evidence tab | §1 | `api/app.py`, `novaforge.js` | PASS | `NovaForgeTimelineApiTests`; live timeline 20 events, `session.created` → `telemetry.snapshot` |

### NF-04 — Benchmark Lab (source: NF-04 report §1–§4)

| Requirement | Source | Implementation | Verdict | Evidence |
|---|---|---|---|---|
| Versioned scenario catalog; one explicitly unsupported `gameplay-objective` | §2 | `novaforge/benchmark.py` scenarios | PASS | `ScenarioCatalogTests`; live catalog 3 supported / 1 unsupported |
| 25 typed metrics; every unavailable row carries a reason | §3 | `benchmark_lab.collect_metrics` (`:982`) | PASS | `MetricCollectionTests`; live `frames 12`, `frame_yield 0.571` |
| Config validation refuses booleans/fractions/out-of-range (422) | §1 | `benchmark.py` `BenchmarkConfig` | PASS | `ScenarioConfigValidationTests` |
| Preflight validates scenario + config + live window and starts nothing | §1 | `benchmark_lab.preflight` | PASS | `BenchmarkCatalogApiTests`; live valid, no run created |
| One active run, bounded; cancel uses the session stop path | §1 | `benchmark_lab.start` (`:408`) | PASS | `BenchmarkRunnerTests`, `BenchmarkRunApiTests` |
| Status from where the session ended; `valid` only for `completed` | §4 | `benchmark_lab` result table | PASS | `BenchmarkRunnerTests`; live `completed`, `valid: true` |
| Aggregation eligible-only and same-basis-only | §1 | `aggregate_runs` (`:1275`) | PASS | `AggregateTests` |
| Comparison refuses incompatible pairs; never names a winner | §1 | `compare_runs` (`:1619`) | PASS | `FairComparisonTests`; UI shows `not_comparable` |
| Persistence read back; dead `running` recovers as `aborted` | §1 | `benchmark_lab` persistence | PASS | `BenchmarkRunnerTests`; live `persistence: verified` |
| Events on the app bus | §1 | `novaforge.benchmark.*` publish seam | PASS | `BenchmarkEventTests` |
| 9 routes + Benchmark Lab tab | §1 | `api/*`, `novaforge.js` | PASS | `BenchmarkCatalogApiTests`; live start/finish; UI tab |

### NF-05 — Controlled Game Evaluation (source: NF-05 report §1–§3)

| Requirement | Source | Implementation | Verdict | Evidence |
|---|---|---|---|---|
| Compatibility registry validates itself; **launch refused for every entry** | §3 | `novaforge/games.py` `GameRecord` | PASS | `GameRegistryTests`; live `runnable_count 2`, `controllable_count 0` |
| Readiness preflight runs 14 named checks and starts nothing | §2 | `evaluation.preflight` (`:355`) | PASS | `ReadinessTests`; live `ready: true`, 14 checks, `blocking: []` |
| Baseline-first: oldest compatible completed evaluation is the baseline | §2 | `evaluation.baseline` (`:554`) | PASS | `ReadinessTests`; live `baseline.available: true`, `role: candidate` |
| Evaluation wraps NF-04; no second runner/lifecycle/store/bus | §2 | `evaluation.start` (`:605`) → `benchmark_lab.start` | PASS | `EvaluationLifecycleTests`; live evaluation created benchmark `4a81c3c4…` |
| Status mapping + cancel through the benchmark/session stop path | §2 | `evaluation` status table | PASS | `EvaluationLifecycleTests` |
| Report carries evidence refs, gaps and limitations | §4 | `evaluation` report builder | PASS | live report, 12 limitations |
| Persistence bounded at 60 | §4 | `evaluation` retention | PASS | `EvaluationAuditTests` |
| 13 routes + Game Evaluation tab | §5 | `api/*`, `novaforge.js` | PASS | `EvaluationRegistryApiTests`; UI tab |
| Real gameplay input / objective verification | §1 | **absent** | **UNAVAILABLE** | live `objective: {available: false, verified: false, achieved: null}` |
| Launching a game | §1 | refused by validation | PASS (refusal) | live preflight; registry validation |

### NF-06 — Forge Coach & Controlled Experiments (source: NF-06 report §1–§4)

| Requirement | Source | Implementation | Verdict | Evidence |
|---|---|---|---|---|
| Read-only, recomputed analysis; never cached | §1 | `coach.analyse` (`coach.py:882`) | PASS | `CoachAnalysisTests`; live `read_only: true`, record unchanged |
| Findings always cite evidence; severity/strength basis stated | §3 | `coach.py` `Finding.validate` | PASS | live 4 findings, all cited; kinds `derived`/`observed` |
| Earliest error is a candidate; `causation_claimed` always false | §3 | `coach.py` earliest analysis | PASS | live `causation_claimed: false` |
| Recommendations name metric + criterion; unmeasurable refused | §4 | `RECOMMENDATION_RULES` (`:357`), 14 rows | PASS | `CoachRecommendationStoreTests`; live 3 recommendations |
| Experiments: `repeat` or single-variable `configuration_change`; blocked kept | §1 | `experiments.propose` (`:594`), `validate_spec` (`:870`) | PASS | `ExperimentProposalTests` |
| Approval bound to exact spec digest; run revalidates | §1 | `ExperimentSpec.digest` (`:189`), `approve` (`:1008`), `run` (`:1069`) | PASS | live: run 422 before approval; approval `spec_digest` = proposal digest `0fd9752ca85c8556` |
| Verdict improved/regressed/inconclusive/not_comparable; no gameplay claim | §1 | `_verdict` (`:1871`) | PASS | live `inconclusive`, `improvement_claimed: false`; UI `not_comparable` pair |
| One experiment at a time; destructive/unbounded refused | §1 | `experiments` guard + permission | PASS | `ExperimentExecutionTests` |
| Insufficient evidence is an answer | §1 | `coach` empty subject | PASS | `CoachAnalysisTests` |
| 9 routes + panel decisions | §1 | `api/*`, `novaforge.js` | PASS | `CoachApiTests`, `ExperimentApiTests`; live UI analysis |
| Model-backed explanation | §1 | **absent** | **UNAVAILABLE** | no model invoked |

---

## 3. Cross-phase data flow — traced, not assumed

One live run, followed end to end (identifiers from the fresh pass):

```
session 007bab6f4e484f7fb359235cd67c6da2
  └─ telemetry  frames 7 · median 328.224 ms · p95 null (7 samples)
  └─ Guardian   input_path_available false · target validated · budget not enforceable
  └─ timeline   20 events, seq 1 session.created → telemetry.snapshot
        └─ benchmark 4a81c3c4b5fa44749eeed24b6abb4ccd  completed · frames 12 · persistence verified
              └─ evaluation e04d07d47a7f4161a08a63ffc6455224  completed · observation_only · baseline d293ba25… (17 Oct)
                    └─ Coach 4 findings / 3 recommendations (read-only)
                          └─ experiment b31ede75b1f6  digest 0fd9752ca85c8556
                                approval → same digest → run → verdict "inconclusive"
```

| Link | Implementation | Checked |
|---|---|---|
| Session → telemetry | `manager._tick` writes `TelemetrySnapshot` in place | same session id on `/telemetry`; frame count matches the frame list |
| Session → Guardian | `guardian.payload(record)` built from the authoritative record | live `input_path_available: false`, target validated, cleanup present |
| Session → timeline | `TimelineLog.append` with `seq`, evidence refs, monotonic elapsed | 20 contiguous events; frame refs resolve; 240-event bound unit-tested |
| Timeline/benchmark → evaluation evidence | benchmark run id and session id stored on the evaluation record | live evaluation cites its own benchmark run and the baseline's run id |
| Evaluation → Coach | `Coach` reads session + telemetry + timeline + benchmark + evaluation by id | analysis subject carried the pinned window; findings cite events and metrics |
| Coach → experiment | proposal carries the subject ids + spec digest | live proposal digest reused by the approval |
| Persistence/restart | `JsonStateStore`; missing/edited records reported, never repaired silently | suite covers restart reconciliation (session → STOPPED) and stale `running` runs (benchmark → aborted) |
| Retention | 40 sessions · 60 benchmarks · 60 evaluations · 60 experiments · 240 events | unit-tested; not exercised to the cap live (§10) |
| Cancellation | one stop path: evaluation → benchmark → session → Guardian cleanup | `EvaluationLifecycleTests`; live stop shows `input_cleanup.ok` |
| Terminal states | session `STOPPED/FAILED`, benchmark/evaluation `completed/failed/aborted/invalid`, experiment `completed/failed/aborted/invalid` | live run reached each terminal state honestly once |

## 4. Gameplay readiness — audited independently

No game was launched and no gameplay input was sent. The five prerequisites a gameplay claim needs were
each checked in the code and on the live surface:

| Prerequisite | State | Evidence |
|---|---|---|
| Real scene interpretation | **ABSENT** | NovaForge records pixels; no per-frame semantics, no vision model wired into a session. The vision processor exists elsewhere in the app but is not on this path. |
| Decision / action proposal | **ABSENT** | No planner or policy is invoked; the registry's `controllable_count` is **0** and NF-05/NF-06 say so. |
| Target-validated input | **ABSENT** | `guardian.INPUT_PATH_AVAILABLE = False`; `input_safety_decision` refuses for a pinned-but-unvalidated target and for "no input path". |
| Enforced safety limits | **PARTIAL** | Target validation, emergency stop, bounded cancel and input cleanup are enforced; the action budget is stored but `configured_not_enforceable`, because no action path exists. |
| Independent objective verification | **ABSENT** | No verifier exists: every live record reports `objective.available: false`, `verified: false`, `achieved: null`. |

**Conclusion:** desktop capture, green tests and observation-only evaluations are **not** gameplay
capability. Any statement that NovaForge "plays", "wins" or "achieves objectives" would be false in this
build.

## 5. Experiment comparability, approval binding and verdicts

| Rule | Implementation | Evidence |
|---|---|---|
| Approval is bound to the current spec digest and revalidated at run time | `ExperimentSpec.digest` (`experiments.py:189`), `approve` (`:1008`, "bind an approval to the CURRENT spec digest"), `run` (`:1069`) | live: `POST …/run` **422** before approval; after approval the stored `spec_digest` equals the proposal's; the run then executed |
| A change to target, metric, repeats or a value requires a new approval | `validate_spec` (`:870`), approval comparison in `run` | `ExperimentApprovalTests`, `ExperimentExecutionTests` |
| Comparison refuses incompatible bases (scenario/version/budget/interval/window/OS) | `benchmark_lab.compare_runs` (`:1619`) | `FairComparisonTests`; UI history shows `not_comparable` for a cross-basis pair |
| A verdict needs ≥2 eligible runs per arm, a compatible comparison, a delta beyond the threshold and no guardrail breach | `_verdict` (`experiments.py:1871`), `assess` (`:1522`) | live single-run arms → `inconclusive` with the reason, `improvement_claimed: false` |
| A completed experiment is "data complete", never "proven improvement" | verdict payload + report limitations | live verdict fields; NF-06 report §1 |

## 6. Cross-phase integration

| Integration point | Verdict | Evidence |
|---|---|---|
| One manager, one benchmark lab, one evaluation lab, one coach, one experiment lab built in `application.py` sharing the app permission layer, state store and event bus | PASS | `application.py:1742–1815`; live objects answered one app instance |
| No second engine: NF-05 calls NF-04, NF-06 calls NF-05 | PASS | import graph (`coach.py` imports only `json_store`); live evaluation created its own benchmark run |
| Route registry agreement | PASS | **43** `/novaforge…` patterns in `ApiSurface`, **52** consumer declarations, 48 lines in `docs/API.md`, `--check` in sync |
| Authentication on every NovaForge route | PASS | auth tests in the API suites |
| One sidebar destination, nine in-panel tabs, content-hashed JS | PASS | live DOM: one `data-panel="novaforgePanel"` entry, nine `.forge-tab` buttons on two wrapped rows, `?v=` injected, no console errors (§8.3) |
| Existing surfaces unaffected | PASS | whole suite green (§1); `test_web_api`, `test_web_platform`, `test_route_shape_contract` pass |

## 7. Live user-level workflow (fresh process, final tree)

25/25 steps passed against a real WinForms window titled `NC FORGE AUDIT` (a disposable OS window, **not a
game**; no input sent):

1–3. health ok · `/` 200 with the never-cache trio, `novaforgePanel` and `?v=` injected · windows probe lists
8 live windows including the audit window.
4–6. mission saved · validated against the live target (handle 20515186, pid 14568) · session started `RUNNING`.
7–9. 6 fresh 2880×1800 frames (~300 ms each) · telemetry `frames 7`, median 328 ms, p95 `null` with reason ·
   frame served `image/png` with no-store.
10–12. pause/resume · stop with `verification.unverified` · timeline 20 events.
13–15. benchmark preflight valid · run completed, `valid: true`, frames 12, yield 0.571, `persistence: verified`.
16–19. evaluation preflight ready (14 checks) · started `observation_only` · completed with
   `objective.verified: false` and a baseline cited · report with 12 limitations.
20. Coach analysis: read-only, 4 cited findings, 3 recommendations, `causation_claimed: false`.
21–25. experiment proposed with digest · run refused 422 · approved by the same digest · ran · results
   `inconclusive` with the reason, `improvement_claimed: false`.

Panel: the nine Forge tabs rendered, the persisted mission and live target list were populated, and
`ANALYSE EVIDENCE` produced `ANALYSED · 4 FINDING(S)` / 3 recommendations. **Browser verification**, not a
DOM shim; the Node-driven renderer tests are separate and are not offered as evidence of the running page.

## 8. Defects found and fixed

### 8.1 Evaluation/audit stores staged writes under a fixed temp name (fixed, now regression-tested)

`evaluation/storage.py` and `audit/logger.py` wrote `<name>.jsonl.tmp` before `os.replace`. Two writers of
the same store share that stage: the second truncates/races the first and, on Windows, `os.replace` raises
`WinError 32` while the handle is open — reproduced in this audit as the `ModuleLifecycleError` in §1.2 that
aborted the `evaluation` module start. `persistence/json_store.py` had already solved this with a uniquely
named staged file; both stores now use `tempfile.mkstemp(dir=…, prefix=…, suffix=".tmp")` and unlink the
staged file if the replace fails.

**Regression tests added** (`ConcurrentStoreWriteTests` in `tests/test_evaluation.py`,
`ConcurrentAuditWriteTests` in `tests/test_audit.py`):
* a rewrite never touches another writer's staged file (the held legacy stage keeps its bytes and the new
  content lands in the real store), and
* a failed replace leaves no staged file behind.

**Mutation-tested:** with the staged name forced back to the shared `<name>.tmp`, both tests' scenario
reproduces `PermissionError [WinError 32]` for both stores; with the fix, **4 passed**. Re-ran
`test_evaluation.py`, `test_audit.py`, `test_persistence.py`, `test_vision_manager.py` — all green — and both
mypy views are clean.

### 8.2 The NF-06 report's unmeasured "slow, not stuck" claim (documentation, fixed)

§13a/§14 implied the route-contract tests do not finish. Both pass individually — **46.1 s** and **28.0 s**
(this pass; 34.2 s / 27.0 s earlier) — because each boots the full app and probes the absent Ollama port;
the whole file is **18 passed in 314 s**. The report now states the measurement.

### 8.3 Five sidebar entries opened one panel, and three tabs were off-screen (fixed, now regression-tested)

Reported by the operator against the running page: `Evidence NF-03`, `Benchmarks NF-04`, `Evaluation NF-05` and
`Forge Coach NF-06` sat in the sidebar beside `NovaForge`, yet all five buttons opened the same panel. Each of
the four extras carried a `data-forge-*` hook whose only job was `switchTab(...)` — four sidebar destinations
for one surface, and four handlers querying markup that the phase tabs already provide.

Fixed at the source, not by hiding:

* `index.html` keeps **one** `data-panel="novaforgePanel"` entry; the four extra buttons and their
  `data-forge-timeline/-benchmark/-evaluation/-coach` attributes are deleted.
* `js/novaforge.js` lost the four dead `querySelector("[data-forge-*]")` click bindings and `nc-os.css` lost
  the now-unused `.nav-item.planned` rules.
* `nc-os.css` `.forge-tabs` now sets `flex-wrap: wrap` and `width: 100%` instead of `overflow-x: auto` +
  `width: fit-content`. Once the phase tabs became the *only* way into four phases, a sideways-scrolling
  strip hid three of them behind no scrollbar affordance — measured live before the fix: `Benchmark Lab`,
  `Game Evaluation`, `Evidence Timeline` and `Forge Coach` all crossed the strip's right edge.

**Regression tests** (`tests/test_novaforge_ui.py`): `test_planned_destinations_are_disabled_and_labelled`
now requires exactly one NovaForge sidebar destination and the **absence** of all four `data-forge-*=`
attributes; the new `test_every_phase_tab_is_reachable_without_horizontal_scrolling` pins the wrapping strip
(no `overflow-x`, no `fit-content`) and the nine `data-forge-tab=` destinations. Suite: **87 passed, 49
subtests passed in 0.90 s**.

**Live re-verification** (fresh server, 8001, content-hashed reload): sidebar nav items
`[Command, Chat, Explore, Routing, J.A.R.V.I.S, Vision, NovaForge, Build, Learn, System, Settings, CLI]` —
one NovaForge entry; `.forge-tabs` `flex-wrap: wrap`, `scrollWidth == clientWidth == 935`, zero tabs past the
strip edge, two rows, nine tabs; each tab click activated its view
(`active:forgeTimelineView`, `active:forgeBenchmarkView`, `active:forgeEvaluationView`, `active:forgeCoachView`).
Screenshot: `screenshots/novaforge-tabs-wrapped.png`.

### 8.4 Stale runners and a stale server from earlier sessions (process hygiene, corrected)

Two old suite runners had been executing the same files for hours and inflated timings; a stale server was
answering 8001. Both were stopped; affected files were re-run once, single-process, and the live pass was
repeated on a fresh build. No inflated figure is used in this report.

## 9. Corrections to phase-report claims

| Claim | Correction |
|---|---|
| NF-06 §13a: route-contract tests "did not finish inside the tool window" | They finish: 46.1 s / 28.0 s each, 314 s for the file (corrected in that report). |
| NF-06: "420 NovaForge tests collected" | **425** collected now (five added after that figure). |
| NF-01/NF-03…NF-06: the extra sidebar entries (`Evidence`, `Benchmarks`, `Evaluation`, `Forge Coach`) | They were enabled, but they were also **redundant**: all five opened one panel. The four extras are deleted (§8.3); the phases are reached through their tabs. |
| NF-05 §12 performance figures | Not re-measured; no measured-path code changed. Treated as previous-pass figures. |

## 10. Not verified / UNAVAILABLE

1. **Real gameplay** — no game launched, no input path (`INPUT_PATH_AVAILABLE = False`), all evaluations observation-only.
2. **Objective verification** — no verifier; `objective.verified` false and `achieved` null everywhere.
3. **A certified improvement** — the live experiment ran one run per arm and returned `inconclusive`; a cross-basis pair is `not_comparable` by design.
4. **Model-backed coaching** — no model is invoked or available.
5. **Retention caps** (40/60/60/60, 240 events) are unit-tested, not exercised to the boundary live (16 sessions, a handful of runs).
6. **`repeat_count > 1` and a guardrail breach** have unit coverage, not a live multi-run experiment.
7. **Non-Windows** — capture, window enumeration and the input guard were verified on Windows only.
8. **Linux CI / Python 3.12 / Playwright** — unavailable on this host (12 skips).
9. **Performance** — only the timings in §1 and §7 are fresh; NF-04/NF-05 figures are historical.

## 11. Go / no-go decision

**NO-GO for beginning the next phase or the game-by-game evaluation campaign.** The evidence supports the
observation/measurement/evidence/experiment chain (NF-01…NF-06), and it supports giving an operator honest
answers about what a recorded run measured. It does **not** support a gameplay claim: three of the five
prerequisites in §4 are absent and one is partial. The next honest step remains the one NF-05 and NF-06
already name — a slow, turn-based, offline environment with a reproducible start state, plus a decision
pipeline, a Guardian-enforced input path and an independent objective verifier. Until those exist, running
the game-by-game campaign would produce observation records only.

**Update (2026-10-20, NF-07):** two of the three named prerequisites now exist — a Guardian-enforced gameplay
input path (digest-bound proposals, policy-bounded input through the shared `InputGuard`, Guardian abort that
interrupts an in-flight hold) and an independent objective verifier that reads the window rather than the
action log. The decision pipeline (choosing what to do from what was observed) is still absent, and the
verifier understands one objective kind (a window title), so the game-by-game campaign still cannot run
against a game that does not announce success in its title. The observation-only conclusion for NF-04, NF-05
and NF-06 is unchanged: those phases send no input. Evidence: `docs/NOVAFORGE_NF07_REPORT.md` and §11 of
`docs/GAME_CONTROL_READINESS_AUDIT.md`.

**Update (2026-10-21, NF-08):** the last of the three prerequisites now exists in a bounded form — `ClosedLoopRunner` chooses one action at a time from a structured observation and executes it through the same plan/digest/Guardian path a manual action uses. It is NOT a gameplay planner: the only shipped policy is a deterministic rule for the disposable harness (`model_backed: false`), so the run proves the loop and its gates, not game intelligence. The verifier also understands a second objective kind, `ocr_text`, read from a fresh, target-cropped frame; region-bounded matching is supported, numeric comparison is not, and a no-confidence engine may decide from recognised words but may not prove absence. One live run completed `verified_success` from OCR evidence after one injected key; the game-by-game campaign is still BLOCKED on a controllable environment and a policy worth measuring. Evidence: `docs/NOVAFORGE_NF08_REPORT.md`.

## 12. Compliance and evidence

* No existing test was skipped, weakened or deselected during this audit. Six tests were **added** as new
  regression coverage and their assertions fail on the old behaviour (mutation-tested, §8.1 and §8.3). The
  only existing assertions changed are the two that pinned the four redundant sidebar destinations
  (`test_planned_destinations_are_disabled_and_labelled`, the NF-04/NF-05 panel-contract tests): they now
  require those attributes to be **absent** and exactly one NovaForge destination to exist — strictly more
  than they asserted before, never less.
* No game was launched and no gameplay input was sent.
* No capability is claimed that the live run did not demonstrate; "completed"/"valid"/"inconclusive" use the
  phases' own definitions (data complete / eligible / effect not established).
* Nothing was reset, stashed, cleaned, committed or pushed. Servers and the disposable window started for
  this audit were stopped.
* Evidence artifacts (outside the repository): `/tmp/nf_audit/merged.tsv` and `summary5.tsv` (per-file
  results), `run4_*.log` / `run5_*.log` (clean per-file logs), `live_probe_final.out` (25-step live output),
  `live3.log` (fresh server log).

---

## Appendix A — NF-13A … NF-22 milestone verification (2026-10-26)

**Tree:** `main`, working tree **dirty** — the NF-13A…NF-22 phase source, tests and reports are all
uncommitted, exactly as they were for the NF-01…NF-06 audit above. Nothing was reset, stashed, cleaned,
committed or pushed during this pass either.
**Host:** Windows 11, Python 3.13 (`.venv`), no local model runtime (`NOVACONTROL_DISABLE_OLLAMA=1` for the
live pass); PySide6 6.12.0 is installed locally and was deliberately hidden for the CI-parity type check.
**Scope:** the native embedded host (NF-13A), embedding compatibility (NF-14), unified observation (NF-15),
state understanding (NF-16), objective/progress understanding (NF-17), game knowledge and memory (NF-18),
the control interface (NF-19), planning and action selection (NF-20), outcome verification and recovery
(NF-21) and first autonomous gameplay (NF-22) — plus their integration with everything §1–§11 already audited.
**Verdict:** **PASS WITH LIMITATIONS for NF-13A … NF-22.** Every surface those ten phases claim is present,
reachable and covered; the whole suite is green **except one Playwright E2E** whose failure is a real,
reproduced UI defect outside the ten phases' code (§A.3.3). **Live gameplay is NOT_TESTED**, no input was
sent to a real game in this pass, and NF-13A's game-embedding half stays BLOCKED on a windowed
(non-borderless) game the operator starts.

> **Follow-up remediation pass (later, same tree):** the one failure above and the NF-20 approval-token
> limitation the phase reports recorded are both **fixed and pinned** — see §A.3.3 (resolved) and
> `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md` items R-01 and R-02, with the audit trail in
> `docs/NOVAFORGE_NF13A_NF22_FINAL_VERIFICATION_REPORT.md`. The numbers in §A.1 and A.2 are the record of
> *this* pass and are left as measured; the follow-up pass re-ran the affected suites, both acceptance gates
> and both type-check views on the changed files.

### A.1 What was executed

All commands ran from the repository root with `PYTHONPATH=src` using `.venv/Scripts/python.exe`.

| # | Check | Exact command | Result |
|---|---|---|---|
| 1 | Phase acceptance scripts (11) | `python scripts/novaforge_nf14_compat_probe.py`, `…_nf15_observation_probe.py`, `…_nf16_state_eval.py`, `…_nf17_objective_eval.py`, `…_nf18_knowledge_eval.py`, `…_nf19_control_acceptance.py`, `…_nf22_autonomy_acceptance.py`, `…_gameplay_acceptance.py`, `…_controlled_evaluation_acceptance.py`, `…_policy_acceptance.py`, `…_embedding_acceptance.py` | **11/11 exit 0**, each script's own checks passed (A.2) |
| 2 | Focused phase tests | `python -m pytest tests/test_api_reference_sync.py tests/test_route_shape_contract.py tests/test_novaforge_autonomy.py tests/test_novaforge_closed_loop.py -q` | **183 passed, 483 subtests passed in 82.84 s** |
| 3 | Whole test suite | `python -m pytest tests/ -q` (single process, one log) | **5 663 passed, 3 847 subtests passed, 1 skipped, 1 FAILED in 1 959.81 s (32:39)** — the failure is the defect in A.3.3, fixed in the follow-up pass |
| 4 | Types, both CI views | `python -m mypy src` and `python -m mypy src --platform win32` | **Success: no issues found in 451 source files** each |
| 5 | Types, CI parity (third-party extras absent) | PySide6 hidden from `.venv` for the duration of the run, then `python -m mypy src` with the real `pyproject.toml` and a fresh `--cache-dir` | **Success: no issues found in 451 source files**; PySide6 restored and re-imported afterwards |
| 6 | API reference sync | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| 7 | Route cross-check (the five places) | in-pass script: live `create_app()` routes vs `ApiSurface.default().routes` vs `ROUTE_CONSUMERS` | **NovaForge 101 live = 101 declared = 101 consumers**; whole surface 376 live vs 372 declared, the 4 extras being FastAPI's own `/docs`, `/docs/oauth2-redirect`, `/openapi.json`, `/redoc` |
| 8 | Live user-level HTTP workflow | 19-step probe against a fresh `uvicorn novacontrol.api.app:create_app` on `127.0.0.1:8001` | 19 steps written, 17 passed, 0 product failures — the 2 mismatches are this probe's own expectations, see A.5 |
| 9 | Live browser pass (real Chromium) | fresh load of `http://127.0.0.1:8001/`, sidebar → NovaForge, tab clicks, console + network inspected | **zero console messages**, every request 200, card and controls honest (A.4) |

Durations: the focused pytest run above is 82.84 s; the acceptance scripts are dominated by full
application boot and live window probes (each 5–40 s). No figure here is estimated.

#### A.1.1 Whole-suite result (one process, one log, final tree)

```
FAILED tests/test_browser_token_reuse_e2e.py::BrowserTokenReuseE2ETests::test_approve_and_run_reuses_token_without_replanning
1 failed, 5663 passed, 1 skipped, 2928 warnings, 3847 subtests passed in 1959.81s (0:32:39)
```

Only **one** test skipped, where the NF-01…NF-06 pass recorded twelve: Playwright and a launchable
Edge/Chrome channel are now present on this host, so the five E2E modules that used to skip at module scope
did run — which is why this pass sees an E2E failure the earlier pass could not. The rerun of that single
file is deterministic (79.22 s, same wait), and its cause is measured in §A.3.3, not assumed. Every other
file, including the whole NF-01…NF-22 NovaForge surface and the web platform, is green.

### A.2 Per-phase verdicts, with this pass's evidence

| Phase | Its report's verdict | Evidence in this pass | Verdict |
|---|---|---|---|
| NF-13A embedded game host | PARTIALLY VERIFIED (host live-verified; embedding an installed game blocked) | host probe artifacts present under `data/novaforge/nf13a/` with `errors: none`; the embedding acceptance script re-ran to exit 0 and measured a **borderless/fullscreen** Unity window (`class 'UnityWndClass'`, style `0x94000000`, 2880×1800 at (0,0), dpi 192) whose reparenting is **refused with a measured reason** | **PASS WITH LIMITATIONS** (game-embedding half UNAVAILABLE on this host) |
| NF-14 embedding compatibility | PASS WITH LIMITATIONS | `novaforge_nf14_compat_probe.py` exit 0: report written, `errors: []`, `sfa_class: UNSUPPORTED`, `fixture_embedded: true`, `state_after_destruction: closed` | **PASS WITH LIMITATIONS** |
| NF-15 unified observation | PASS WITH LIMITATIONS | `novaforge_nf15_observation_probe.py` exit 0, `"ok": true`, 408-line report with live `window_crop`/`window_surface` reads and honest `target_unreadable` refusals | **PASS WITH LIMITATIONS** |
| NF-16 state understanding | PASS WITH LIMITATIONS | `novaforge_nf16_state_eval.py` exit 0: **3/3 frames analysed, 8/8 fields recognised**, `hud=['recognized', …]`, latency first 1504.9 ms / steady 1272.0 ms | **PASS WITH LIMITATIONS** |
| NF-17 objective & progress | PASS WITH LIMITATIONS | `novaforge_nf17_objective_eval.py` exit 0: **32/32 fixture checks, accuracy 1.0, false completions 0**, discovery precision/recall 1.0, progress/outcome/unknown 1.0, **7/7 service guarantees**, avg latency 0.116 ms | **PASS WITH LIMITATIONS** |
| NF-18 game knowledge & memory | PASS WITH LIMITATIONS | `novaforge_nf18_knowledge_eval.py` exit 0 with `live evaluation: NOT_TESTED` (no live game corpus) | **PASS WITH LIMITATIONS** |
| NF-19 control interface | PASS (20/20 live checks) | `novaforge_nf19_control_acceptance.py` exit 0, `"failed_steps": []` (the four tracebacks in its output are CPython asyncio shutdown noise, A.5) | **PASS** |
| NF-20 planning & action selection | PASS WITH LIMITATIONS (live planning NOT_TESTED) | `novaforge_gameplay_acceptance.py` exit 0, `{"steps": 15, "failed": [], "ok": true}`; planner services reachable (`/novaforge/sessions/{id}/plan`, `/plan/execute`, `/plan/replan` all 101/101/101-declared and answered) | **PASS WITH LIMITATIONS** |
| NF-21 outcome verification & recovery | PASS WITH LIMITATIONS | `novaforge_controlled_evaluation_acceptance.py` exit 0: `objective_verified: true`, `verdict: verified_success`, `control_exercised: true`, 15 steps / 0 failed; `novaforge_policy_acceptance.py` exit 0 with `candidate_state_dependent: true`, `baseline_did_not_complete: true` | **PASS WITH LIMITATIONS** |
| NF-22 first autonomous gameplay | PASS WITH LIMITATIONS, `live_gameplay: NOT_TESTED` | `novaforge_nf22_autonomy_acceptance.py` exit 0: **16/16 steps OK**, `failed: 0`, trace shows the whole cycle `INITIALIZING → IDENTIFYING_OBJECTIVE → OBSERVING → UNDERSTANDING_STATE → PLANNING → EXECUTING → VERIFYING → COMPLETED`, 3/3 dispatched actions verified with evidence refs, every budget/refusal/pause/cancel step honest, `live_gameplay: NOT_TESTED`; 112 autonomy tests; the panel card verified live in a browser (A.4) | **PASS WITH LIMITATIONS** |

Cross-phase integration held in every check above: one `NovaForge` manager owns the phases, the autonomy
controller composes NF-15…NF-21 rather than reimplementing them (its own module imports none of their
internals), every phase's route set is declared in all five places (A.3.1), and the whole suite — including
the NF-01…NF-06 suites this document audited — is green (§A.1.1).

### A.3 Defects found and fixed in this pass

Both are integration-level, both are now pinned by a test that fails on the old behaviour, and neither
changes a phase's design.

#### A.3.1 Six routes were served but declared nowhere (fixed, now regression-tested)

A route is done in five places (handler, `ApiSurface`, `ROUTE_CONSUMERS`, `docs/API.md`, a test). Six
handlers had shipped with only the first and the fourth-by-regeneration: `POST /build/project`,
`GET /build/artifacts`, `POST /build/run`, `GET /models/status`, `POST /models/load` and
`POST /models/unload`. They answered correctly and were invisible to the frontend contract, to the
`ApiSurface` the app serves at `/status`, and to every drift check the suite had — because the existing
checks compare the four **declarations** with each other and never against the handlers.

Fixed at the source: the six rows were added to `api/models.py` and `api/route_consumers.py` (the doc block
follows from those rows), and `tests/test_api_reference_sync.py` gained
`test_every_route_the_app_serves_is_declared`, which parses every `@app.<verb>("…")` decorator out of
`api/app.py` and asserts the served set is a subset of the declared set. FastAPI's own `/docs`, `/redoc`,
`/openapi.json` and `/docs/oauth2-redirect` are added by the framework rather than by a decorator here, so
the pin needs no exemption list.

**Evidence:** the cross-check now reports **101 live = 101 declared = 101 consumers** for `/novaforge…`;
`generate_api_reference.py --check` is in sync; and the pin was **mutation-tested** — inserting one
undeclared probe route (`GET /nc22-undeclared-probe`) into `api/app.py` fails it with
`('GET', '/nc22-undeclared-probe') : every handler in api/app.py must be declared in ApiSurface and
ROUTE_CONSUMERS`, and removing the probe restores green.

#### A.3.2 The two type-check environments disagreed about the Qt subclasses (fixed, environment-proven)

The NF-22 review pass removed two `# type: ignore[misc]` comments from `gui/game_host.py` because local
strict mypy called them unused. That fix is correct locally and **wrong in CI**: CI installs only `.[dev]`,
so PySide6 is unresolvable there and each Qt base class degrades to `Any` under the `PySide6.*`
`ignore_missing_imports` override — which is exactly the `Subclassing Any` `[misc]` error those comments
were suppressing. Neither an ignore nor its absence is correct in both environments.

Fixed with the remedy this file already documents for sqlalchemy: a module-scoped override in
`pyproject.toml` (`novacontrol.gui.app`, `novacontrol.gui.game_host`, `disallow_subclassing_any = false`),
which is correct in both, so the comments stay removed.

**Evidence:** with the override and a fresh cache, hiding PySide6 from `.venv` and running the full
`python -m mypy src` yields **no issues in 451 files** — the same result the local (PySide6 present) run
gives. The mechanism was isolated first in a throwaway directory: a module-level `from PySide6.QtWidgets
import QWidget` plus `class X(QWidget)` reports `Class cannot subclass "QWidget" (has type "Any") [misc]`
when PySide6 is hidden (**2 errors, one per subclass — the exact shape of `GameSurface` and
`GameHostPanel`**) and `Success` with the module-scoped relaxation.

#### A.3.3 RESOLVED (follow-up pass) — the JARVIS panel's Approve And Run stopped receiving clicks after its first run

Found by the whole-suite run (§A.1.1) and reproduced outside the suite. `tests/test_browser_token_reuse_e2e.py`
asserts that a second Approve And Run falls back to one re-plan plus one execute; it fails on the wait for
that second pair. It is **not** a race and **not** a test artefact:

* The wire contract is correct. Measured against the running app: `POST /browser/plan` →
  `waiting_for_approval` with a token; `POST /browser/execute` → 200 `status: "executed"`; replaying the
  consumed token → **403** `"Approval token is unknown. Plan the command again and approve the freshly
  previewed action."`, whose text matches the frontend's `/token|expired|unknown/i` fallback.
* Driving the real page in the app's own browser panel (Chromium, two real clicks) does exactly what the
  spec expects: click 1 → `/browser/execute` only (token reused, approval cleared), click 2 → `/browser/plan`
  + `/browser/execute` (the fallback), with the executed answer rendered.
* In the spec's own headless Edge channel it does not: a captured event trace shows click 1 as
  `mousedown/mouseup/click → jarvisRunButton`, and **every later click as
  `mousedown → ARTICLE.command-console`, `mouseup → jarvisRunButton`, `click → ARTICLE.command-console`** — a
  `click` is only dispatched to the nearest common ancestor, so `#jarvisRunButton`'s listener never runs and
  **no request is made at all**. The button is enabled (`disabled: false`), visible, `pointer-events: auto`,
  and `document.elementFromPoint` at the same coordinates resolves to it in the same task — yet the input
  lands on the container. Delaying the click one second changes nothing (it is not a settling race), and the
  point's element stack reads `jarvisInput → DIV.ai-composer → ARTICLE.command-console` at a moment the
  button's own box is reported over that point, i.e. the composer region is still moving under the pointer.

So the phase-level conclusion — token reuse and the one-re-plan fallback are implemented and reachable — holds,
but a user of this panel in a browser where the composer keeps reflowing after a run can click Approve And Run
and get **silence**, with no request and no message. It was recorded here rather than worked around in the test,
because the spec asserts the correct behaviour and no assertion was weakened to make the suite green.

**Root cause (found in the follow-up remediation pass).** The mechanism is not scrolling and not the composer's
width. `effects.js` tilts every card in `.surface, .command-console, .info-card, .metric-card, .telemetry-card`
on each `mousemove` with `perspective(800px) rotateX(...) rotateY(...) translateZ(4px)`. Once the JARVIS
console (which contains `#jarvisRunButton`) has carried that 3D transform, the browser hit-tests pointer input
through the transformed ancestor and **retargets the click to the card**: the measured trace at the button's own
coordinates is `box = [1071, 240, 150, 37]` with `document.elementFromPoint` also resolving to the button, while
the dispatched `mousedown` targets `ARTICLE.command-console` — so the nearest-common-ancestor rule sends `click`
to the article and the button's listener never runs. The first click works because no run has re-rendered the
console's subtree yet; every later one is swallowed, at any delay.

**Fix.** The tilt is refused for any card that contains an interactive control (`button, a[href], input,
select, textarea, [role='button'], [contenteditable='true']`, cached per card in a `WeakMap`), because a
decorative transform must never sit on an ancestor of a control. Measured live afterwards: of 86 cards, the 42
that hold controls are never transformed (the JARVIS console among them) while the 44 control-free cards still
receive `perspective(800px) rotateX(...) …`, so the effect survives where it cannot intercept input.

**Evidence.** `python -m pytest tests/test_browser_token_reuse_e2e.py -q` → **1 passed** (was the suite's only
failure); `tests/test_reduced_motion_e2e.py` → **2 passed** (its tilt probe still observes the transform);
`tests/test_web_platform.py -k ancestor_of_a_control` → **1 passed**, mutation-tested (with the guard removed the
pin fails by name, and the file was restored byte-identically). Live in the app's own panel at 1280×900, two real
clicks on Approve And Run produce `POST /browser/plan, /browser/execute, /browser/plan, /browser/execute` with
zero console messages. Full detail: `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md` R-01.

### A.4 Live user-level verification

**HTTP (fresh `uvicorn`, 127.0.0.1:8001, no API token configured → the local principal):**

* `/health` 200, `ui_version` `27f8dd3ffe8a`; `/` 200 with the exact never-cache trio
  (`no-cache, no-store, max-age=0, must-revalidate` + `pragma: no-cache` + `expires: 0`), 126 072 bytes,
  **no un-substituted `__NC_ASSET_VERSION__`/`__NC_UI_VERSION__` marker**, `?v=` injected, and the single
  `data-panel="novaforgePanel"` destination present.
* `/novaforge/status` 200 with the honest limitation list (`capture_supported: true`,
  `windows_available: true`, `session_count: 40`, four named limitations incl. "NovaForge never launches a
game"); `/novaforge/games` 200 (`runnable_count: 3`, `observation_only_count: 2`,
  `controllable_count: 1`); `/novaforge/library` 200; `/novaforge/capabilities`, `/windows`, `/sessions`,
  `/autonomy/trace`, `/trials`, `/state/profiles`, `/objectives/profiles` all 200.
* `POST /novaforge/sessions/does-not-exist/autonomy/start` → **422** with
  `"No NovaForge session is named 'does-not-exist'."` — refused, nothing started.
* The three previously undeclared routes now answer and are declared: `POST /build/project` 200,
  `GET /build/artifacts` 200, `GET /models/status` 200.

**Browser (real Chromium, fresh load against that server):** the page carried the server's injected asset
version (`/static/js/dom.js?v=27f8dd3ffe8a`) and the sidebar footer stamp `UI v27f8dd3f` matched `/health`'s
`ui_version` prefix — the stale-page trap this project fights is measurable here. **Zero console messages**
were produced by the load. Clicking the sidebar's NovaForge destination opened the panel; all ten phase tab
buttons are present and clicking *Forge Coach* activated exactly `forgeCoachView` (one visible view). The
**NF-22 autonomy card** (`#forgeAutonomyCard`) renders its honest empty state on the default tab: badge
`untested`, the plain-language explanation of what a run does, and *"No autonomous run for this session."*
with `Start`/`Pause`/`Resume`/`Cancel`/`Approve` **disabled** and only `Refresh Run` enabled — i.e. a user
who has not selected a session cannot start an autonomous run from the page.

### A.5 Observations that are not defects

1. `GET /novaforge/knowledge/records` answers **422** without `?game=` — by design: knowledge is scoped to
   one game, and the route says which parameter is missing. (This probe expected 200; the route is right.)
2. `GET /novaforge/sessions/{unknown}/autonomy` answers **200** with the service's bounds, `runs: []`,
   `sessions: []` and `counts` at zero, while its sibling `…/autonomy/start` refuses with **422**. The
   status payload is about the service (what a run would be bounded by) and honestly reports no run and no
   session; the panel only ever asks it for a real selected session id (`forge.sessionId`), so the looser
   shape is not reachable as a false claim. Noted, not changed.
3. `/events/stream` returns **no response head until the first event** (a 6 s `curl -N` and a 10 s socket
   read both see nothing), while the browser's `EventSource` records `200` and receives steps — verified
   live in §7's pass and again by the network log here. An initial `: connected` comment would make the
   connection observable earlier; it is an improvement, not a defect, and was not made in this pass.
4. Two acceptance scripts (`nf19`, `embedding`) print CPython's
   `Exception ignored in: <function …SubprocessTransport.__del__>` / `Event loop is closed` noise at
   interpreter shutdown. Both exit 0 and their reports complete; the noise is the asyncio transport being
   collected after the loop closes, not a phase failure.

### A.6 Not verified / UNAVAILABLE

1. **Live gameplay** — no game was launched and no input was sent to a real game in this pass; NF-22's
   `live_gameplay` is `NOT_TESTED` and NF-20's live planning is `NOT_TESTED`.
2. **A verified gameplay objective on a real game** — NF-17/NF-21 verify objectives from readings, and the
   only objectives exercised live were the disposable harness's own title/OCR ones.
3. **Game embedding of an installed title** — NF-13A's remaining measurement needs a windowed game; the one
   examined here is borderless/fullscreen and is refused by design.
4. **Model-backed reasoning anywhere on this path** — no local model runtime was present (`Echo` provider).
5. **Non-Windows behaviour** — capture, window enumeration, embedding and the input guard were verified on
   Windows only; the CI-parity type check is the only Linux-shaped check run here.
6. **Retention caps exercised to their boundary** live (40 sessions / 60 runs / 240 events are unit-tested).
7. **A real game's success/failure signal** for a commercial title — the fixture and harness are the only
   worlds NF-22 ran in.
8. ~~**The Approve And Run click-loss defect (§A.3.3)** — characterised and reproduced, not fixed; the single
   failing test in the whole suite.~~ **Resolved in the follow-up remediation pass**: the cause was the 3D card
tilt retargeting input, the guard and its pins are in §A.3.3, and the suite entry that failed here now passes.

### A.7 Compliance

* No existing test was skipped, weakened or deselected in this pass; one test was **added**
  (`test_every_route_the_app_serves_is_declared`) and mutation-tested (§A.3.1).
* No game was launched, no input was injected into any real application, and the autonomous loop was
  exercised only in its simulated fixture world (the acceptance script's own `evidence_source: simulated`).
* Nothing was reset, stashed, cleaned, committed or pushed. The live server started for §A.4 was stopped
  and the mutated file restored (verified by re-grep) before the run recorded in A.1.1.
* No capability is claimed that the run did not demonstrate: "PASS" here means the phase's own checks
  passed, never that NovaForge can play an arbitrary game.

---

## Appendix B — NF-24 milestone verification (2026-10-27)

Source: `docs/NOVAFORGE_NF24_REAL_GAME_STATE_UNDERSTANDING_REPORT.md` (full evidence),
`docs/NOVAFORGE_NF24_IMPLEMENTATION_PLAN.md` (completed work + deviations). This appendix records the
**current** results; Appendix A and the phase reports above are left as they were written.

### B.1 What was executed

Current results (the verification pass, B.7; the first-pass numbers are kept in the phase report §7):

| Command | Result |
|---|---|
| `pytest tests/test_novaforge_nf24.py -q` | **57 passed** (incl. a Node group against the real `novaforge.js`); **41** at the first pass |
| `pytest tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py tests/test_novaforge_observation.py tests/test_novaforge_nf23.py -q` | **277 passed** |
| `pytest tests/test_novaforge_ui.py tests/test_novaforge_api.py tests/test_perception.py -q` | **279 passed, 94 subtests** |
| `pytest tests/test_novaforge.py tests/test_novaforge_planning.py tests/test_novaforge_verification.py -q` | **248 passed, 53 subtests** |
| `pytest tests/test_novaforge.py tests/test_novaforge_closed_loop.py tests/test_novaforge_gameplay.py tests/test_novaforge_planning.py -q` (first pass) | **237 passed, 33 subtests** |
| `pytest tests/test_novaforge_verification.py tests/test_novaforge_autonomy.py tests/test_web_api.py -q` (first pass) | **296 passed, 120 subtests** |
| `mypy src` / `mypy src --platform win32` | both **clean** (452 files) |
| `python scripts/generate_api_reference.py --check` | **in sync** (no route added) |
| `python scripts/novaforge_nf16_state_eval.py` | fixtures **20/20**; recorded frames 3/3, 8/8 fields |
| `python scripts/novaforge_nf17_objective_eval.py` | **32/32**, false completions **0**, service 7/7 |
| `python scripts/novaforge_nf18_knowledge_eval.py` | **51/51** |

### B.2 Per-objective verdicts

| NF-24 objective | Verdict | Evidence |
|---|---|---|
| A — evidence-grounded state model | **PASS** | `GameState.scene`/`transition`, observed/inferred/unknown with quotes, measured confidence or `None` |
| B — actual visual interpretation | **PASS on the shipped path; NOT on a live game** | recorded frame: `menu` **observed** from real OCR (`SINGLE PLAYER OPTIONS QUIT GAME CREDITS`); no second OCR or capture stack |
| C — profiles and capability boundaries | **PASS (bounded)** | scene rules only for the fixture layouts and the `GAME MENU` fixture; a missing profile yields `unknown`, not an invented state; a profile now also **declares what the pipeline must provide** (`required_capabilities`, refused when unmet) and a rule may set a confidence floor |
| D — changes and temporal consistency | **PASS** | N-of-M confirmation, duplicate/contradiction handling, `scene_changed` only when confirmed, session reset, persisted restore, and a non-contiguous observation **sequence** drops the unconfirmed run instead of counting across a capture gap |
| E — objective recognition and progress | **PASS** | scene evidence channel with single-attribution rule; disappearance is still not completion; nothing is completed by an executed action |
| F — knowledge/verification integration | **PASS** | `situation:scene:<kind>` with observed/inferred method; NF-21 untouched, `NOT_VERIFIABLE` still reachable |
| G — tests and regression coverage | **PASS** | 57 new tests + the suites in B.1; no existing assertion weakened (the schema pin moved to `nf24.1` deliberately) |
| H — live verification | **BLOCKED** | no real game window can exist on this host (B.4) |
| I — performance and resources | **PASS** | 939.1–979.3 ms recorded / 734.6–738.9 ms live window; no model load; O(1) tracker; one analysis at a time |

### B.3 What changed

New: `src/novacontrol/novaforge/scene.py`, `tests/test_novaforge_nf24.py`. Modified:
`novaforge/state.py` (schema `nf24.1`, `scene_rules`, `scene`/`transition`, measured confidence,
`scene_changed`), `novaforge/state_service.py` (tracker ownership/restore/session reset, event and status
fields), `novaforge/objectives.py` (`EvidenceKind.SCENE` + attribution rule), `novaforge/knowledge.py`
(confirmed-scene `situation` record), `web/static/js/novaforge.js` (scene row/sentence, export),
`tests/test_novaforge_state.py` (schema pin), and the two NF-24 documents. No API route changed.

### B.4 Live evidence — and why the live-game half is still blocked

* **Blocked, re-measured with Steam running** (steam PID 4740, `SteamService` 19220) and the gate open
  (3.41 GB free, 77.8 % used): the product's own `GameLaunchService` returned
  **`INSTALLED_BUT_LAUNCH_FAILED`** after 122.45 s; Steam's log shows `Launcher.exe` (AppID 1347780) added at
  19:22:21 and removed at 19:22:23; running `Launcher.exe` from its own directory prints
  **`SteamAPI_Init failed`** and exits **rc=1 in 0.5 s**. The other candidate offline folders
  (`Poppy Playtime`, `Aurora Hills Chapter 1`, `Quarantine Zone The Last Check Demo`) are **empty (0 bytes)**;
  the remaining launchable titles are GTA V (excluded) and two online multiplayer titles. No process was left
  behind and nothing was force-killed.
* **Live non-game check (not game evidence):** the product's own services read two fresh frames of a
  disposable harness window (840×600, `printwindow`): scene **`unknown`** with the reason
  *"no profile matched…"*, transition **`none`**, 738.9 / 734.6 ms. Measured integration property: at a 0.5 s
  observation interval every reading is refused `observation_superseded` (perception ~740 ms); 3.0 s succeeds.
* **Recorded-frame check (not live):** three consecutive `GAME MENU` frames → `menu` **observed via
  `game-menu@1`**, `pending (1/2) → confirmed → none`, HUD `SINGLE PLAYER OPTIONS QUIT GAME CREDITS`, knowledge
  candidates 3 → 4 → 3 (one confirmed-scene record only).

### B.5 Not verified / UNAVAILABLE

1. **Live-game state interpretation** — no real-game frame exists on this host (B.4).
2. **A commercial game's screens** — no profile for one, and none is claimed.
3. **Gameplay as a scene** — deliberately not inferred from cues; only a declared rule can name it.
4. **Per-field confidence** — the shipped OCR reader reports none; the state-level measured mean is reported
   instead, and is `None` here.
5. **Live objectives/knowledge driven by a real game** — the fixture and recorded-frame paths are verified;
   the real-game path is the same blocker as (1).

### B.6 Compliance

* No test was skipped, deselected or weakened; one schema pin was updated because the wire shape grew, and its
  comment says why. Twelve defects/over-permissive behaviours were found and fixed with pins (report §11 and
  §15.1).
* No game was launched successfully (none could be), no gameplay input was sent, no display mode was changed,
  no process was force-killed, and the disconnected external drive was not touched.
* Nothing was committed, pushed, reset or stashed. Scratch evidence lives in untracked `.nc24_tmp/`.
* The verdict is **PARTIAL**: PASS on the deterministic and live-window-verifiable scope, BLOCKED on
  live-game state interpretation — no capability is claimed that was not demonstrated.

### B.7 Verification pass (2026-10-27, after the phase was first reported)

Scope: re-check NF-24 against its own objectives with inputs the first pass never tried — no re-run of the same
cases. B.1/B.2 above already reflect these results; the live-game result was **not** re-attempted (nothing about
the blocker changed) and remains BLOCKED, so the verdict is unchanged.

| # | Defect found | Why it mattered | Fix |
|---|---|---|---|
| N-07 | cues and rule patterns matched as **substrings**; bare `"victory"`/`"defeat"` were cues | `"game over"` inside `"game overlay"`; an on-screen objective `"Defeat the boss"` named the DEFEAT screen, which — once confirmed — **failed the objective** | whole-word phrase patterns for cues and rules; bare screen nouns removed from the cue table (a one-word banner is a profile rule's job) |
| N-08 | the tracker's contradiction log was **unbounded** | one string per candidate flip for the life of the process | `MAX_TRACKED_CONTRADICTIONS = 8`, trimmed oldest-first, also on restore |
| N-09 | confirmation counted **frames**, not the observation sequence | observations 1 and 5 agreeing confirmed a transition across three never-captured frames; the persisted run had the same hole after a restart | `observe(..., sequence=)` drops the unconfirmed run on a gap, records why, marks the transition `interrupted`; `sequence`/`interrupted` persist and restore |
| N-10 | Objective C incomplete: no declared capability requirement, no confidence floor | a text-dependent profile could not say OCR was missing, and a rule fired over an **unmeasured** reading | `GameProfile.required_capabilities` (validated vocabulary) + `SceneRule.min_confidence`; an unmet OCR requirement returns `unknown` with `source: "capability"`; a floored rule keeps the screen `unknown` and a weaker cue may not decide it |
| N-11 | the panel showed `unknown (unknown)` and hid the reason | the one thing an operator needs was rendered nowhere | `stateSceneText` shows the reason for an unknown screen and names a capture interruption beside the pending count |
| N-12 | profile rules recompiled their phrase pattern on every frame, against the documented "compiled once" design | the same handful of regexes was rebuilt per rule per observation | `_cue_pattern` is `lru_cache`d (256); one compiled pattern now serves cues and rule patterns (57-test suite 0.83 s → 0.31 s, no behaviour change) |

N-07 to N-11 each have a pin in `tests/test_novaforge_nf24.py` (41 → 57 tests); N-12 changes no answer, so it is
pinned by the same suite staying green. Commands and counts are in B.1; the full
event-by-event evidence is in the phase report §15.

## Appendix C — NF-25 milestone verification (2026-10-27)

Source: `docs/NOVAFORGE_NF25_REAL_GAME_OBJECTIVE_EXECUTION_REPORT.md` (full evidence),
`docs/NOVAFORGE_NF25_IMPLEMENTATION_PLAN.md` (plan as built). This appendix records the **current** results;
Appendices A and B and the phase reports above are left as they were written.

### C.1 What was executed

| Command | Result |
|---|---|
| `pytest tests/test_novaforge_nf25.py -q` | **91 passed** |
| `pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_verification.py tests/test_novaforge_planning.py tests/test_novaforge_nf24.py -q` | **417 passed, 74 subtests** |
| `pytest tests/test_novaforge_api.py tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_control.py -q` | **198 passed, 16 subtests** |
| `pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_api.py tests/test_novaforge_control.py -q` (after the verification fixes) | **269 passed, 37 subtests** |
| `pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_verification.py tests/test_novaforge_planning.py tests/test_novaforge_nf24.py tests/test_web_api.py -q` (final regression) | **492 passed, 146 subtests** |
| `pytest tests/test_web_api.py -q` | **75 passed, 72 subtests** |
| `mypy src` / `mypy src --platform win32` | both **clean** (453 files) |
| `python scripts/generate_api_reference.py --check` | **in sync** (no route added) |
| `python scripts/novaforge_nf25_task_eval.py` | fixtures **7/7**, false completions **0**, layer latency **0.0013–0.0019 / 0.030–0.032 / 0.060–0.075 ms** |

### C.2 Per-objective verdicts

| NF-25 objective | Verdict | Evidence |
|---|---|---|
| A — bounded real-game task | **PASS** | `TaskSpec` refuses a task with no objective, a vague success condition, no required evidence, an unknown action kind or a budget above the supervisor's ceiling |
| B — evidence-grounded action selection | **PASS (delegated) + policy gate** | the task refuses to start on an unreadable/stale/unnamed/wrong initial screen; planning stays NF-20's through NF-22; a dispatch outside the task's permitted actions cannot be judged `verified_completion` |
| C — safe execution through existing controls | **PASS on fixtures** | approval, target identity and control-policy gates; actions still travel NF-19 → Guardian |
| D — closed-loop execution | **PASS (inherited)** | `TaskRunner` owns no loop; NF-22's observe→verify cycle is the only one |
| E — objective progress and completion | **PASS** | completion needs the terminal ending, the declared evidence **and** the action policy; the nine stages (`task_stages`) are answered one by one; dispatch-only, progress-only, stale-scene and out-of-policy runs stay unverified |
| F — recovery and failure handling | **PASS (mapped)** | each NF-22 ending maps to its own verdict; the spec's recovery limit becomes NF-22's bound; a confirmed cancel frees the session, an unconfirmed one claims nothing, and a cancel after another ending never rewrites it |
| G — live-game test design | **PASS as a design; NOT RUN** | procedure and approval gate documented (plan §7); no approval requested, no input sent |
| H — automated testing | **PASS** | 91 new tests + 417 / 198 / 269 existing tests across the dependency and integration suites |
| I — performance and reliability | **PARTIAL** | the layer's own overhead measured (0.0013–0.0019 / 0.030–0.032 / 0.060–0.075 ms per call); end-to-end cycle latency and the invalid-observation rate need a live target |

### C.3 What changed

New: `src/novacontrol/novaforge/tasks.py`, `tests/test_novaforge_nf25.py`,
`scripts/novaforge_nf25_task_eval.py`, the two NF-25 documents. Modified:
`novaforge/manager.py` (`tasks()` / `run_task()`), `novaforge/__init__.py` (exports, including
`task_stages` and `dispatched_kinds`). No API route changed.

### C.4 Live evidence — none, and why

* No game was launched, no window was observed for NF-25, and **no input was sent**. The brief's live gameplay
  test requires the operator's explicit approval; none was requested, because the NF-23/NF-24 blocker still
  holds (the only safe installed offline title's launcher prints `SteamAPI_Init failed` and exits rc=1; the
  remaining candidates are empty folders, GTA V — excluded — or online multiplayer).
* The fixture eval is labelled `simulated` and is **not** a live-game result. Nothing in Appendix C or the phase
  report claims a live objective execution.

### C.5 Not verified / UNAVAILABLE

1. **Live-game objective execution** — no launchable safe title; no approval (§C.4).
2. **End-to-end observe→act→verify cycle latency** — needs a live target; only the task layer's own overhead was
   measured.
3. **A commercial game's screens as task evidence** — no profile exists, and none is claimed.
4. **An approved live input test** — procedural blocker, deliberately not bypassed.
5. **Plan-time enforcement of the task's action policy** — the gate is applied to the completion claim, not to
   the plan the existing supervisor already built (report §13.7).

### C.6 Compliance

* No test was skipped, deselected or weakened; nine defects were found and fixed with pins (report §12) — five
  during implementation, four in the verification pass: the task's action policy was a declaration rather than a
  gate, a confirmed cancellation left the task active forever (blocking the session), Objective E's nine stages
  were not reported separately, and the eval script's fake supervisor predated the held-input reader (the
  measurement script crashed until it was updated).
* No game was launched, no gameplay input was sent, no display mode changed, no process force-killed, and the
  disconnected external drive was not touched.
* Nothing was committed, pushed, reset or stashed. The eval writes `data/novaforge_nf25_task_eval.json` (runtime
  data).
* The verdict is **PARTIAL**: PASS on the bounded task layer and its evidence discipline, NOT PERFORMED on live
  objective execution — no capability is claimed that was not demonstrated.

## Appendix D — NF-26 milestone verification (2026-10-28)

Source: `docs/NOVAFORGE_NF26_ADAPTIVE_GAMEPLAY_REPORT.md` (full evidence),
`docs/NOVAFORGE_NF26_IMPLEMENTATION_PLAN.md` (plan as built). This appendix records the **current** results;
Appendices A–C and the phase reports above are left exactly as they were written.

### D.1 What was executed

| Command | Result |
|---|---|
| `pytest tests/test_novaforge_nf26.py -q` | **70 passed** |
| `pytest tests/test_novaforge_nf26.py tests/test_novaforge_nf25.py -q` | **161 passed** |
| `pytest tests/test_novaforge_planning.py tests/test_novaforge.py tests/test_novaforge_api.py -q` | **170 passed, 26 subtests** |
| `pytest tests/test_novaforge_autonomy.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_verification.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py -q` | **363 passed, 58 subtests** |
| `pytest tests/test_api_reference_sync.py tests/test_route_shape_contract.py tests/test_application.py -q` | **58 passed, 477 subtests** |
| `mypy src` / `mypy src --platform win32` | both **clean** (454 files) |
| `python scripts/generate_api_reference.py --check` | **in sync** (no route added) |
| `python scripts/novaforge_nf26_learning_eval.py` | **6/6 cases as expected; false labels 0; live-evidence claims 0** (exit 0) |

### D.2 Per-objective verdicts

| NF-26 objective | Verdict | Evidence |
|---|---|---|
| A — evidence-based experience records | **PASS** | facts and interpretations in separate fields; only a judged `verified_completion` is a success; a dispatch-only run stays `uncertain`; a report that never ran produces no record at all; a record cannot claim live evidence whose own `evidence_source` says simulated |
| B — strategy evaluation | **PASS** | named metrics with their own directions, Wilson 95 % intervals, `MIN_TRIALS` = 5 decided trials, per-metric deltas, `live_evidence` separation; an arm that mixes live with simulated, or spans more than one game or strategy version, is refused by name so a rate always describes one class, one title and one profile |
| C — bounded strategy adaptation | **PASS** | `StrategyProfile` can only reorder candidates, shorten a plan and demand fresher evidence; both ordering directions are pinned through the real planner and the plan records the effect it actually applied; every change records its evidence, benefit, risks, conditions and method; approval is enforced, rollback restores the previous profile, and a rolled-back change can be applied again |
| D — failure analysis | **PASS** | signature-grouped patterns with counterevidence, measured confidence, `FailureBlame` classes, remediation, and `verified` only when the record's own evidence names the cause |
| E — persistent learning with provenance | **PASS** | one versioned namespace on the application's JSON store, compact rows (1,412.7 bytes/record), bounded growth, duplicates and corrupt rows handled and counted, `provenance(id)` answers which change cited which experience |
| F — controlled exploration | **PASS as a design; NOT RUN** | `StrategyExperiment` needs approval and a runnable task, alternates arms deterministically, stops on a safety signal or low state confidence, inherits the task's budgets; no live exploration was run |
| G — regression prevention | **PASS** | the reference profile is the rollback target (every change carries the profile it replaced, and rollback restores it to both slots), every change traceable and rollback-able, regressions detected by the comparator, deterministic fixtures |
| H — integration with NF-23 … NF-25 | **PASS** | the layer consumes NF-25's judged report only; recommendations return through NF-20's `PlanningContext.strategy`; `NovaForgeManager.learning()` records the judged run against the strategy in force, and a fresh manager reads it back from the same store |
| I — testing and performance | **PASS (fixture scope)** | 70 new tests; the layer's own cost measured over five runs (0.0138–0.0148 / 0.0146–0.0172 / 0.0429–0.0472 / 0.0157–0.0181 ms; **0.00129–0.00168 ms** added per planning decision); memory: 1,594.4–1,635.4 bytes/record at the 200-record cap (~0.33 MB) and a 411-byte hint; live-loop latency needs a live target |
| J — measurable improvement | **PASS (simulation)** | evaluation set B: 0/6 vs 6/6 completions, disjoint Wilson intervals, 6 decided trials per arm, no safety metric worse; a busier arm, a tuning-only advantage and a safety stop are all refused |

### D.3 What changed

New: `src/novacontrol/novaforge/learning.py`, `tests/test_novaforge_nf26.py`,
`scripts/novaforge_nf26_learning_eval.py`, the two NF-26 documents. Modified: `novaforge/planner.py`
(`PlanningContext.strategy` + the hint's three bounded effects + `plan["strategy"]`), `novaforge/manager.py`
(`learning()`, the hint in `_planning_context`, the `run_task` hand-off, `run_experiment`),
`novaforge/__init__.py` (exports). No API route added or changed.

### D.4 Live evidence — none, and why

* No game was launched, no window was observed for NF-26, and **no input was sent**. The brief's live gameplay
  test requires the operator's explicit approval; none was requested, because the NF-23/24/25 blocker still
  holds (no launchable safe offline title on this host).
* The evaluation is fixture-based and labelled `simulated` in every record and every comparison
  (`live_evidence=False`). Nothing in Appendix D or the NF-26 report claims improved real-game adaptation.

### D.5 Not verified / UNAVAILABLE

1. **Live-game adaptation** — no approval, no launchable safe title; the adapted strategy's real effect is
   untested.
2. **A real-game improvement claim** — the demonstration is simulation-based by construction.
3. **An operator-facing route for the learning surface** — reachable through `NovaForgeManager.learning()` and
   `plan["strategy"]`; a route was not in scope.
4. **A time-windowed retention policy** — growth is bounded by a record cap, not by an age window.
5. **The experiment's starting-condition fairness** — recorded and reported, not detectable by the layer itself.
6. **Schema migration** — records are versioned and any other schema version is dropped and counted, never
   migrated; `nf26.1` is the only version ever written, so no migration is claimed or exercised.
7. **Cross-game or cross-version aggregation** — deliberately absent: the comparator refuses such an arm and
   the caller scopes the records (one game, one version per comparison).

### D.6 Compliance

* No test was skipped, deselected or weakened; **fourteen defects or stated-but-missing pins were found and
  fixed** (the two verification-pass appendices of the report). First pass: a refused approval poisoned a valid change, an applied strategy never reached the
  slot planning reads, identical replays were treated as updates, a dead knowledge collaborator was removed, the
  planner pins were rewritten to drive the real planner instead of a private helper, a rate assertion was
  corrected to match the metric's real denominator, a no-argument `rollback()` could not undo an adaptation
  applied under a different strategy id, plan §7's untrusted-text claim had no pin, and the plan's strategy row
  claimed `preferred_first` for hints that preferred nothing. Objective I's memory figure was also measured
  (1,594.4–1,635.4 bytes/record at the 200-record cap; 411-byte hint). The eval's small-sample control was renamed to
  state its real sample size (two decided trials per arm), with no change to the verdict.
* Second pass, re-checking every objective against the code, found five more, each fixed and pinned: two arms
  that both mixed live and simulated records were treated as comparable and pooled (now refused by name); an arm
  spanning two strategy versions was measured as one rate (now refused unless `version` is pinned); an arm
  spanning two games was measured as one rate (now refused, the caller scopes); a rolled-back change could never
  be applied again (`apply` returned early on `applied_at`, now a fresh activation clears the rollback fields);
  and a record could claim live evidence while its own `evidence_source` said simulated (now a validation error,
  so promotion is refused at the record and not only at the comparator). §13's measurements were re-taken over
  five runs on the final code, and the report's comparability/reference-profile/counterevidence wording was
  corrected to match the implementation.
* No game was launched, no gameplay input was sent, no display mode changed, no process force-killed, and the
  disconnected external drive was not touched.
* Nothing was committed, pushed, reset or stashed. The eval writes `data/novaforge_nf26_learning_eval.json`
  (runtime data).
* The verdict is **PARTIAL**: PASS on the bounded learning layer, its evidence discipline and its fixture-based
  evaluation, NOT PERFORMED on live-game adaptation — no capability is claimed that was not demonstrated.

---

## Appendix E — NF-22 … NF-26 cross-phase milestone audit (2026-10-28)

This pass audited the five phases **together**, including their integration with NF-13A…NF-21, because the
upper layers' honesty depends on labels the lower layers produce: NF-26 measures outcomes NF-25 judged, which
NF-22 executed over evidence NF-23 captured and NF-24 understood. A defect in any arrow invalidates the
capability claimed above it, and each phase's own report could not see the whole chain.

### E.1 What was executed

Every command below ran from the repository root with `PYTHONPATH=src` on the final tree, in this order: the
phase suites, then the complete suite (no single run fits the tool's timeout, so it was run in file groups), then
the static checks, then the evidence scripts, then user-level workflows through a booted application. The suite
was run **twice**: once before this pass's fixes (four groups) and again **after** them (five groups), because
the provenance fix touches the autonomy layer every NovaForge phase sits on.

| Command | Result | Duration |
|---|---|---|
| `python -m pytest tests/test_novaforge_nf26.py -q` (before this pass's fixes) | **71 passed** | 3.8 s |
| `python -m pytest` **files 1–50 of 148** `-q` (before the fixes) | 1465 passed, 1 skipped, 1857 subtests | 546.44 s |
| `python -m pytest` **files 51–90** `-q` (before the fixes) | 1668 passed, 926 subtests | 594.48 s |
| `python -m pytest` **files 91–120** `-q` (before the fixes) | 1692 passed, 203 subtests | 304.57 s |
| `python -m pytest` **files 121–148** `-q` (before the fixes) | 1133 passed, 861 subtests | 402.21 s |
| **complete suite, pre-fix, all 148 files** | **5958 passed, 1 skipped, 3847 subtests, 0 failed** | **1847.70 s (30 m 48 s)** |
| `python -m pytest` **files 1–30** `-q` (**after the fixes**) | **860 passed, 1 skipped, 808 subtests** | 234.56 s |
| `python -m pytest` **files 31–60** `-q` (after the fixes) | **854 passed, 1144 subtests** | 226.94 s |
| `python -m pytest` **files 61–90** `-q` (after the fixes) | **1420 passed, 831 subtests** | 475.42 s |
| `python -m pytest` **files 91–120** `-q` (after the fixes) | **1695 passed, 203 subtests** | 293.06 s |
| `python -m pytest` **files 121–148** `-q` (after the fixes) | **1133 passed, 861 subtests** | 412.83 s |
| **complete suite, final tree, all 148 files** | **5962 passed, 1 skipped, 3847 subtests, 0 failed** | **1642.81 s (27 m 23 s)** |
| `python -m pytest tests/test_novaforge_nf26.py -q` (final) | **74 passed** | 3.8 s |
| `python -m pytest tests/test_novaforge_autonomy.py -q` (final) | **114 passed, 21 subtests** | 24.1 s |
| `python -m pytest tests/test_novaforge_nf25.py tests/test_novaforge_nf26.py tests/test_novaforge_gameplay_api.py -q` (final) | **179 passed, 10 subtests** | 13.1 s |
| `python -m mypy src` | **Success: no issues found in 454 source files** | 2 s (warm cache) |
| `python -m mypy src --platform win32` | **Success: no issues found in 454 source files** | <1 s (warm cache) |
| `python scripts/generate_api_reference.py --check` | **`docs/API.md`: in sync** | 2 s |
| `python scripts/novaforge_nf26_learning_eval.py` | **6/6 cases as expected; 0 false labels; 0 live-evidence claims**; store 24/24 rows exact round trip; memory 200 records / 322,534 bytes; report written; exit 0 | 2 s |
| `python scripts/novaforge_nf25_task_eval.py` | **7/7 fixtures as expected; 0 false completions**; exit 0 | 1 s |
| `python scripts/novaforge_nf22_autonomy_acceptance.py` | **16 steps, 0 failed**, `PASS WITH LIMITATIONS`, `live_gameplay: NOT_TESTED`; exit 0 | 4 s |
| `.nc_audit_workflows.py` (real `create_app()`, temporary data dir, no-op device runners) | **42/42 workflow steps passed**, 0 FAIL lines | 41 s |

The single skipped test is the whole of `tests/test_database.py`, skipped at import because the optional
`database` extra (`pip install -e .[database]`) is not installed in this environment
(`unittest.case.SkipTest: sqlalchemy not installed`, confirmed with `-rs`). It is an optional-dependency skip,
not a deselected or failing test, and no NF-22…NF-26 path depends on it.

**The skipped file was not the static surface.** `docs/API.md` is regenerated from the live routes, so a route
that ships undeclared fails the check; it did not.

### E.2 Per-phase requirement verdicts

Legend: **PASS** — implemented and demonstrated by a check run in this pass; **PARTIAL** — implemented, with a
stated part unexercised; **UNAVAILABLE** — cannot be demonstrated on this host or without operator approval;
**FAIL** — claimed but not true.

| Phase | Requirement | Verdict | Evidence in this pass |
|---|---|---|---|
| NF-22 | One bounded autonomous loop over the existing services | **PASS** | `test_novaforge_autonomy.py` 114 passed, 21 subtests; acceptance script 16/16 steps |
| NF-22 | Objective and completion criteria pinned before the first cycle | **PASS** | acceptance script's pinned-criteria step; `test_novaforge_autonomy.py` |
| NF-22 | Completion only from a fresh post-input reading | **PASS** | verification + autonomy suites; NF-22 report §9 |
| NF-22 | Nine declared terminal outcomes, verified failure distinct from unknown | **PASS** | `VocabularyTests`, lifecycle tests |
| NF-22 | Ending a run releases held input; no restart | **PASS** | lifecycle/recovery tests; acceptance script |
| NF-22 | Budgets stop the run exactly at their bound | **PASS** | `LimitTests` |
| NF-22 | Simulated and live evidence labelled | **PASS (fixed this pass)** | default was `live`; now `simulated`, pinned by `test_undeclared_provenance_is_never_reported_as_live` |
| NF-22 | Live gameplay | **UNAVAILABLE** | no launchable safe title, no approval; `live_gameplay: NOT_TESTED` |
| NF-23 | One installed title reliably identified | **PASS** | `GameCatalog.resolve` (report §9); `test_novaforge_library.py` green |
| NF-23 | Supported capture ladder, identity/freshness, invalid frames never live | **PASS** | `test_capture_backends.py` (1 465-test group), `test_novaforge_nf23.py`, `test_novaforge_observation.py` |
| NF-23 | Consecutive real-game frames | **UNAVAILABLE** | title's Steam API init fails on this host (measured, report §9); real-window frames were acquired instead |
| NF-23 | Failure propagates safely; no second capture subsystem | **PASS** | F-06/F-08 pins; `test_route_shape_contract.py` |
| NF-24 | State understanding from the live capture path | **PASS** | `test_novaforge_state.py`, `test_novaforge_nf24.py`, `test_perception.py` |
| NF-24 | A commercial-game profile | **UNAVAILABLE** | no profile for the blocked title; layouts the state layer can name only |
| NF-24 | Performance figures measured, not asserted | **PASS** | report §10; re-measured figures in §15 |
| NF-25 | Task spec, preflight refusals, judgement, evidence requirements | **PASS** | `test_novaforge_nf25.py`; eval 7/7 with 0 false completions |
| NF-25 | Action policy enforced; safety stop on breach | **PASS** | eval's `action_policy_exceeded` fixture; suite |
| NF-25 | Live objective execution | **UNAVAILABLE** | same two blockers (no title, no approval) |
| NF-26 | Trustworthy provenance on every record | **PASS (fixed this pass)** | `evidence_source`/`simulated` agreement enforced at the record; unlabelled autonomy runs no longer claim live |
| NF-26 | Uncertain outcomes never become success labels | **PASS** | eval: 0 false labels across 6 cases; `label_for_verdict` pins |
| NF-26 | Defined, reproducible comparison metrics and sample sizes | **PASS** | eval sets A–D; `MIN_TRIALS` = 5; Wilson intervals |
| NF-26 | Adaptation traceable and reversible | **PASS** | change records, `history()`, default rollback pin |
| NF-26 | Failure analysis separates hypothesis from verified cause | **PASS** | `FailureAnalysisTests` |
| NF-26 | Learning never gates the task it observes | **PASS (pinned this pass)** | `test_a_broken_recorder_never_fails_the_task` |
| NF-26 | Baseline-vs-adapted improvement under stated conditions | **PASS (simulation)** | evaluation set B: 0/6 vs 6/6, disjoint Wilson intervals, no safety metric worse |
| NF-26 | Live-game adaptation improvement | **UNAVAILABLE** | no approval, no launchable title |

No requirement was marked FAIL: every claim the reports make is backed by a check that ran in this pass, and
the three claims that could not run are marked UNAVAILABLE in both the reports and this table.

### E.3 Integration with the earlier phases

| Integration | Check | Result |
|---|---|---|
| NF-26 records NF-25's REAL report shape | drove the real `TaskRunner` (NF-25 gateway, one completed cycle) into `LearningService.record_task_report` | record parsed `progress`/`evidence_found`/`limits`/`resource_gate`/`initial`, label `verified_success`, `valid`, 2 dispatched / 1 responded, 7 evidence refs |
| NF-26's hint reaches the REAL planner | `StrategyHintPlannerTests` over NF-20 `GamePlanService` fixtures | a preferred kind moves ahead of a cheaper one, an avoided kind moves behind one, a 600 s hint is clamped to the policy's 10 s |
| NF-25 runs over NF-22's supervisor | `AutonomyTaskGatewayTests` + `ManagerWiringTests` | the adapter passes translated limits and the operator's approval; one runner per manager |
| NF-22 over NF-16…NF-21 | autonomy suite + acceptance script | plans through NF-20, acts only through NF-19, verifies against NF-17 readings |
| Every route declared, consumed, documented | `generate_api_reference.py --check`; `test_route_shape_contract.py`; `test_api_reference_sync.py` | in sync; shape contract green |
| The UI reads the same payloads the API sends | 42-step workflow pass (page, assets, chat, command plan/approve/execute, device plans, NovaForge surfaces, operator surfaces) + `test_novaforge_ui.py` | all 200/expected; no 5xx on the refusal paths |
| Token flow shared by desktop/browser/phone | workflow steps plan → execute → replay | executed once, replay refused with 403, phone returns pairing guidance with 200 |

### E.4 Defects found and fixed in this pass

1. **An unlabelled autonomy run was reported as live evidence** (`novaforge/autonomy.py`). The controller's
   `evidence_source` default was `"live"`, and `_run_from_dict` restored a stored run without the field as
   `"live"`. An undeclared run could therefore be counted as real-game evidence by NF-26 — the promotion
   NF-26 refuses inside its own module, done one layer below it. Both defaults are now `"simulated"`;
   production (the manager) and the acceptance harness already declare their class explicitly, so no real run
   changed label. *Pinned by* `test_undeclared_provenance_is_never_reported_as_live`.
2. **An omitted step list was recorded as "zero submitted steps"** (`novaforge/learning.py`). A cycle without
   an `execution` block contributed 0 to `actions_requested`, so a report that said it dispatched inputs either
   failed validation or claimed a measurement nobody took. An omission is now `None` (unmeasured) while an
   execution block that really carried no steps stays a measured `0`. *Pinned by*
   `test_a_cycle_without_its_step_list_is_unmeasured_not_zero` and
   `test_an_execution_block_with_no_steps_is_a_measured_zero`.
3. **The manager recorded the game's NAME as the record's profile** (`novaforge/manager.py`). `run_task` passed
   `profile=payload["game"]`, claiming a profile identity nothing observed. The record now takes the state
   layer's profile id from the run, and records none when the run named none. *Pinned by*
   `test_the_game_profile_is_the_state_layers_profile_not_the_game_name`.
4. **The "learning never gates the task" invariant was unpinned.** Added
   `test_a_broken_recorder_never_fails_the_task`: both `active_profile()` and `record_task_report()` raise, and
   the task's report comes back unchanged.
5. **Two autonomy tests asserted `"live"` from an undeclared controller** — they were relying on the unsafe
   default rather than declaring a class. They now declare `evidence_source="live"` explicitly; no assertion
   was relaxed or removed.

The fourteen defects from NF-26's own two verification passes (report appendix) were not re-opened: their pins
are in the suite and ran green in this pass's full-suite groups.

### E.5 User-level workflows (live application, not unit tests)

`create_app()` was booted exactly as the product starts it, with a temporary data directory and no-op device
runners, and 42 operator-level steps were exercised. All passed:

* the console page (200, `no-cache, no-store, max-age=0, must-revalidate` + `Pragma: no-cache`, injected asset
  and UI-version markers, sidebar present) and the five assets the page loads, each uncacheable;
* chat through the local scratch brain (`route: chat`);
* a desktop command planned (`waiting_for_approval`, token present), approved and executed through
  `/command/execute`, with a replay refused 403;
* a browser command planned (`browser_automation`) and executed through the same `/command/*` path;
* `/desktop/plan` and `/phone/plan` (the latter returning 200 `waiting_for_phone_bridge` with pairing steps,
  never a 403);
* ten NovaForge read surfaces (`status`, `sessions`, `state`, `objectives`, `observation`, `autonomy/trace`,
  `games`, `library`, `capabilities`, `windows`) and nine operator surfaces (`/status`, `/health`, `/activity`,
  `/tasks`, `/vision/status`, `/bugs`, `/intelligence`, `/capabilities`, `/diagnostics`);
* the failure paths with no game window: session creation refused 422 with a reason, unknown sessions 404,
  autonomy start refused 422 — no 5xx anywhere on a refusal path;
* the application persisted its own state under the data directory it was given.

### E.6 Not verified / UNAVAILABLE

1. **Live gameplay of any phase** — NF-22's loop, NF-23's frames, NF-24's commercial profile, NF-25's objective
   execution and NF-26's adaptation. Two independent blockers, unchanged: no launchable safe offline title on
   this host (NF-23's measured `SteamAPI_Init failed`), and no operator approval for live input.
2. **Real-game adaptation improvement** — the demonstration is simulation-based by construction.
3. **End-to-end observe→verify cycle latency on a real game** — the stage latencies exist separately.
4. **Cross-game / cross-version aggregation** — deliberately refused by design, so it is untested by intent.
5. **The single skipped test** — `tests/test_database.py`, skipped because the optional `sqlalchemy` extra is
   not installed; no failure is hidden behind it and no phase path uses that module.

### E.7 Compliance and verdict

* No test was skipped, deselected, weakened or deleted in this pass; four new pins were added and two tests were
  made to declare what they had been assuming. The one skip is pre-existing and environmental.
* No game was launched, no gameplay input was sent, no display mode changed and no process was force-killed.
* Nothing was committed, pushed, reset or stashed. The audit's scratch files (`.nc_audit_workflows.py`,
  `.nc_audit_nf25_nf26.py`) and the eval's `data/novaforge_nf26_learning_eval.json` are runtime/scratch data,
  not part of the product.
* **Verdict: PASS on NF-22…NF-26 as implemented and verified against fixtures, simulations and the live
  application surface.** PARTIAL only where a live game is required, and no such capability is claimed. The
  cross-phase seam that mattered — NF-25's real report feeding NF-26's ledger — was proven against the real
  payload, and the one provenance hole found between phases is closed and pinned.
* NF-27 has **not** been started; this audit is the stop condition for the NF-22…NF-26 milestone.
