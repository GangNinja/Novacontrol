# NovaMarket — AT-02 Session-Aware Monitoring and Discovery

**Milestone:** AT-02 (second milestone of the approved Autonomous Trading Architecture)
**Worktree:** `novamarket` — branch `novamarket/nm-01`
**Date:** 10 November 2026
**Verdict:** **PASS**

AT-02 delivers verified-session monitoring and instrument discovery, persisted in
SQLite, hosted by the single AT-01 runtime tick. It has **no reachable path** to a
signal, proposal, order, fill, risk approval, portfolio mutation or ledger write.

---

## 1. Prerequisite gate — AT-01

AT-01 was accepted at **PASS** before AT-02 work began:

- `docs/AT-01_IMPLEMENTATION_REPORT.md` — PASS, 153 focused tests, mypy clean.
- AT-01 runtime, lease, operator permission and 7 routes were already present.
- The AT-01 persistence layer was migrated **additively** from schema v1 → v2 → v3;
  no AT-01 table or row was rewritten. Existing v1/v2 databases gain the new
  monitoring tables and the `data_quality` column in place.

The AT-01 acceptance test for the schema now asserts **v3** and the eight tables
(4 AT-01 + 4 AT-02); every other AT-01 test remains unchanged except the removal of
the over-broad `discovery` import prohibition, because AT-02 is required to compose
the NM-05 discovery engine. No financial engine import is permitted in either case.

---

## 2. Delivered architecture

| Component | Purpose |
|---|---|
| `autonomy/session.py` | Deterministic, timezone-aware session evaluator over the verified NM-02 calendar. |
| `autonomy/monitoring.py` | The only AT-02 runner: mode/lease/session gates, NM-05 discovery call, normalization, 20-instrument rotation, persistence, health. |
| `autonomy/store.py` | Additive schema v3: monitoring config, state, cycles, candidates + persisted `data_quality`. |
| `autonomy/runtime.py` | AT-01 runtime, unchanged behaviour, now exposes its store to the monitoring runner. |
| `service.py` | Hosts `maybe_run()` inside `_runtime_tick`; exposes session/monitoring/cycle reads and operator-gated run/config. |
| `api/app.py` + registries | 7 new authenticated routes; two mutations require `X-Autonomy-Operator-Token`. |
| `web/src/pages/Autonomy.tsx` | Session/calendar card, scheduler/rotation/data-freshness card, cycle history, operator-gated manual run. |

**No second scheduler.** `maybe_run()` is awaited from the existing `_runtime_tick`
after the notification/market refresh work. There is no new asyncio task, thread,
Redis, Celery or external queue.

**No second discovery engine.** AT-02 calls
`AssetDiscoveryEngine.run_cycle(force=True, trigger=TriggerKind.MANUAL)` and reads
`AssetDiscoveryEngine.candidates(limit=500)`.

---

## 3. Requirement-by-requirement acceptance matrix

| Requirement ID | Requirement | Implementation evidence | Verification evidence | Verdict |
|---|---|---|---|---|
| AT2-01 | AT-01 prerequisite PASS | `docs/AT-01_IMPLEMENTATION_REPORT.md`; no AT-01 behaviour replaced | 77 combined AT-01/AT-02 tests pass | PASS |
| AT2-02 | Use the verified exchange calendar, no duplicate engine | `session.py` imports `calendar_by_mic`, `ExchangeCalendar`, `SessionState`, `calendar.state()` | Calendar tests: NSE/BSE open, holiday, weekend, pre/post session | PASS |
| AT2-03 | Explicit calendar coverage/verification, fail closed | `evaluate_session` returns `CALENDAR_UNAVAILABLE` / `CALENDAR_UNVERIFIED`; monitoring records a `deferred` cycle and never calls discovery | 2027 date → `CALENDAR_UNVERIFIED`; `XNOT` → `CALENDAR_UNAVAILABLE`; holiday → `EXCHANGE_HOLIDAY` | PASS |
| AT2-04 | Exchange-local, timezone-aware timestamps only | `calendar.local(moment)`, IANA `Asia/Kolkata`, reject naive datetimes | `+05:30` local time; naive datetime raises `ValueError` | PASS |
| AT2-05 | Distinguish all session states | `SessionVerdict`: trading/continuous, pre-open, closed, weekend, holiday, unavailable, unverified, unknown; stable `SessionReason` codes | 11 session tests; live UI showed `Exchange Holiday` | PASS |
| AT2-06 | Deterministic, injected-clock evaluator | All monitoring/status calls take `clock`; `_now()` requires tz-aware results | Boundary tests 09:15/15:30 IST; 2026-11-11 04:00 UTC open | PASS |
| AT2-07 | No duplicate runtime/scheduler; host in AT-01 lifecycle | `AutonomyMonitoring` constructed once per runtime; `_ensure_monitoring` reuses; `_runtime_tick` awaits `maybe_run` | Shutdown/restart test: boot `disabled`, scheduler `false`, no duplicate cycles | PASS |
| AT2-08 | Mode gating: disabled / monitoring / autonomous / paused | `run_cycle` and `maybe_run` refuse `RUNTIME_DISABLED`, `RUNTIME_PAUSED`; autonomous runs monitoring only | 4 mode tests; autonomous result carries `trading_implemented=false`, `orders_possible=false` | PASS |
| AT2-09 | Lease ownership + one cycle at a time | `_lease_valid(runtime)` gate; `runtime.claim_cycle`; single monitoring lock | Expired lease → `LEASE_INVALID`, zero cycles; concurrent call → `ALREADY_RUNNING`, discovery not called | PASS |
| AT2-10 | Graceful shutdown, cancellation, retry, timeout | `AutonomyRuntime.close`; `recover()` marks running monitoring cycles `failed / CYCLE_CANCELLED`; `asyncio.wait_for`; bounded retries | Recovery test, timeout test (`CYCLE_TIMEOUT`), transient retry test | PASS |
| AT2-11 | Validated, persisted scheduler config | `MonitoringConfig`: interval 900–86400, batch 1–20, retries 0–5, timeout 5–600, non-blank exchange; stored in `autonomy_monitoring_config` | Config bounds tests; persistence/reload test; API 422 on unsafe values | PASS |
| AT2-12 | Integrate the existing NM-05 engine | `run_cycle` + `candidates`; `TriggerKind.MANUAL`; no duplicate discovery | `RealDiscoveryEngineIntegrationTests` drives the real engine/store and produces `XNSE:ALPHA` | PASS |
| AT2-13 | Normalize, validate, dedupe, reject, preserve provenance | `_normalise`: required `instrument_id`, uppercase exchange, dedupe set, NM-02 lookup when available, rejection reasons | Duplicate/blank → rejected; unknown instrument lookup → `INVALID_INSTRUMENT`; symbols filled from NM-02 | PASS |
| AT2-14 | 20-instrument cap and deterministic fair rotation | `BATCH_CAP=20`; `_rotate` slices from persisted cursor; `set_rotation` after success only | 1, 20, 25, 45-instrument tests; 45 → 20/20/5; cursor persists across instances | PASS |
| AT2-15 | New/removed instruments, restart-safe rotation | Cursor is clamped/reset on shrink; new IDs enter later batches; rotation state in SQLite | New instrument processed in a later cycle; removed set resets safely; failure does not advance cursor | PASS |
| AT2-16 | Persistent cycle lifecycle with required fields | `create/update_monitoring_cycle`; phases created → calendar_validation → discovery → eligibility_filtering → batch_preparation → monitoring → completed; deferred/failed/cancelled | Cycle persistence/counts/candidates/detail tests | PASS |
| AT2-17 | Recovery, duplicate prevention, safe migration | `recover()` / `recover_monitoring()` interrupt active cycles as `failed`; `cycle_key` unique; additive `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` | Interrupted-cycle recovery test; v1→v3 migration preserves an AT-01 marker; 8-table schema test | PASS |
| AT2-18 | Market-data freshness/provenance, no fabrication | Curated candidate `data_quality` from NM-05 `evaluation.data_quality` (`good`/`partial`/`stale`/`insufficient`); status maps to fresh/delayed/stale/unavailable; unavailable ≠ zero price | Real-engine test asserts a persisted quality; status freshness test; no price is computed or synthesized anywhere in AT-02 | PASS |
| AT2-19 | Minimum APIs, authorization, validation, docs | 7 routes: session, monitoring contracts/status/cycles/cycles/{id}, run, config; operator token on mutations; 401/403/404/409/422 paths | API tests; live 401/403/404 checks; `docs/API.md` regenerated (600 routes, in sync) | PASS |
| AT2-20 | UI status accuracy and error visibility | Autonomy page session/scheduler/rotation/freshness/cycles; disabled buttons unless operator verified; distinct holiday/deferred/failed states | Live UI: XNSE, 2026-11-10, Exchange Holiday, verified calendar, next open 11 Nov 09:15, 900 s, batch 20, deferred cycle `EXCHANGE_HOLIDAY`; no console/network errors after token | PASS |
| AT2-21 | Structured observability and categorized failures | `MonitoringErrorCode` codes; cycle `error_code`/`error`; status `last_error*`, `consecutive_failures`, health reasons | Failure tests cover provider failure, timeout, invalid instrument, no candidates; live deferred reason | PASS |
| AT2-22 | Strict financial safety | No execution/portfolio/wallet/risk/strategy/learning import; no `.apply()`; no order/proposal/fill route; flags `paper_only=true`, `live_trading=false`, `orders_possible=false`, `broker_connected=false` | AST safety test; route scan; live wallet-status body unchanged; monitoring responses carry the flags | PASS |
| AT2-23 | Focused tests for every capability | `tests/test_novamarket_at02.py` (63 tests) + updated AT-01 suite | 77 passed, 0 failed, 0 skipped in the autonomy suites | PASS |
| AT2-24 | Real application verification | Uvicorn app on `127.0.0.1:8002` with isolated data dir; real HTTP calls; restart | 17 scenarios below; holiday deferred correctly, restart persisted cycles, no duplicate scheduler | PASS |
| AT2-25 | Performance/resource verification | `tracemalloc`, `perf_counter`, single-task check | 30 completed cycles measured; no growth in background tasks; memory peak 103.2 KiB | PASS |
| AT2-26 | API reference synchronized | `docs/API.md` regenerated with `PYTHONPATH=src` | `--check` → `docs/API.md: in sync` (600 routes) | PASS |
| AT2-27 | Non-goals preserved | No AT-03–AT-06 code; no decision/risk/execution/exit/rebalance/learning promotion; no NM-15 label; no broker | Source/route scans; scope review | PASS |

---

## 4. Calendar and session evidence

Verified calendar: NSE/BSE 2026, source
`NSE circular NSE/CMTR/71775 (2026 holidays)` plus
`circular 03/2026 (15-Jan-2026 municipal-election closure)`.

| Scenario | Injected instant (UTC) | Result |
|---|---|---|
| Verified trading session | 2026-11-11 04:00 | `trading_session`, eligible, `09:30 +05:30` |
| Before opening | 2026-11-11 03:00 | `market_not_open` |
| At/after closing | 2026-11-11 10:00 | `market_closed` (close is exclusive) |
| Weekend | 2026-11-14 05:00 | `weekend` |
| Verified holiday | 2026-11-10 05:00 | `exchange_holiday` |
| Unverified year | 2027-01-04 05:00 | `calendar_unverified`, `calendar_verified=false` |
| Missing calendar | exchange `XNOT` | `calendar_unavailable` |
| Naive datetime | — | `ValueError`, no cycle |

The live application (10 November 2026) correctly reported
`verdict=exchange_holiday`, `reason_code=EXCHANGE_HOLIDAY`,
`calendar_verified=true`, `timezone=Asia/Kolkata`, and deferred the manual cycle.

---

## 5. Discovery, batching and persistence evidence

- Real NM-05 engine test: one seeded `XNSE:ALPHA` instrument with three daily bars
  and a fresh observation; AT-02 cycle → `completed`, `eligible=1`, `processed=1`,
  candidate `XNSE:ALPHA` with its `data_quality`.
- Duplicate/blank identifiers rejected with reasons; unknown NM-02 lookup rejected
  as `INVALID_INSTRUMENT`; a lookup that is unavailable keeps the candidate rather
  than inventing metadata.
- Rotation: 45 instruments over three cycles → **20, 20, 5**; 30 cycles → cursor 0
  after ten complete rotations; 1,350 candidate rows persisted.
- Deferred cycles claim no AT-01 cycle slot (`autonomy_cycles` count = 0) and do
  not advance rotation.
- Schema v3 tables: `autonomy_monitoring_config`, `autonomy_monitoring_state`,
  `autonomy_monitoring_cycles`, `autonomy_monitoring_candidates`
  (+ the four AT-01 tables).
- A v1 database with an AT-01 marker migrates to v3 without losing the marker;
  a running monitoring cycle left by a dead owner becomes
  `failed / CYCLE_CANCELLED`, never `completed`.

---

## 6. API surface added

| Method | Path | Authorization |
|---|---|---|
| GET | `/market/autonomy/session` | ordinary API bearer |
| GET | `/market/autonomy/monitoring/contracts` | ordinary API bearer |
| GET | `/market/autonomy/monitoring` | ordinary API bearer |
| GET | `/market/autonomy/monitoring/cycles` | ordinary API bearer |
| GET | `/market/autonomy/monitoring/cycles/{cycle_id}` | ordinary API bearer |
| POST | `/market/autonomy/monitoring/run` | bearer + `X-Autonomy-Operator-Token` |
| POST | `/market/autonomy/monitoring/config` | bearer + `X-Autonomy-Operator-Token` |

No route in the autonomy group contains `order`, `proposal`, `fill`, `execute` or
`trade`. The manual run passes the same mode, lease and verified-session gates as
the scheduled tick.

---

## 7. Real application verification

Environment: uvicorn `novacontrol.api.app:create_app`, isolated data dir, port
`8002`, tokens `api-secret` / `operator-secret`. Actual date: 2026-11-10 (verified
NSE holiday).

| # | Scenario | Result |
|---|---|---|
| 1 | Runtime starts | 200 on `/`; lifecycle running |
| 2 | AT-02 integrates with AT-01 | `/market/autonomy/monitoring` 200, `initialised=true` |
| 3 | Mode from persisted state | Boot `disabled`, `runtime_state=stopped` |
| 4 | Calendar status displayed | `exchange_holiday`, verified source, next open 2026-11-11 09:15 IST |
| 5 | Outside-session monitoring deferred | `POST run` → 200 `deferred`, `EXCHANGE_HOLIDAY`, no active cycle |
| 6 | Unverified/unknown exchange fails closed | `XNOT` → `calendar_unavailable`, `eligible=false` |
| 7 | Verified session permits discovery | Real NM-05 engine with injected 2026-11-11 04:00 clock → `completed`, `XNSE:ALPHA` |
| 8 | Filtering/normalization | Real-engine candidate persisted with symbol and quality; duplicates/blank rejected |
| 9 | >20 rotation, cap preserved | 45 candidates → 20/20/5, cap 20, cursor persisted |
| 10 | History persisted/visible | Cycle list + detail 200; deferred row visible in UI |
| 11 | Restart no duplicate scheduler/cycles | Boot disabled, 1 persisted deferred cycle; second run → 2 unique cycles, `active_cycle=null` |
| 12 | Invalid lease prevents cycles | Expired lease → `LEASE_INVALID`, zero cycles |
| 13 | Missing calendar fails closed | `XNOT` → `CALENDAR_UNAVAILABLE` |
| 14 | Provider failure reported | `DISCOVERY_PROVIDER_FAILURE` persisted; `consecutive_failures` increments |
| 15 | Stale data identified | Quality `stale` → freshness `stale`; no price substitution |
| 16 | UI matches backend | Live page: Monitoring/Running, lease held, 0 failures, `EXCHANGE_HOLIDAY` cycle; network calls 200 |
| 17 | No financial state changed | `/market/wallet/status` body identical before/after; no order/proposal route exists |

The final UI pass, run after the data-freshness addition, rendered the new
`Data freshness: Unavailable` row (no completed cycle existed) with every page
request returning 200 and no console errors.

**Note:** the live server ran on a verified holiday, so it correctly did not invoke
discovery. The eligible-session discovery path was verified with the **real NM-05
engine**, real stores and an injected clock, as permitted for deterministic
session-boundary verification. No live paid or exchange provider was fabricated or
claimed.

---

## 8. Performance and resources (Windows, CPython 3.13, local SQLite)

| Measurement | Value |
|---|---|
| Runtime + monitoring construction/start | 54.37 ms |
| Calendar evaluation (1,000 iterations) | 0.0349 ms average |
| Completed monitoring cycle (45 candidates) | 21.25 ms average (18.55–27.16 ms) |
| 30 completed cycles | 1,350 candidate rows, rotation cursor 0 |
| Memory after 30 cycles | 41.0 KiB current, 103.2 KiB peak (tracemalloc) |
| asyncio tasks after repeated cycles | 1 (the caller) |
| Refresh floor | 900 s (no 15-minute violation) |
| Background tasks added | 0 (hosted by the existing runtime tick) |

No unbounded candidate accumulation: candidates are stored per cycle with a unique
`(cycle_id, instrument_id)` key and each cycle is capped at the configured batch for
processing, with the remainder recorded as deferred. No database connection leak was
observed across the 30-cycle run; the runtime closes its store on shutdown.

---

## 9. Exact verification commands and results

```text
PYTHONPATH=src python -m pytest tests/test_novamarket_at02.py tests/test_novamarket_autonomy.py -q
→ 77 passed, 109 warnings (0 failed, 0 skipped)

PYTHONPATH=src python -m pytest tests/test_novamarket_discovery.py tests/test_novamarket_marketdata.py -q
→ 96 passed, 2 skipped, 31 warnings

PYTHONPATH=src python -m pytest tests/test_web_api.py -q
→ 75 passed, 76 subtests passed (273.30 s)

PYTHONPATH=src python -m pytest tests/test_web_platform.py -q -k "Contract or Route or surface"
→ 15 passed, 643 subtests passed

python -m mypy src
→ Success: no issues found in 561 source files

python -m mypy src --platform win32
→ Success: no issues found in 561 source files

PYTHONPATH=src python scripts/generate_api_reference.py --check
→ docs/API.md: in sync (600 routes)

cd web && npm run build
→ built in 6.09 s; exit 0
   dist/assets/index-C3dDR33g.css  91.25 kB (gzip 16.80 kB)
   dist/assets/index-C8VFNsdW.js  582.11 kB (gzip 156.05 kB)
   (existing >500 kB chunk warning only)
```

No full-repository suite was run. No failures were suppressed or tests weakened.

---

## 10. Known limitations and unresolved issues

1. **Day-trading forced end-of-day flattening** remains `PENDING_OWNER_DECISION`.
   AT-02 neither implements nor assumes it.
2. **Live discovery was not run on 2026-11-10** because it is a verified NSE
   holiday; the live app correctly deferred. The eligible-session path is verified
   with the real NM-05 engine and an injected clock, not with a live market-data
   provider.
3. **Calendar coverage is 2026 only.** 2027 dates fail closed until the annual
   calendar is verified; no holidays are extrapolated.
4. **No tick-level data claim.** Market data remains the existing periodically
   refreshed, validated NM-02 data; freshness is reported from NM-05 quality.
5. **AT-03–AT-06 absent.** No decision, risk approval, paper execution, exit
   supervision, learning promotion or rebalancing path exists.
6. **UI `Health Ok`** describes scheduler/lease health, not an open session; the
   session card separately shows `Exchange Holiday`. This is intentional.
7. The annual calendar maintenance process remains an operational task for 2027.

---

## 11. Files

**Created**

- `src/novacontrol/novamarket/autonomy/session.py`
- `src/novacontrol/novamarket/autonomy/monitoring.py`
- `tests/test_novamarket_at02.py`
- `docs/AUTONOMOUS_TRADING_AT02_REPORT.md`

**Modified**

- `src/novacontrol/novamarket/autonomy/models.py` (AT-02 vocabulary/config)
- `src/novacontrol/novamarket/autonomy/store.py` (schema v3 + monitoring persistence)
- `src/novacontrol/novamarket/autonomy/runtime.py` (`store` accessor)
- `src/novacontrol/novamarket/autonomy/__init__.py` (exports)
- `src/novacontrol/novamarket/service.py` (tick host, reads, run/config)
- `src/novacontrol/api/app.py` (7 routes, error mapping)
- `src/novacontrol/api/models.py` (`ApiSurface` rows)
- `src/novacontrol/api/route_consumers.py` (consumer rows)
- `web/src/pages/Autonomy.tsx` (session/monitoring/cycles/freshness UI)
- `tests/test_novamarket_autonomy.py` (v3 schema + discovery import allowance)
- `docs/API.md` (regenerated, 600 routes)

No financial engine, ledger, wallet, risk or portfolio file was modified. No commit,
stage, push or NM-15 label was made.

---

## 12. Final acceptance gate

| Gate | Verdict |
|---|---|
| Prerequisite AT-01 PASS | PASS |
| Session-aware verified calendar | PASS |
| Discovery via existing NM-05 engine | PASS |
| 20-instrument batching and fair rotation | PASS |
| Persistent cycle lifecycle and recovery | PASS |
| Market-data freshness and provenance | PASS |
| API and operator authorization | PASS |
| UI accuracy | PASS |
| Financial safety (no proposal/order/fill/mutation path) | PASS |
| Focused verification and real application checks | PASS |
| Type checks, API docs, frontend build | PASS |

**Overall AT-02 verdict: PASS**, with the documented limitations above (notably the
owner-pending day-trading flattening decision and 2027 calendar verification).
