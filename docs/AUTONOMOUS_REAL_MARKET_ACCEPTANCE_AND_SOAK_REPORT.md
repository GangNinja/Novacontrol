# Autonomous Runtime — Real-Market Acceptance and Soak Report

**Repository:** NovaControl / NovaMarket
**Predecessor report:** `docs/AUTONOMOUS_PAPER_TRADING_RUNTIME_INTEGRATION_REPORT.md` (AT-06)
**Date:** 2026-11-23
**Run window:** 22:30–23:37 IST (17:00–18:07 UTC), Monday
**Instance:** isolated, loopback-only, port 8021, data directory `tmp/acceptance_2026_11_23/workdir/data`
**Result:** **PARTIAL** — the runtime starts, holds a single lease, degrades correctly and handles provider/database faults, but **the automatic decision path does not work with real monitored instruments**, so no autonomous paper order, exit, or RL experience could be produced.

> **Remediation note (24 November 2026):** this document is the pre-remediation real-market baseline. D1–D9 were subsequently addressed in `AUTONOMOUS_DECISION_PATH_REMEDIATION_REPORT.md`, with fixture/integration evidence; the real-market run recorded here has **not** been repeated. The defect table and verdict below remain historical evidence, not a description of the current build.

---

## 0. Why this was not a green run

Three defects in the live path were reproduced, not inferred:

1. **AT-02 → AT-03 handoff is broken for normal instruments.** Monitoring records a candidate's `exchange` as the human venue (`"NSE"`), while its `instrument_id` carries the MIC (`"XNSE:RELIANCE"`). AT-03 drops every candidate whose exchange string is not the MIC prefix (`decision/pipeline.py:944-950`). A completed cycle with `processed_count=1` therefore produces `409 NO_ELIGIBLE_CANDIDATES`.
2. **Monitoring ignores the configured exchange and universe.** With `monitoring.exchange=XNYS` and `config.universe=["XNYS:AAPL"]`, the cycle reported an **open NYSE session** and processed **`XNSE:RELIANCE`** — an instrument on a closed exchange.
3. **There is no 20-minute interval.** `DataInterval` defines 1m/5m/15m/30m/1h/1d/1wk/1mo only. A `20m` ingest returns **HTTP 500** instead of a validation error, while the PI-12 policy advertises a `20m` short-term horizon in the UI.

Two further defects affect data completeness and operator control: XNYS daily bars are silently skipped, and "Resume" returns the runtime to `monitoring` only.

Because of (1) and (2), the honest classification for **automatic BUY/SELL proposals, NM-08 approval of a live signal, NM-10 execution, exit supervision of a real position, and RL experience from a completed trade** is **BLOCKED by defect**, not "passed".

---

## 1. Automatic startup — PASS

The application was started with no script, no manual runtime call, and no environment preset beyond disabling optional external subsystems:

```
python -m uvicorn novacontrol.api.app:app --host 127.0.0.1 --port 8021
```

On a **fresh store** the persisted policy default is `auto_start=true`, `auto_start_mode=autonomous`. Observed immediately after startup:

| Evidence | Value |
| --- | --- |
| `/market/autonomy/status` → `mode` | `autonomous` |
| `runtime_state` | `running` |
| `last_reason` | `application start` |
| `started_at` | `2026-11-23T17:00:30.705569+00:00` |
| `lease.held` / `valid` / `foreign` | `true` / `true` / `false` |
| `orders_possible` | `true` |
| `live_trading` / `broker_connected` | `false` / `false` |
| `/market/autonomy/health` → `health.state` | `ok` |
| Reliability → `scheduler_running` | `true` |

Dependencies initialised with the runtime: AT-02 monitoring (`initialised=true`, `eligible=false`, `MARKET_CLOSED` for XNSE), AT-03 decisions (`initialised=true`), AT-04 executions (`initialised=true`), AT-05 exits (`initialised=true`).

**No manual script execution was required.** The runtime enabled itself and constructed its critical pipelines before enabling the mode, exactly as the AT-06 report describes.

**Duplicate instances cannot start.** A second server was launched against the *same* data directory while the first held the lease:

| Instance | mode | state | lease held | foreign | conflict |
| --- | --- | --- | --- | --- | --- |
| primary :8021 | `autonomous` | `running` | `true` | `false` | `false` |
| duplicate :8022 | `disabled` | `degraded` | `false` | `true` | `true` |

The duplicate refused to auto-start and did not steal the lease. It was then stopped.

---

## 2. UI verification — PARTIAL

Driven in a real browser (Vite dev server on :5174 proxying the isolated backend; Chromium via the preview session).

**Verified working**

- Runtime status: mode `Autonomous`, runtime `Running`, health `Ok`, lease `Held`, paper execution `NM-10`, live trading disabled.
- Safety panel: `paper_only=true`, `live_trading=false`, `broker_connected=false`, `live_orders_possible=false`, learning promotion not implemented.
- **Pause**: mode → `Paused`; Resume appeared; decision and execution buttons became disabled with an explanation; protective exits explicitly remain active; toast `Autonomy pause request accepted.`
- **Resume**: accepted, toast/state updated, controls re-enabled.
- **Stop**: opens a confirmation dialog ("Stop the autonomy shell? … an already accepted NM-10 paper order is not cancelled or reversed"). Confirming produced mode `disabled`, state `stopped`, lease released, and `auto_start=0` persisted.
- Monitoring, decisions, predictions, outcomes, execution counts, exit supervision and RL panels all render and report the same values as the API (no client-side invention).

**Visible defects**

1. **"Exchange-local time" is not exchange-local.** With exchange `XNYS`, the panel displayed `23 Nov 2026, 10:35 pm` — the machine's IST clock — while the exchange was `12:35 pm` EST. "Next open" showed `08:00 pm` (IST) rather than 09:30 ET.
2. **Lease expiry is misleading at first paint**: `Expires just now` for a lease still valid for ~60 s; it corrects to "Expires in 1 minute" after a refresh.
3. **Resume cannot restore autonomous mode.** The only control is "Resume monitoring"; after a pause, the runtime returns to `monitoring`, not `autonomous`, so paper forwarding stays off until a separate explicit start. This is safe but materially changes operator semantics.
4. After pausing, the composed pipeline health reads `Degraded` and the scheduler `Idle` — defensible, but a paused-by-operator runtime is labelled with the same word as a fault.

Market data, positions, decisions, proposals, orders, forecasts and RL panels were all reachable; their content was empty because no trade occurred (see §4).

---

## 3. Real-market provider validation — PARTIAL

Provider: **Yahoo** (the only enabled provider for these instruments; CoinGecko is also enabled but was not used).

Connectivity measured directly and through the application:

| Probe | Result |
| --- | --- |
| `query1.finance.yahoo.com` chart AAPL | HTTP 200, 0.81 s |
| CoinGecko `/ping` | HTTP 200, 0.60 s |
| API ingest, 1m, two instruments | 200, 3.44 s, 499 bars |
| API ingest, 5m | 200, 3.61 s, 498 bars |
| API ingest, 15m | 200, 3.22 s, 498 bars |
| API ingest, 30m | 200, 3.71 s, 498 bars |
| API ingest, 1d | 200, 2.71 s, 247 bars |

**Instrument availability.** `XNSE:RELIANCE` (Yahoo `RELIANCE.NS`) and `XNYS:AAPL` (Yahoo `AAPL`) registered and resolved. Bars carry `provider`, `retrieved_at`, `source_timezone` and `currency`.

**Timestamp correctness and freshness** (newest bar vs retrieval time):

| Instrument | Interval | Newest bar (UTC) | Quality | Freshness |
| --- | --- | --- | --- | --- |
| XNYS:AAPL | 1m | `2026-11-23T17:01:00Z` | valid | delayed |
| XNYS:AAPL | 5m | `2026-11-23T16:55:00Z` | valid | delayed |
| XNYS:AAPL | 15m | `2026-11-23T16:45:00Z` | valid | delayed |
| XNYS:AAPL | 30m | `2026-11-23T16:30:00Z` | valid | delayed |
| XNSE:RELIANCE | 1m | `2026-11-23T09:59:00Z` | valid | closed_market |
| XNSE:RELIANCE | 1d | `2026-11-23T03:45:00Z` | valid | closed_market |

Quotes: AAPL `price=347.656`, `observed_at=2026-11-23T17:03:08Z`, `quality=valid`, `freshness=fresh`, `live=true`; RELIANCE `price=1351.0`, `live=false`, `freshness=closed_market`. The system distinguishes an open exchange from a closed one correctly.

**Candle completeness**

- 1m/5m/15m/30m: 248–250 stored bars per instrument, contiguous to the last completed session minute. **PASS for these intervals.**
- **20m: not supported.** No `MINUTE_20` exists; `POST /market/data/ingest {"intervals":["20m"]}` returns **HTTP 500** (`internal_server_error`), not a 4xx. Meanwhile the PI-12 policy advertises horizons `5m, 15m, 20m, 1h, 1d, 3d`, so the UI promises a horizon the data layer cannot store. **FAIL.**
- **XNYS daily bars are never ingested.** `XNYS:AAPL` daily count is **0**; a targeted ingest returns `status="skipped"`, `provider_attempts=0`, `bars_inserted=0`. A daily forecast for AAPL is then refused: `422 XNYS:AAPL has no usable 1d bars`. RELIANCE daily works (247 bars). **FAIL for XNYS daily completeness.**

**Provider interruption and recovery.** A controlled failure used a real but non-existent symbol (`ZZZFAILNOTREAL`) — no fabricated data:

- Ingest: `ok=false`, `instruments_unavailable=1`, `provider_failures=2`, error `yahoo: the provider did not find the requested symbol`, `bars_inserted=0`, no quote written.
- Immediate re-ingest of a valid instrument: `ok=true`, `provider_failures=0`, 5 bars inserted in 2.84 s.

**PASS for classification, refusal to store bad data, and recovery.**

---

## 4. Autonomous paper-trading validation — BLOCKED (by defect)

An isolated virtual book was created and funded inside the isolated data directory:

- Wallet `wal_2548d322d20c413ca5ba6080ace6a264`, deposit **1,000,000.00 INR** (idempotency key `acc-dep-1`).
- Portfolio `pf_9250622c48cd6446e0a7`, horizon `short_term`, capital **500,000.00**; valuation exactly `cash 500000.00`, `invested 0.00`, `total_value 500000.00`, `holdings 0`. No broker, no real money, no connection to the user's live database.

The live session was then genuinely open (NYSE, 12:03 ET). Monitoring ran through the production gate:

```
POST /market/autonomy/monitoring/run
→ 200 completed, session eligible=true, verdict=trading_session,
  discovered=1, eligible=1, processed=1, rejected=0
```

But the recorded candidate was:

```json
{"instrument_id":"XNSE:RELIANCE","exchange":"NSE","disposition":"processed","data_quality":"good"}
```

and AT-03 refused:

```
POST /market/autonomy/decisions/run
→ 409 NO_ELIGIBLE_CANDIDATES
  "monitoring cycle mon_… has no processed eligible candidates"
```

The candidate exists and is `processed`; `decision/pipeline.py:949` discards it because `"XNSE:RELIANCE".split(":")[0] != "NSE"`. **Automatic market analysis produced a monitoring cycle; automatic decision generation did not run.** No BUY or SELL proposal, therefore no NM-08 assessment of a live signal, no NM-10 order, no position, no exit and no ledger reconciliation of a trade.

To prove the downstream chain is not independently broken, the instrument's `exchange` metadata was normalised from `NSE` to `XNSE` (identity and provider symbol unchanged) and the cycle re-run:

```
monitoring → completed, candidate exchange=XNSE
decisions  → 200 completed, prediction_decisions=1,
             by_type {"BUY":0,"SELL":0,"HOLD":0,"WAIT":0,"NO_ACTION":1}
proposals  → 0
executions → 0 intents, 0 orders, 0 fills
```

So AT-03 executes and records a decision when its filter is satisfied. The decision was **NO_ACTION** because the real forecast's predicted return was `0.0`, below the `1.5%` minimum expected return — the system correctly declined to invent a trade.

**Classification:** automatic monitoring **PASS**; automatic decision **FAIL** (default metadata); BUY/SELL proposals, risk approval of a signal, execution, position supervision, exit execution and trade reconciliation **BLOCKED** — no qualifying real signal and no working default path.

**Additional session-gate defect:** with monitoring configured for `XNYS`, the cycle's session verdict was the **NYSE open** session while the instrument it processed was on **XNSE**, whose market was closed. The per-instrument session is not enforced at discovery.

---

## 5. Prediction and RL validation — PARTIAL

**Forecasts are persistently recorded.** Real forecasts were generated from stored provider bars:

| Request | Result |
| --- | --- |
| `XNYS:AAPL`, `1h`, 5m bars | 200, `sft_fd6350e5870cd0bf8bde`, ref `348.0775146484` at `16:55Z`, predicted return `0.0`, direction `neutral`, interval `[-0.00665778, 0.00192076]` |
| `XNSE:RELIANCE`, `1d` | 200, `sft_01cc43e8662b29691fec`, ref `1351.0`, predicted return `0.0` |
| `XNYS:AAPL`, `1d` | **422** — no usable 1d bars (see §3) |

**Mature forecasts are measured only after the horizon, from persisted observations.** The outcome endpoint returns `total=0` with `metrics.mae=null`, `rmse=null`, `brier_score=null`, `directional_accuracy=null`, `calculated_from="persisted prediction_decision_outcomes only"`, `automatic_promotion=false`. Nothing was invented for an unelapsed horizon — correct.

**Decision evidence** is stored with the forecast context: `pdec_149b4c2e90d35e33`, `decision_type=NO_ACTION`, `automatic_promotion=false`, `evaluation_context.available=false` with reason `no matching PI-11 evaluation report is stored`.

**RL experience from completed trades could not be produced** because no paper round trip completed. The exit-learning endpoint reports `verified_experiences=0`, `train/validation/test = 0/0/0`, `automatic_policy_promotion=false`, `training_performed=false`, `policy_modified=false`. RL status: `advisory_only=true`, `production_authority=false`.

**No RL improvement is claimed** — there is no out-of-sample evidence, and the system explicitly reports an empty sample rather than a score.

---

## 6. Multi-hour soak — PARTIAL (bounded run)

Planned duration: **4 hours**. Actually completed: **20.0 minutes** (sampler `duration_s=1200.6`; 20 samples spanning `2026-11-23T17:08:19Z` → `2026-11-23T17:27:19Z`). The multi-hour criterion is therefore **NOT MET**; this is a bounded soak, not an endurance run.

Conditions: one isolated instance, autonomous mode, real Yahoo provider, NYSE session open for the first ~3 hours of the planned window, NSE closed.

### Measured

| Metric | Result |
| --- | --- |
| Runtime uptime | 20.0 min continuous; `mode=autonomous` and `lease_valid=true` in 20/20 samples; 0 API errors |
| Health probe latency | median **12.5 ms**, min 6.2, max 45.1 (n=20) |
| Status latency | median **10.2 ms**, min 3.0, max 29.8 (n=20) |
| Process RSS | **377.2–389.7 MB**, median 387.8, final 387.2 — flat after warm-up |
| CPU consumed | **7.3 s** over 20 min (≈0.6% of one core); counter 10.1 s → 17.1 s |
| Provider latency (1m ingest) | **2.70 s / 3.21 s / 5.28 s**, all `ok=true`, 0 provider failures, 16 bars |
| Monitoring cycle latency | **36.4 / 46.1 / 39.6 ms**, all `completed`, session `trading_session` |
| Scheduler stability | `scheduler_running=true` throughout; **no autonomous tick fired** |
| Decision latency | n/a — no autonomous tick, and AT-03 blocked by §0 defect |
| Database growth | 7,679,720 → 7,827,176 bytes (**+147,456 B / +144 KiB** over 20 min) |
| Duplicate cycles | 0 overlapping cycles; 5 concurrent requests serialised, 30 ms apart |
| Order integrity | 0 intents, 0 orders, 0 fills, 0 duplicates |
| Portfolio reconciliation | `cash 500000.00`, `invested 0.00`, `total_value 500000.00`, `realized 0.00` |
| Failure domains | none recorded |

### Scheduler finding

`_run_runtime` sleeps **before** its first tick (`service.py:9209-9213`). At 22:30 IST the plan is `hourly` (outside the 09:00–17:00 window), so `last_tick` remained `null` and the first autonomous tick was not due until ~23:30 IST. During the entire run the scheduler was alive but **did not execute one cycle**. This is why the soak measures the runtime shell, lease renewal, API and provider path rather than autonomous cadence. It also means an application opened at 08:45 IST waits until ~09:30 for its first cycle, missing the open.

---

## 7. Failure and recovery — PASS

| Drill | Result |
| --- | --- |
| **Provider interruption** | Real invalid symbol → `unavailable`, 2 attempts, 0 bars, explicit error; next valid ingest succeeded in 2.84 s |
| **Application restart** | With `auto_start=0` persisted, the relaunched instance came up `mode=disabled`, `state=stopped`, `last_reason="safe restart"` — the emergency halt survived |
| **Explicit re-start** | `POST /market/autonomy/start {mode:autonomous, acknowledge_paper_only:true}` → `autonomous`/`running`, lease reacquired |
| **Database lock** | 8 s `BEGIN EXCLUSIVE` on the live database → monitoring answered **503** in 5.5 s, `/health` stayed **200**, and the next monitoring cycle after release completed in **0.1 s** |
| **Duplicate runtime** | Second instance refused; `foreign=true`, `conflict=true`; primary lease unaffected |
| **Graceful shutdown** | **PARTIAL** — Ctrl+C exited the process and `auto_start=1` survived, but no lifespan-shutdown lines were written and the still-valid 60 s lease blocked the next auto-start (D9) |
| **Emergency paper stop** | UI confirmation → `disabled`, lease released, `auto_start=0` persisted; no order cancelled or reversed |
| **Open positions abandoned** | None existed; `supervised_positions=0`, `open_exits=0`, `pending_orders=0` |
| **Duplicate orders** | None possible in this run; counters 0 and order-id idempotency is enforced by AT-04 design (not exercised end-to-end) |

**Paused semantics were verified**: paused mode disables new entries and decision runs in the UI/API while stating that protective exits and reconciliation remain active.

---

## 8. Acceptance criteria matrix

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1.1 | Intended auto-start configuration enabled | **PASS** | `auto_start=1`, `auto_start_mode=autonomous` on fresh store |
| 1.2 | Runtime initialises when the application opens | **PASS** | `mode=autonomous`, `last_reason="application start"` |
| 1.3 | Runtime health and dependencies initialise | **PASS** | health `ok`; AT-02…AT-05 `initialised=true` |
| 1.4 | No manual script required | **PASS** | Single uvicorn command; no script call |
| 1.5 | Duplicate runtime instances cannot start | **PASS** | Second instance `disabled/degraded`, `foreign=true`, `conflict=true` |
| 2.1 | Runtime status visible | **PASS** | UI and API agree |
| 2.2 | Start / pause / resume / stop controls | **PARTIAL** | All four work; resume cannot restore autonomous |
| 2.3 | Market-data health visible | **PASS** | Session, freshness, last attempt, next due |
| 2.4 | Portfolios, positions, decisions, proposals, orders, forecasts, RL visible | **PASS (empty, honest)** | Panels render; counts match API |
| 2.5 | No visual/functional defects | **FAIL** | Exchange-local time wrong; lease "just now"; resume downgrade |
| 3.1 | Provider connectivity | **PASS** | Yahoo 200, 0.81 s |
| 3.2 | Instrument availability | **PASS** | XNSE:RELIANCE, XNYS:AAPL |
| 3.3 | Data provenance | **PASS** | `provider=yahoo`, `retrieved_at`, `source_timezone` |
| 3.4 | Timestamp correctness | **PASS** | Newest AAPL 1m `17:01Z` vs retrieval `17:03Z` |
| 3.5 | Data freshness | **PASS** | `delayed` for open session; `closed_market` for NSE |
| 3.6 | 1m / 5m completeness | **PASS** | 248–250 bars each, contiguous |
| 3.7 | 20m completeness | **FAIL** | Interval does not exist; ingest `20m` → HTTP 500 |
| 3.8 | Provider interruptions handled | **PASS** | `unavailable`, 0 bars stored, error surfaced |
| 3.9 | Recovery after interruption | **PASS** | Next ingest 200, 5 bars, 2.84 s |
| 4.1 | Automatic market analysis | **PASS** | Cycle `completed`, `eligible=true`, `processed=1` |
| 4.2 | Automatic prediction generation | **PASS** | Forecasts recorded with identity and provenance |
| 4.3 | Automatic strategy selection | **PASS (data)** | AT-03 run completed after metadata normalisation; 0 strategies configured |
| 4.4 | Default AT-02 → AT-03 handoff | **FAIL** | `409 NO_ELIGIBLE_CANDIDATES` despite `processed=1` |
| 4.5 | BUY proposal | **BLOCKED** | Real forecast `0.0` return → NO_ACTION |
| 4.6 | SELL proposal | **BLOCKED** | No position exists |
| 4.7 | NM-08 risk approval | **BLOCKED** | No proposal to assess |
| 4.8 | NM-10 execution | **BLOCKED** | 0 intents / 0 orders |
| 4.9 | Portfolio updates | **PASS (no-op)** | Valuation exact: 500,000 cash, 0 invested |
| 4.10 | Position supervision / exit execution | **BLOCKED** | 0 supervised positions |
| 4.11 | Ledger reconciliation of a trade | **BLOCKED** | No trade; wallet/portfolio internally reconciled |
| 5.1 | Forecasts persistently recorded | **PASS** | `sft_…` records with ref price/time |
| 5.2 | Mature forecasts evaluated after horizon | **PASS (none due)** | Outcomes empty; metrics `null`, never fabricated |
| 5.3 | Errors / Brier recorded where applicable | **PASS (none due)** | `brier_score=null`, `calculated_from` names persistence |
| 5.4 | Completed trades generate verified RL experience | **BLOCKED** | No completed trade; `verified_experiences=0` |
| 5.5 | Rewards include costs and outcomes | **BLOCKED** | No experience produced |
| 5.6 | Experimental policies isolated | **PASS** | RL `advisory_only`, `production_authority=false` |
| 5.7 | `automatic_policy_promotion=false` | **PASS** | Asserted by API, metrics, learning and exit evidence |
| 5.8 | No RL improvement claimed | **PASS** | No out-of-sample evidence; report does not claim any |
| 6.1 | Multi-hour soak | **PARTIAL** | 20.0 minutes completed of a 4-hour plan |
| 6.2 | Uptime, memory, CPU measured | **PASS** | RSS 377–390 MB (median 387.8); 7.3 s CPU / 20 min |
| 6.3 | Provider latency measured | **PASS** | 2.70 / 3.21 / 5.28 s per ingest |
| 6.4 | Scheduler stability | **PARTIAL** | Alive, but no autonomous tick fired (hourly plan + sleep-first) |
| 6.5 | Decision latency | **BLOCKED** | No autonomous decision path (§0) |
| 6.6 | Database growth | **PASS** | +147,456 bytes over 20 min |
| 6.7 | No duplicate cycles | **PASS** | 5 concurrent requests serialised, 0 overlap |
| 6.8 | Order integrity | **PASS (vacuous)** | 0 orders, 0 duplicates |
| 6.9 | Portfolio reconciliation | **PASS** | Exact cash and zero P&L |
| 6.10 | Recovery behaviour | **PASS** | See §7 |
| 7.1 | Provider interruption | **PASS** | §7 |
| 7.2 | Application restart | **PASS** | Emergency halt survived |
| 7.3 | Scheduler interruption | **BLOCKED** | No endpoint or supported signal interrupts and re-arms the tick without restarting the process; restart covered the same task lifecycle |
| 7.4 | Database lock | **PASS** | 503, health up, recovery |
| 7.5 | Graceful shutdown | **PARTIAL** | Opt-in preserved, lease not released before exit |
| 7.6 | Emergency paper-trading stop | **PASS** | UI stop, durable |
| 7.7 | Open positions not abandoned | **PASS (vacuous)** | None existed |
| 7.8 | Duplicate orders impossible | **PASS (design/vacuous)** | 0 orders; idempotency not exercised |
| 8 | Report delivered | **PASS** | This document |

Tally: **PASS 40 · PARTIAL 4 · FAIL 3 · BLOCKED 10** (57 criteria).

---

## 9. Defects found

| # | Severity | Defect | Evidence |
| --- | --- | --- | --- |
| D1 | **High** | AT-03 drops monitored candidates when `exchange` is the venue name (`NSE`) rather than the MIC (`XNSE`); autonomous decisions never run with the default instrument registration | `decision/pipeline.py:944-950`; `409 NO_ELIGIBLE_CANDIDATES` after `processed=1` |
| D2 | **High** | AT-02 session gate and discovery disagree: `monitoring.exchange=XNYS` + `universe=[XNYS:AAPL]` still discovered and processed `XNSE:RELIANCE` on a closed exchange | cycle `mon_5857…`, candidate `exchange=NSE`, session `XNYS/open` |
| D3 | **High** | No 20-minute interval exists, yet the PI-12 policy advertises a `20m` horizon; unsupported ingest returns HTTP 500 instead of 4xx | `DataInterval` enum; `POST /market/data/ingest` `20m` → 500 |
| D4 | **Medium** | XNYS daily bars are silently skipped (`provider_attempts=0`), blocking daily forecasts for US instruments | AAPL 1d count 0; forecast `422 no usable 1d bars` |
| D5 | **Medium** | The scheduler sleeps before the first tick and uses the pre-tick plan, so no cycle runs at startup and the first live cycle can lag the open by up to the interval | `service.py:_run_runtime`; `last_tick=null` for the whole run |
| D6 | **Medium** | UI labels system-local time as "Exchange-local time" and shows IST for XNYS | UI: `XNYS` with `10:35 pm` while NY was `12:35 pm` |
| D7 | **Low** | "Resume monitoring" cannot restore the mode that was paused; autonomous forwarding stays off until a new start | `resume(mode)` requires an explicit mode; UI offers monitoring only |
| D8 | **Low** | Lease expiry reads "Expires just now" on first paint for a ~60 s lease | UI snapshot before refresh |
| D9 | **Medium** | After an unclean or interrupted termination the 60 s lease survives; a restart inside that window refuses auto-start, comes up `disabled`/`degraded`, and never retries. A normal restart 70 s later auto-started correctly (`reason="application start"`) | restart at +10 s → `disabled/degraded`, foreign lease; restart at +70 s → `autonomous` |

No defect was fixed during this acceptance run; the instruction was to establish whether the system operates, not to change it. Fixing D1–D4 would materially change the verdict and should be its own reviewed change.

---

## 10. Limitations of this evidence

- **No multi-hour endurance.** The completed run is 20.0 minutes. Memory/CPU trends over a session are not established.
- **No autonomous tick executed**, because the run was outside the NSE window and the plan was hourly. Scheduler cadence over a live open was not observed.
- **No paper round trip.** The real forecast was neutral, so no BUY/SELL existed; and the default decision path is blocked by D1. Execution, exits, trade reconciliation and RL experience are therefore unproven end-to-end with real data — they are covered only by the predecessor report's fixture-based acceptance test.
- **US-market configuration was used to obtain an open session.** The product's configured market is NSE; using `XNYS` exercised the same engine and calendar but is not the default deployment.
- **Instrument metadata was normalised once** (`exchange=NSE` → `XNSE`) solely to prove AT-03 runs; that workaround is disclosed, not hidden.
- **No broker, no live order, no real money, no automatic promotion** were used at any point.
- The user's own `data/` directory was not touched; all writes went to `novamarket/tmp/acceptance_2026_11_23/`.

---

## 11. Reproduction

```bash
cd novamarket
# isolated instance (own data dir, loopback only)
cd tmp/acceptance_2026_11_23/workdir
PYTHONPATH="../../../src" python -m uvicorn novacontrol.api.app:app --host 127.0.0.1 --port 8021

# UI
cd web && NOVAMARKET_API_ORIGIN=http://127.0.0.1:8021 \
  NOVACONTROL_ADMIN_TOKEN=<local admin token> npm run dev -- --port 5174

# bounded soak (sampler)
SOAK_PID=<server pid> SOAK_SECONDS=1200 python tmp/acceptance_2026_11_23/soak.py
```

Artifacts: `tmp/acceptance_2026_11_23/soak.jsonl`, `soak.log`, `server.log`, `server_restart.log`, `server_dup.log`, `vite.log`.

---

**Conclusion.** NovaMarket **does** start autonomously, hold a single durable lease, refuse duplicate instances, distinguish open from closed markets, ingest real provider bars with provenance, survive provider failure, database lock and restart, and expose all of it truthfully in the UI. It does **not**, in this build, carry a real monitored instrument from monitoring to a decision under default metadata: AT-02 and AT-03 disagree about what an exchange is, and discovery ignores the configured exchange. Until D1 and D2 are fixed, autonomous paper trading against real data cannot be accepted, regardless of the integration suite's 2,069 passing tests.
