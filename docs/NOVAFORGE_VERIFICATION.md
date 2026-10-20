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
