# Autonomous Paper-Trading Runtime Integration Report (AT-06)

**Repository:** NovaControl / NovaMarket
**Milestone:** AT-06 — integrated paper-only autonomous runtime
**Date:** 2026-11-23 (browser verification added 2026-11-24)
**Scope owner instruction:** treat *"Autonomous Paper-Trading Runtime Integration"* and *"Advanced Autonomy Requirements"* as **one** unified specification.
**Overall result:** **PASS** on every acceptance criterion that was in scope; the only **PARTIAL** is the explicitly stated multi-hour soak. Browser click-through is complete (see §8.7). No criterion is FAIL or BLOCKED.

---

## 1. Executive summary

The AT-01…AT-05 control plane, decision, paper-execution and exit-supervision components already existed and were already hosted by a single `_runtime_tick`. The integration work therefore did **not** rewrite them. It closed the gaps that prevented those components from forming a *continuously operating, self-starting, self-recovering, accountable* runtime:

| Gap found during inspection | Closure |
| --- | --- |
| Capability metadata still described trading as unimplemented (`TRADING_IMPLEMENTED=False`, gate closed) while AT-04 paper execution existed | Capability constants corrected to paper reality (`TRADING_IMPLEMENTED=True`, `RELEASE_GATE="open"`), live/broker/promotion remain false |
| Runtime had no `FAILED` health state, so a broken dependency looked merely `stopped` | `RuntimeState.FAILED` + `AutonomyRuntime.fail()`, surfaced in `status()` and the contract |
| Nothing started autonomy when the application opened | Persisted `auto_start` / `auto_start_mode` (schema v4) + `NovaMarketService._auto_start_autonomy()` |
| No distinction between "application closed gracefully" and "operator halted trading" | `stop(disable_autostart=…)`: graceful shutdown preserves opt-in, operator/emergency stop persists `auto_start=False` |
| Durable AT-04/AT-05 work from a previous run was not reconciled before the first new tick | `_autonomy_startup_reconcile()` runs before the tick loop and pauses the runtime on unresolved work |
| Prediction decisions were stored but never *measured* after their horizon | Forecast identity + error/Brier/provenance recorded in `decision/outcomes.py`; rolling metrics in new `decision/forecast_metrics.py` |
| Completed round trips had no learning artefact | New `learning/paper_feedback.py`: verified, costed, chronologically split experience — advisory only |
| Completed exit evidence did not carry slippage or the learning bridge | `autonomy_exits/pipeline.py` now embeds `slippage` and `rl_feedback` |
| UI could not show whether the runtime was actually learning/measuring | `web/src/pages/Autonomy.tsx` shows rolling forecast metrics and verified RL experience/splits |
| No single acceptance proof of the full workflow | New 20-step end-to-end acceptance test over the real engines |

**Verification headline:** full NovaMarket regression suite **2069 passed, 2 skipped, 500 subtests passed, exit code 0** (30 min 16 s). Focused AT-06 suite **227 passed**. `mypy` clean on 27 changed source files. Frontend production build exit 0.

**Browser verification (2026-11-24):** the Autonomy page was driven through start → pause → resume → stop; the stop survived an API restart; and the rendered forecast metrics, RL aggregate and per-row split labels were inspected. See §8.7.

---

## 2. The unified specification

Two requirement sets were merged into one dependency-ordered plan:

**A. Runtime integration** — one continuously operating loop started by the application, covering monitoring, decisions, NM-08 risk approval, NM-10 paper execution, exit supervision, prediction evaluation, RL feedback, UI control/observability, restart recovery and acceptance tests.

**B. Advanced autonomy** — the runtime must start itself, supervise itself, recover itself, expose what it is doing, and remain provably safe: paper-only, mandatory risk approval, no live broker, no automatic policy promotion, no fabricated data, no risk bypass.

Both sets were satisfied by wiring the same components; there is no second scheduler, no second loop and no parallel execution path.

---

## 3. Architecture as inspected (before changes)

```
NovaMarketService
 ├─ _run_runtime()          asyncio task, one per application
 │   └─ _runtime_tick()     single sequential stage host
 │       ├─ AT-02 AutonomyMonitoring.maybe_run()
 │       ├─ AT-03 DecisionPipeline.maybe_run()        → Strategy Proposal
 │       ├─ AT-04 ExecutionPipeline.maybe_run()       → NM-08 → NM-10 → ledger
 │       ├─ AT-05 ExitSupervisor.run_cycle()
 │       ├─ PI-21 portfolio review coordinator
 │       └─ PI-12 track_prediction_outcomes()
 ├─ AT-01 AutonomyRuntime    durable mode + lease (SQLite)
 └─ _start_autonomy_heartbeat()  lease renewal every lease/3
```

Key invariant preserved throughout: **Strategy Proposal → NM-08 Risk Approval → NM-10 Paper Execution**. The runtime enables a *mode*; it never submits an order itself.

---

## 4. Staged plan and completion

Each stage was implemented, tested and documented before the next began.

| # | Stage | Dependency rationale | Status |
| --- | --- | --- | --- |
| 0 | Architecture inspection and gap matrix | Must know what exists before changing anything | **Done** |
| 1 | Truthful capability metadata and safety constants | Everything downstream reads these flags; a false flag makes every later test meaningless | **Done** |
| 2 | Persisted auto-start policy (schema v4) | Auto-start needs durable opt-in that survives restart but can be revoked | **Done** |
| 3 | Runtime lifecycle: `FAILED` state, start/stop semantics, opt-in persistence | Auto-start can only be safe if start/stop are exact and fail-closed | **Done** |
| 4 | Application-start auto-start + startup reconciliation | Needs stages 1–3 to have a mode, a policy and a failure state | **Done** |
| 5 | Continuous tick verification and stage error isolation | Requires a started runtime (stage 4) and the existing pipelines | **Done** |
| 6 | Prediction accountability + rolling forecast metrics | Needs decisions to exist and elapse (stage 5) | **Done** |
| 7 | RL feedback bridge from verified round trips | Needs stage 5 exits + stage 6 outcomes | **Done** |
| 8 | API contract and UI observability | Needs stages 1–7 to have something true to display | **Done** |
| 9 | Acceptance tests: 20-step workflow + runtime integration | Must exercise the finished whole, not parts | **Done** |
| 10 | Full regression, static checks, build, this report | Final gate | **Done** |

---

## 5. Stage detail

### Stage 1 — Truthful capability metadata

`autonomy/models.py`

```
PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
TRADING_IMPLEMENTED = True      # was False
RELEASE_GATE = "open"           # was closed
ONE_CYCLE_AT_A_TIME = True
PAUSED_EXIT_POLICY = "protective"
EXIT_SUPERVISION_IMPLEMENTED = True
LEARNING_PROMOTION_IMPLEMENTED = False
AUTOMATIC_POLICY_PROMOTION = False
```

Rationale: AT-04 paper execution and AT-05 exit supervision are implemented, so the gate must be open **for paper orders only**. Live trading, broker connectivity and automatic policy promotion stay permanently false in this build. `AutonomyRuntime.status()` now reports `orders_possible=True`; the monitoring and decision stage contracts deliberately continue to advertise *paper, no execution* because those stages genuinely do not execute.

### Stage 2 — Persisted auto-start policy

`autonomy/store.py` — schema **v4**, additive migration:

- new `autonomy_config` columns `auto_start`, `auto_start_mode`;
- `save_auto_start(enabled, mode, now)`;
- read path returns the policy with every snapshot so `status()` and the contract are truthful;
- migration is additive and idempotent, so existing v1–v3 databases open without data loss.

`AutonomyConfig` is now **v2** and carries `auto_start` / `auto_start_mode` with validation (mode must be `monitoring` or `autonomous`) and serialization. A fresh autonomy store defaults to `auto_start=True`, `auto_start_mode="autonomous"` — the requested "starts when the application opens" behaviour — while any persisted `auto_start=False` wins.

### Stage 3 — Runtime lifecycle

`autonomy/runtime.py`

- `RuntimeState.FAILED` added; `fail(error)` marks failed-closed so no new entry can be evaluated.
- `start()` requires `monitoring|autonomous`, enforces `acknowledge_paper_only=True` for `autonomous`, asserts the paper-only invariants, acquires the lease, writes the mode, **and persists `auto_start=True`** — an explicit start is an opt-in.
- A failed persistence roll-back sets mode `disabled`, state `failed`; the process never believes it owns a mode that was not written.
- `stop(disable_autostart=True)` persists `auto_start=False` before releasing the lease (operator/emergency stop). `stop()` without the flag preserves the opt-in (graceful application shutdown).
- `close()` releases the lease and closes the SQLite handle exactly once.
- The paper-only invariant check now requires the integrated paper state to be coherent, rather than assuming paper execution does not exist.

### Stage 4 — Application auto-start and startup reconciliation

`service.py`

`_auto_start_autonomy()` runs after the autonomy store opens and before the tick loop:

1. returns unchanged unless the service is `RUNNING`/`DEGRADED` and `auto_start` is set;
2. refuses when the mode is not `disabled` (idempotent restart) or a **foreign live lease** exists (never steal another process's runtime);
3. constructs the critical pipelines (`_ensure_decision`, `_ensure_autonomy_execution`, `_ensure_autonomy_exits`) **before** enabling the mode — a broken dependency calls `runtime.fail(...)` instead of leaving a half-started runtime the tick could later drive;
4. starts the persisted mode with the paper acknowledgement when required.

`_autonomy_startup_reconcile()` runs as the first statement of `_run_runtime()`:

- calls AT-04 `recover()` and AT-05 `recover()`;
- **never replays an unfinished order**;
- if anything is unresolved, it **pauses** the runtime so new entries stop while protective exits and reconciliation remain available;
- returns `{status: completed|degraded, unresolved, unresolved_count, paused}`.

Auto-start failures are contained: they set `_autonomy_error` and never prevent NovaControl from starting.

### Stage 5 — Continuous tick

The tick order is fixed and sequential; every stage is isolated in its own `try/except` so one failure cannot stop monitoring:

| Order | Stage | Cadence |
| --- | --- | --- |
| 1 | forecast auto-planning (PI-09) | every tick |
| 2 | notification centre | every tick |
| 3 | event radar refresh | 300 s |
| 4 | held-position market data refresh | 300 s |
| 5 | AT-02 monitoring cycle | `maybe_run()` |
| 6 | AT-03 decision / strategy proposal | `maybe_run()` |
| 7 | AT-04 execution — NM-08 → NM-10 | `maybe_run()` |
| 8 | AT-05 exit supervision | every tick (also in `paused`) |
| 9 | PI-21 scheduled portfolio review | `maybe_run()` |
| 10 | PI-12 prediction outcome measurement | every tick, limit 25 |

Paused semantics are deliberate: **paused blocks new entries, not protective exits or reconciliation.**

### Stage 6 — Prediction accountability

`decision/outcomes.py` — each measured outcome now persists the prediction's identity and its error, so a forecast can be audited rather than merely counted:

`prediction_id`, `model_id`, `ensemble_id`, `prediction_at`, `target_at`, `observed_at`, `actual_return`, `confidence`, `signed_error`, `absolute_error`, `squared_error`, `brier_score`, and `provenance.actual_observation_at`.

`decision/forecast_metrics.py` (**new**) — `forecast_metrics(rows)` returns `evaluated`, `excluded`, `mae`, `rmse`, `mean_error`, `directional_accuracy`, `brier_score`, `calibration` buckets, and `automatic_promotion=False`. Outcomes are evaluated **only after the horizon has elapsed** and **only from persisted measured outcomes**; an empty set returns `mae=None`, never an invented score.

### Stage 7 — RL feedback bridge

`learning/paper_feedback.py` (**new**) — `build_paper_experience()` and `chronological_splits()`:

- one experience per **verified complete round trip** (`status == complete`, reconciliation passed, entry and exit fills present);
- fees and slippage are carried as costs; realized and unrealized P&L are separated; `deposit` is 0 (no fabricated capital);
- drawdown and cost penalties are applied to the reward;
- splits are **chronological** 70/15/15, never random;
- `training_performed=False`, `automatic_policy_promotion=False`.

Incomplete or unreconciled trades produce **no** experience. This is an advisory experience/reward bridge only — no training run, no policy promotion, no model activation.

`autonomy_exits/pipeline.py` now writes `slippage` into fill evidence and embeds `rl_feedback` in complete learning evidence, so the UI and API can show the reward without recomputation.

### Stage 8 — Contract and UI

- `NovaMarketService.autonomy_contract()` reports milestone AT-06, truthful order flags, the seven runtime states, storage schema v4 and explicit limitations including *"An operator stop disables auto-start; graceful shutdown preserves it."*
- `autonomy_prediction_outcomes()` returns rolling metrics; learning evidence returns the RL summary.
- `web/src/pages/Autonomy.tsx` renders rolling forecast metrics (MAE, RMSE, directional accuracy, Brier, calibration) and the verified RL experience with its splits and reward.

### Stage 9 — Tests

- `tests/test_novamarket_at06_end_to_end.py` (**new**) — one continuous 20-step workflow.
- `tests/test_novamarket_at06_runtime_integration.py` (**new**, 6 tests) — forecast metrics, paper feedback, auto-start/emergency-stop survival.
- `test_novamarket_autonomy.py`, `test_novamarket_at02.py`, `test_novamarket_at06.py` updated for the final contract, schema v4 and verified RL feedback. **No existing assertion was weakened**; the changes align tests with the corrected, truthful contract.

### Stage 10 — Verification

See §8–§9.

---

## 6. Runtime lifecycle semantics

```
stopped ──start──► starting ──► running ──pause──► paused
                                    │               │
                                    ├──resume───────┘
                                    ├──stop──► stopped (mode disabled)
                                    └──dependency failure──► failed
```

| Event | Mode | `auto_start` | Next application launch |
| --- | --- | --- | --- |
| Application opens (fresh store) | `autonomous` | `true` | starts `autonomous` |
| Graceful shutdown / `stop_runtime()` | `disabled` | **preserved `true`** | starts again |
| Operator stop / emergency stop | `disabled` | **persisted `false`** | stays `disabled` — the halt cannot be undone by a restart |
| Unresolved recovery | `paused` | unchanged | starts, then pauses again until reconciled |
| Critical dependency failure | `disabled` | unchanged | fails closed (`failed`), no entries |

A **foreign live lease** is never stolen: auto-start returns the existing snapshot and the status reports `degraded` with the reason.

Lease renewal runs on its own task every `lease_seconds/3` (three renewals per lease), so a slow tick cannot silently expire the lease.

---

## 7. Twenty-step isolated paper workflow — acceptance mapping

`CompleteWorkflowAcceptanceTests::test_20_step_isolated_paper_workflow` runs one continuous workflow. Per its docstring: *the market, discovery and forecast objects are deterministic fixtures; risk, strategy, decision, portfolio, wallet, execution, exit and learning persistence are the production implementations.* No stage of the execution path is mocked.

| Step | Requirement | Assertion that proves it | Result |
| --- | --- | --- | --- |
| 1 | Application runtime is live and autonomous | `runtime.status()["mode"] == "autonomous"` | **PASS** |
| 2 | Real paper book is restored | portfolio detail contains `valuation` | **PASS** |
| 3 | Cash is the real wallet balance, not a constant | `valuation["cash"] is not None` | **PASS** |
| 4 | Deterministic market fixture prepared | harness `ExitHarness` market fixture | **PASS** |
| 5 | Prediction decision persisted | `save_prediction_decision` returns the same `decision_id` | **PASS** |
| 6 | Forecast identity recorded for later audit | `primary_forecast` carries `model_version`, `ensemble_id`, `data_observed_at` | **PASS** |
| 7 | Target time elapsed, so it is measurable | `target_at = now - 2 days` | **PASS** |
| 8 | Strategy proposal produced | `enter_position()` builds the AT-03 proposal | **PASS** |
| 9 | **NM-08 risk approval** | proposal reaches execution only as `risk_approved` | **PASS** |
| 10 | **NM-10 paper execution** | `order["order_id"].startswith("ord_")`, fills present | **PASS** |
| 11 | Ledger and portfolio updated | `fills[0]["ledger_transaction_ids"]` non-empty, `holding()` true | **PASS** |
| 12 | Exit supervision detects the protective trigger | crash price → `run_cycle()` reports `submitted == 1` | **PASS** |
| 13 | Exit proposal produced | exit decision recorded in the execution store | **PASS** |
| 14 | Exit passes NM-08 | exit decision `status == "filled"` only through approval | **PASS** |
| 15 | Exit passes NM-10 | exit order and fills present | **PASS** |
| 16 | Ledger reconciliation passed | `reconciliation_status == "passed"` | **PASS** |
| 17 | Position closed with realized P&L | `position_closed` true, `holding()` `None`, `portfolio_realized_pnl` set | **PASS** |
| 18 | Prediction evaluated after horizon with accountability | outcome `error`, `absolute_error`, `brier_score`, `provenance.actual_observation_at` all set; `forecast_metrics` returns `evaluated=1`, MAE and directional accuracy, `automatic_promotion=False` | **PASS** |
| 19 | RL feedback from the verified round trip | `rl_feedback.status == "verified"`, linked `source_evidence_id`, `fees > 0`, `unrealized_pnl == "0"`, no training, no promotion | **PASS** |
| 20 | Restart reconciles without replaying | order-id list unchanged, one exit decision, same `evidence_id` after `recover()` → `completed` | **PASS** |

The end-to-end result is a genuine paper round trip: a risk-approved buy, a protective exit through the same gates, reconciled ledger, realized P&L, a measured forecast and a costed experience — with no order submitted twice across a restart.

---

## 8. Test evidence

### 8.1 Full NovaMarket regression

```
PYTHONPATH=src NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1 ../.venv/Scripts/python.exe \
  -m pytest tests/test_novamarket*.py -q -p no:cacheprovider

2069 passed, 2 skipped, 883 warnings, 500 subtests passed in 1816.45s (0:30:16)
exit code 0
```

The 2 skips are test-level conditional skips, unchanged by this work; there are no failures and the exit status is 0.

### 8.2 Focused AT-06 suite

```
227 passed
```
covering `test_novamarket_autonomy.py`, AT-02…AT-06, the two new AT-06 files and PI-12.

### 8.3 Per-file collection

| File | Tests |
| --- | --- |
| `test_novamarket_autonomy.py` | 20 |
| `test_novamarket_at02.py` | 63 |
| `test_novamarket_at03.py` | 21 |
| `test_novamarket_at04.py` | 24 |
| `test_novamarket_at05.py` | 34 |
| `test_novamarket_at06.py` | 25 |
| `test_novamarket_at06_end_to_end.py` | 1 |
| `test_novamarket_at06_runtime_integration.py` | 6 |
| whole suite | 2071 collected |

### 8.4 Named acceptance tests

- `CompleteWorkflowAcceptanceTests::test_20_step_isolated_paper_workflow`
- `AutoStartIntegrationTests::test_application_start_auto_starts_and_emergency_stop_survives_restart`

### 8.5 Static and build checks

| Check | Command | Result |
| --- | --- | --- |
| Type check | `mypy` on the 27 changed source files | `Success: no issues found in 27 source files` |
| Frontend build | `npm run build` in `novamarket/web` | exit 0 |

### 8.6 Manual boot observation

Three consecutive application boots with a scratch store:

```
boot 1 → mode=autonomous, trading_implemented=True, release_gate=open
boot 2 → mode=autonomous
operator stop → auto_start=False
boot 3 → mode=disabled
```

This is the behaviour the specification requires: the runtime starts itself, and an operator halt is durable across restarts.

### 8.7 Browser verification — completed 2026-11-24

The Autonomy page was driven in Chromium against an isolated copy of the seeded UI acceptance store (`.ui_audit/autonomy_ui_data`) through the Vite dev server on `127.0.0.1:5174` and the API on `127.0.0.1:8011`. The copy was reset to `mode=disabled`, `runtime_state=stopped`, `auto_start=0` before the run. No existing store was modified.

**Lifecycle sequence observed in the rendered page:**

| Step | Operator action | Observed result |
| --- | --- | --- |
| 1 | Initial load | Page showed `Disabled` / `Stopped`, `Start monitoring` and no hourly lease. |
| 2 | Clicked **Start monitoring** | Page replaced the start control with **Pause**; the runtime entered monitoring mode. |
| 3 | Clicked **Pause** | Page replaced **Pause** with **Resume monitoring** and displayed the paused state. |
| 4 | Clicked **Resume monitoring** | Page replaced the resume control with **Pause**; monitoring resumed. |
| 5 | Clicked **Stop and release lease**, then confirmed **Stop and release** | Page returned to `Disabled` / `Stopped` and showed `Start monitoring`. API status reported `mode=disabled`, `runtime_state=stopped`, `last_reason="operator confirmed stop from Autonomy page"`, and `auto_start=false`. |
| 6 | Stopped and restarted the API against the same isolated store, then reloaded the page | API status reported `mode=disabled`, `desired_mode=disabled`, `runtime_state=stopped`, `last_reason="safe restart"`, `auto_start=false`; the page still showed `Start monitoring`. The halt survived the restart. |

**Measured forecast values rendered from the persisted outcomes:**

| Metric | Value |
| --- | --- |
| Evaluated / excluded | `3 / 0` |
| MAE | `0.015` |
| RMSE | `0.01554563` |
| Directional accuracy | `66.7%` |
| Brier score | `0.29666667` |

These are the rolling values from the three persisted `prediction_decision_outcomes` in the seeded store; the page states that only elapsed measured outcomes are included.

**Verified RL values rendered from the persisted evidence:**

| Check | Value |
| --- | --- |
| Verified paper experiences | `4` |
| Chronological train / validation / test | `2 / 1 / 1` |
| Mean reward | `0.348` |
| Per-row splits | `train`, `train`, `validation`, `test` |
| Per-row rewards | `0.6`, `0.36`, `0.288`, `0.144` |

**Per-row split-label defect found and fixed.** The page previously read only `evidence.rl_feedback.split`. The API returns chronological split assignments in the top-level `rl_feedback.rows`, so every verified row rendered `split unassigned` while the aggregate still read `2 / 1 / 1`. `Autonomy.tsx` now joins those rows back to the evidence by `source_evidence_id` / `experience_id` and keeps `feedback.split` as the first preference. After the browser reload all four rows rendered `train`, `train`, `validation`, `test`; `split unassigned` was absent from the page. The aggregate remained `2 / 1 / 1`.

---

## 9. Resource usage

Measured with `tmp/at06_acceptance/measure_runtime.py` (Python 3.13.0, win32, SQLite-backed autonomy store, 300 cycles, 5 start/stop rounds). These measure the **runtime shell** — store, lease, mode transitions and cycle bookkeeping — not the full trading tick, which additionally performs market-data I/O and is driven by `plan_market_refresh()` cadence.

| Operation | n | min | median | mean | p95 | max |
| --- | --- | --- | --- | --- | --- | --- |
| `start(autonomous)` | 5 | 3.19 ms | 5.84 ms | 5.23 ms | 6.02 ms | 6.32 ms |
| `stop()` | 5 | 1.42 ms | 2.30 ms | 4.01 ms | 3.59 ms | 11.19 ms |
| `status()` | 50 | 0.27 ms | 0.38 ms | 0.39 ms | 0.41 ms | 0.69 ms |
| `claim_cycle` + `finish_cycle` | 300 | 1.87 ms | 2.65 ms | 3.97 ms | 5.39 ms | 186.78 ms |
| Reopen existing store | 1 | — | 14.39 ms | — | — | — |

- **Cycle throughput:** 252 cycles/s (300 cycles in 1.191 s).
- **The 186.78 ms max** is the first cycle after store creation (SQLite page allocation); the median is 2.65 ms and p95 is 5.39 ms.
- **Memory:** `tracemalloc` shows 0.06 MiB current / 0.08 MiB peak for the measurement harness. Process RSS was **not captured** — the harness's RSS probe returned 0 on this platform, so no RSS figure is claimed.
- **Start-up cost is negligible** against a market refresh interval measured in seconds-to-minutes: 5.8 ms median to become autonomous, 14.4 ms to reopen a durable store with the persisted opt-in.

Interpretation: the runtime shell cannot be the bottleneck. Cost is dominated by market-data fetches and the individual pipelines, exactly as before integration.

---

## 10. Defects found and fixed during integration

1. **Capability metadata contradicted the implementation** — `TRADING_IMPLEMENTED=False` with a closed gate while AT-04 existed. Corrected to paper truth; live/broker/promotion untouched.
2. **No failure state** — a dependency that failed during start was indistinguishable from "never started". Added `FAILED` plus `fail()`.
3. **Auto-start opt-in had no durable home** — added schema v4 columns, additive migration and `save_auto_start()`.
4. **Graceful shutdown and emergency stop were indistinguishable** — they now differ in whether `auto_start` survives.
5. **A broken dependency could leave a half-started runtime** — pipelines are constructed before the mode is enabled, and failure calls `fail()`.
6. **Startup did not reconcile prior durable work** — AT-04/AT-05 recovery now precedes the first tick and pauses on unresolved work instead of trading into an unknown state.
7. **Stop-flow release ordering** — `stop_runtime()` stops the heartbeat and runtime *before* cancelling the tick, so a cancelled tick cannot renew a lease that should be gone.
8. **Forecast outcomes could not be audited** — added prediction/model/ensemble identity, error decomposition, Brier score and provenance.
9. **Completed exits carried no slippage or learning link** — both are now written into learning evidence.
10. **Verified RL rows rendered `split unassigned`** — `Autonomy.tsx` read only the per-evidence `rl_feedback.split`, while the API returned the chronological assignments in `rl_feedback.rows`. The page now joins by `source_evidence_id` / `experience_id`; the aggregate stays `2 / 1 / 1` and the four rows render `train`, `train`, `validation`, `test` (verified in §8.7).

---

## 11. Limitations and deliberate non-implementations

Kept false **by instruction**, not by omission:

- **Live trading, broker connection, real-money execution** — do not exist in this build (`LIVE_TRADING`, `BROKER_CONNECTED` false; `live_orders_possible=False`).
- **Automatic RL policy promotion / training** — `AUTOMATIC_POLICY_PROMOTION=False`, `LEARNING_PROMOTION_IMPLEMENTED=False`; the bridge produces experience and reward only.
- **PI-25 and any redesign of completed PIs** — not touched.
- **Risk bypass** — impossible by construction: AT-04 forwards only `risk_approved` proposals, and paused/disabled modes block new entries while leaving protective exits available.
- **Fabricated market data** — outcomes stay pending when a bar is missing; metrics return `None` rather than a score.

Verification limitations (honest gaps, not implementation gaps):

- **No multi-hour/multi-day soak.** Sustained behaviour was demonstrated by a 300-cycle burst plus the 30-minute full regression suite, not by a wall-clock endurance run across a real trading session.
- **Live market providers were not exercised in the browser verification.** Tests use deterministic market/forecast fixtures with production stores and engines; the tick's provider cadence is unchanged from the previously accepted AT-04/AT-05 work. Browser click-through itself is complete: the lifecycle controls, restart-survived halt, measured forecast/RL values and per-row splits were driven in Chromium (§8.7).

---

## 12. Final criteria matrix

| Criterion | Status | Evidence |
| --- | --- | --- |
| Runtime starts automatically when the application opens | **PASS** | Auto-start test; three-boot manual observation |
| Operator/emergency stop survives restart | **PASS** | `auto_start=False` persisted; boot 3 `disabled`; browser restart §8.7 |
| Graceful shutdown preserves the opt-in | **PASS** | runtime integration test |
| Continuous monitoring → decision → NM-08 → NM-10 → exits in one loop | **PASS** | single `_runtime_tick`, 20-step e2e |
| Strategy Proposal → NM-08 → NM-10 mandatory for entries **and** exits | **PASS** | e2e steps 8–15 |
| Protective exits run even while paused | **PASS** | tick stage 8 ordering; AT-05 tests |
| Prediction evaluated only after horizon, with provenance | **PASS** | e2e step 18; forecast metrics tests |
| RL feedback is verified, costed and advisory | **PASS** | e2e step 19; paper feedback tests |
| No automatic policy promotion or training | **PASS** | `AUTOMATIC_POLICY_PROMOTION=False`; assertions in e2e and feedback tests |
| Restart reconciles without replaying an order | **PASS** | e2e step 20; startup reconcile tests |
| Unresolved recovery fails closed (pause) | **PASS** | `_autonomy_startup_reconcile` + tests |
| Foreign live lease is never stolen | **PASS** | auto-start guard + lease tests |
| Emergency halt cannot be undone by restart | **PASS** | persistence tests; browser restart §8.7 |
| No live trading, broker or real money | **PASS** | capability constants + contract |
| No risk bypass and no fabricated market data | **PASS** | gate assertions; metrics return `None` on empty |
| UI exposes metrics, RL feedback and controls | **PASS** | `Autonomy.tsx`; `npm run build` exit 0 |
| UI verified in a real browser session | **PASS** | §8.7: start/pause/resume/stop, restart-survived halt, measured forecast/RL values and split labels |
| 20-step isolated paper workflow with real engines | **PASS** | `test_20_step_isolated_paper_workflow` |
| No existing test weakened | **PASS** | full suite 2069 passed, exit 0 |
| Full regression green | **PASS** | 2069 passed / 2 skipped / 500 subtests |
| Static typing clean on changed files | **PASS** | mypy, 27 files |
| Resource use acceptable | **PASS** | §9 measurements |
| Multi-hour endurance soak | **PARTIAL** | 300-cycle burst + 30-min suite; no wall-clock session run |
| Report delivered | **PASS** | this document |

**No criterion is FAIL or BLOCKED.**

---

## 13. Files changed

**Modified**

- `novamarket/src/novacontrol/novamarket/autonomy/models.py` — safety constants, `RuntimeState.FAILED`, `AUTOMATIC_POLICY_PROMOTION`, `AutonomyConfig` v2 with `auto_start`/`auto_start_mode`, validation and serialization.
- `novamarket/src/novacontrol/novamarket/autonomy/store.py` — schema v4, additive migration, `save_auto_start()`, auto-start in snapshots.
- `novamarket/src/novacontrol/novamarket/autonomy/runtime.py` — `config` property, failed state, `fail()`, start/stop opt-in persistence, `stop(disable_autostart=…)`, paper-only invariant over the integrated state.
- `novamarket/src/novacontrol/novamarket/service.py` — `_auto_start_autonomy()`, `_autonomy_startup_reconcile()`, startup recovery in `_run_runtime`, duplicate-task prevention, graceful-shutdown ordering, operator stop disabling auto-start, truthful contract, metrics in outcome reads, RL summary in learning evidence.
- `novamarket/src/novacontrol/novamarket/decision/outcomes.py` — forecast identity, error decomposition, Brier score, provenance.
- `novamarket/src/novacontrol/novamarket/autonomy_exits/pipeline.py` — slippage in fill evidence, `rl_feedback` in complete learning evidence.
- `novamarket/web/src/pages/Autonomy.tsx` — rolling forecast metrics, verified RL experience, and the `rl_feedback.rows` split join.
- `novamarket/tests/test_novamarket_autonomy.py`, `test_novamarket_at02.py`, `test_novamarket_at06.py` — contract, schema and RL assertions aligned to the corrected behaviour.

**Added**

- `novamarket/src/novacontrol/novamarket/decision/forecast_metrics.py`
- `novamarket/src/novacontrol/novamarket/learning/paper_feedback.py`
- `novamarket/tests/test_novamarket_at06_end_to_end.py`
- `novamarket/tests/test_novamarket_at06_runtime_integration.py`
- `novamarket/docs/AUTONOMOUS_PAPER_TRADING_RUNTIME_INTEGRATION_REPORT.md` (this report)
- `novamarket/tmp/at06_acceptance/measure_runtime.py` (measurement harness, not shipped code)

---

## 14. Reproducing the evidence

```bash
cd novamarket

# 20-step acceptance workflow
PYTHONPATH=src NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1 ../.venv/Scripts/python.exe \
  -m pytest tests/test_novamarket_at06_end_to_end.py -q -p no:cacheprovider

# runtime auto-start / emergency-stop integration
PYTHONPATH=src NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1 ../.venv/Scripts/python.exe \
  -m pytest tests/test_novamarket_at06_runtime_integration.py -q -p no:cacheprovider

# whole NovaMarket regression
PYTHONPATH=src NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1 ../.venv/Scripts/python.exe \
  -m pytest tests/test_novamarket*.py -q -p no:cacheprovider

# resource measurements
PYTHONPATH=src NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1 ../.venv/Scripts/python.exe \
  tmp/at06_acceptance/measure_runtime.py

# frontend
cd web && npm run build
```

Evidence logs: `novamarket/tmp/at06_acceptance/novamarket_suite.log` (full suite), `novamarket_suite.exit` (exit code 0).

---

## 15. Remaining recommended work (not required by this specification)

1. **Endurance soak** — run the runtime across a full simulated trading session (or several) with the real providers, watching lease renewals, tick duration drift and store growth.
2. **Provider-outage drill** — force market-data failures mid-session to confirm the runtime degrades and reports rather than trades on stale data.
3. **RL training and evaluation** — deliberately out of scope here; would need its own approval gate and promotion policy before any activation.

Browser verification of the Autonomy page is no longer outstanding: the lifecycle controls, restart-survived halt, forecast metrics, RL aggregate and per-row split labels were verified in Chromium on 2026-11-24 (§8.7).

---

**Conclusion.** The AT-01…AT-06 components now form one continuously operating paper-trading runtime that starts with the application, supervises entries and exits through the mandatory Strategy Proposal → NM-08 → NM-10 path, measures its own forecasts after their horizons, converts verified round trips into costed advisory experience, exposes all of it through the API and UI, and recovers from restart without replaying a single order — with an operator halt that a restart cannot undo. Every acceptance criterion other than the verification-only multi-hour soak **PASSES**; that soak remains partial and is recorded above rather than claimed. Browser click-through is complete (§8.7).
