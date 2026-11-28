# Autonomous Decision-Path Remediation Report

**Repository:** NovaControl / NovaMarket
**Date:** 25 November 2026; suite status updated 26 November 2026
**Scope:** Remediate the defects found by `AUTONOMOUS_REAL_MARKET_ACCEPTANCE_AND_SOAK_REPORT.md` (D1–D9) without weakening the paper-only, NM-08, NM-10, lease or duplicate-order invariants.
**Verdict:** **PASS — Stage A and Stage B acceptance complete, and the broader suite is now green.** The monitored real-instrument path is no longer blocked by the reported identity, universe, 20m, daily-bar, resume, lease, timezone or first-tick defects. D6 and D8 are browser-verified on the rendered Autonomy page. A fresh **real-market** acceptance/soak run has **not** been executed for this report; Stage C remains unauthorized. Following the 26 November stabilization pass, the full suite completed in 44 bounded file groups with **8,154 passed, 0 failed, 3 skipped and 6,426 subtests passed**; all 17 prior failures are remediated and no group timed out (see §5 and `FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`).

> This report does **not** claim real-market autonomous execution was verified. The end-to-end proof below uses deterministic market, discovery and forecast fixtures with the real strategy, NM-08 risk, NM-10 execution, portfolio, ledger, exit, reconciliation and learning code. Live-provider verification remains a separate acceptance step.

---

## 1. Outcome by original defect

| ID | Severity | Classification | What now happens |
| --- | --- | --- | --- |
| D1 | High | **PASS** | A monitored candidate reaches the decision pipeline as its canonical `MIC:SYMBOL`; legacy venue metadata is canonicalised without accepting a cross-exchange identity. NSE and NYSE are pinned by tests. |
| D2 | High | **PASS** | Monitoring scopes discovery to the configured MIC and instrument allow-list, checks every returned instrument against NM-02 metadata, rejects universe/exchange mismatches and fails closed on an ambiguous lookup. A configured XNYS cycle cannot process an XNSE candidate. |
| D3 | High | **PASS** | `20m` is a real data interval with exact aggregation, storage, freshness, ingestion, gateway, scheduler, forecast, validation and UI support. Incomplete four-bar groups are excluded rather than padded. |
| D4 | Medium | **PASS** | XNYS/XNAS daily ingestion requests provider history during an open US session and stores the last completed sessions. Provider attempts remain observable; missing history remains unavailable, not fabricated. |
| D5 | Medium | **PASS** | `_run_runtime` reconciles durable work and then executes its first tick immediately, using the current plan. It still passes through session, freshness, lease, decision and execution gates; it does not place an order. |
| D6 | Medium | **PASS (browser-verified)** | The rendered Autonomy page formats `local_time` and `next_open` with the session's IANA zone. NSE/IST, NYSE/EST, and the 1 Nov 2026 fall-back transition (EDT → EST) were exercised in Chromium with the browser explicitly in Asia/Kolkata; the displayed values followed the exchange, not the browser. Invalid, missing and unknown zones/timestamps rendered “—”. |
| D7 | Low | **PASS** | Pause records the pre-pause mode in durable `desired_mode`; omission of `mode` on resume restores that mode. Autonomous still requires its paper-only acknowledgement, and the UI labels the actual target. |
| D8 | Low | **PASS (browser-verified)** | The rendered lease metric displayed `in 30 seconds`, `in 44 seconds`, `in 1 minute`, and `in 2 minutes` for future values, and `30 seconds ago`, `44 seconds ago`, `1 minute ago`, `now` and `—` for past, zero and invalid values. No future timestamp rendered as “just now”. |
| D9 | Medium | **PASS** | A foreign live lease is still never stolen. Application auto-start records a bounded retry deadline and retries after expiry; an emergency stop remains persistent and disables auto-start. |

No safety invariant was removed or weakened. No live broker path, automatic policy promotion, fabricated bar or bypass around NM-08/NM-10 was added.

---

## 2. Root causes and fixes

### D1 — Instrument identity mismatch (`decision/pipeline.py`)

**Original defect:** AT-02 stored/could expose a venue name such as `NSE`, while AT-03 expected a MIC such as `XNSE`. The pipeline rejected the candidate before strategy or risk evaluation.

**Root cause:** Exchange comparison treated the raw values as equal. A second compatibility defect was found during this remediation: `_candidate_instrument()` accepted only mappings or objects with `to_dict()`. Read-only instrument records (including the AT-04 test seam) were therefore treated as missing even when their identity was valid.

**Fix:**

- `_candidates()` canonicalises the stored row exchange and the looked-up MIC/venue through `canonical_mic()` before requiring agreement.
- `_candidate_instrument()` now accepts a `Mapping`, `to_dict()`, or an instrument-like record exposing the identity contract fields.
- A lookup that returns a different `instrument_id` fails closed. A bare symbol is not accepted as identity.
- The pipeline still requires the candidate's `MIC:SYMBOL` prefix to agree with the canonical exchange; cross-exchange candidates remain refused.

**Files:** `src/novacontrol/novamarket/decision/pipeline.py`, `tests/test_novamarket_at03.py`.

### D2 — Monitoring ignored the configured exchange/universe

**Root cause:** Discovery returned its global candidate set, and monitoring evaluated the session only for the configured exchange. Candidate-level universe and exchange checks were absent, so a global XNSE instrument could be processed inside an XNYS cycle.

**Fix:**

- Monitoring passes an explicit `UniverseFilter(exchanges=(configured_mic,), ...)` and the configured instrument IDs to discovery.
- `_normalise()` rejects candidates outside the allow-list, candidates whose NM-02 MIC disagrees with the configured MIC, duplicates and malformed rows.
- A lookup that returns another canonical identity is rejected as `INVALID_INSTRUMENT` rather than being processed under the requested identity.
- The verified exchange calendar still gates the cycle; closed/unverified exchanges are deferred, and no executable proposal is produced.

**Files:** `src/novacontrol/novamarket/autonomy/monitoring.py`, `tests/test_novamarket_at02.py`.

### D3 — 20-minute timeframe

**Root cause:** The horizon existed in forecast/user-facing policy, but `DataInterval` and the ingestion path had no 20m member. A request for 20m fell through as an unsupported internal error.

**Fix:**

- Added `DataInterval.MINUTE_20 = "20m"` as an intraday interval.
- Yahoo requests 5m source bars and aggregates exactly four consecutive 5m bars into one 20m bar. The aggregation preserves OHLCV, currency, source timezone, component timestamps and partial/usable state. A group that is not exactly `[0, 5, 10, 15]` minutes is not emitted.
- Added 20m to ingestion settings/schema, engine interval bounds, freshness, gateway schema, ensemble/gradient/statistical/dataset mappings, scheduler policy and validation paths.
- UI horizon lists already advertised 20m and now match real backend support.

**Files (principal):** `marketdata/models.py`, `marketdata/providers/yahoo.py`, `marketdata/engine.py`, `marketdata/freshness.py`, `marketdata/settings.py`, `marketdata/gateway.py`, `datasets/models.py`, `features/models.py`, `features/engine.py`, `config.py`, `decision/forecast.py`, `forecast_intelligence/models.py`, `statistical/validation.py`, `scheduling/models.py`.

### D4 — XNYS daily bars skipped

**Root cause:** `_needs_bars()` skipped daily provider requests while the US session was open because today's partial daily bar would be discarded. With no stored daily history, an AAPL daily forecast therefore had no completed bars.

**Fix:**

- During an open session, daily ingestion now asks the provider whenever the most recent **completed** exchange session is absent.
- Session dates are compared in the exchange calendar's own zone, not UTC or host-local dates.
- Provider attempts/failures and per-instrument results remain in the ingestion report.
- Missing history remains a truthful unavailable/422 outcome; no bar is synthesized.

**Files:** `marketdata/engine.py`, `marketdata/providers/yahoo.py`, `tests/test_novamarket_marketdata.py`.

### D5 — First tick slept before evaluation

**Root cause:** `_run_runtime` calculated a plan, slept, and only then evaluated; its first cycle could miss the session open by a full interval.

**Fix:** after startup reconciliation, the loop now computes the current plan and runs `_runtime_tick()` **before** its first sleep. The tick is the normal gated pipeline (session → monitoring → decision → NM-08 → NM-10 → exits), not an immediate order. Serialization, one-cycle-at-a-time and the lease remain authoritative.

**File:** `src/novacontrol/novamarket/service.py`; pinned by `test_runtime_scheduler_ticks_before_its_first_sleep`.

### D6 — Exchange-local time displayed as browser time

**Root cause:** `dateTime()` formatted ISO timestamps in the browser's local zone while the UI labelled the result “Exchange-local time”.

**Fix:**

- Added `dateTimeInZone(value, timeZone)` using `Intl.DateTimeFormat` with the session's IANA zone and a short zone abbreviation.
- `Autonomy.tsx` uses the session timezone for both `local_time` and `next_open`.
- A missing/invalid IANA zone returns “—” rather than silently showing browser time.

**Files:** `web/src/lib.ts`, `web/src/pages/Autonomy.tsx`.

### D7 — Resume downgraded autonomous mode

**Root cause:** `AutonomyRuntime.resume()` required an explicit mode, the API defaulted an omitted mode to `monitoring`, and the UI always sent `monitoring`.

**Fix:**

- `save_control()` accepts an optional `desired_mode`; pause persists the mode being paused.
- `resume()` with no mode reads the durable desired mode. It still validates the lease and autonomous acknowledgement and still enters through the same transition rules.
- The API passes `None` when mode is omitted; explicit mode remains supported.
- The UI offers “Resume monitoring” or “Resume autonomous” from `desired_mode` and sends the matching paper-only acknowledgement.
- Emergency stop remains `disabled`, releases the lease and disables auto-start; resume cannot undo it.

**Files:** `autonomy/runtime.py`, `autonomy/store.py`, `service.py`, `api/app.py`, `web/src/pages/Autonomy.tsx`, `tests/test_novamarket_autonomy.py`.

### D8 — Future lease rendered as “just now”

**Root cause:** `timeAgo()` collapsed every absolute difference below 45 seconds to “just now”, including future timestamps.

**Fix:** sub-45-second values now go through signed `Intl.RelativeTimeFormat`; a valid lease reads “in 30 seconds”/“in 1 minute”, while a past value reads “30 seconds ago”.

**File:** `web/src/lib.ts`.

### D9 — No retry after a temporary lease conflict

**Root cause:** On restart, a still-valid lease from the previous process made auto-start return `disabled`/`degraded` once, with no later retry. The lease was correctly not stolen, but an unclean termination had no bounded recovery path.

**Fix:**

- `_auto_start_autonomy()` sets a retry deadline of the lease duration plus a bounded grace period.
- The autonomy heartbeat calls `_retry_autonomy_auto_start()` while the deadline is open.
- A genuinely live foreign owner that keeps renewing causes the retry to stop at the deadline; the lease is never stolen.
- Emergency stop (`auto_start=False`) remains persistent and prevents retry.
- `/market/autonomy/status` exposes `auto_start_retry.active`, `retry_until` and a truthful reason in addition to `lease.foreign/conflict` and `recovery_required`. The UI shows the retry deadline and reason in the lease card.

**Files:** `service.py`, `autonomy/runtime.py`, `autonomy/store.py`, `tests/test_novamarket_at06_runtime_integration.py`.

---

## 3. Pipeline behaviour after remediation

The intended path is now:

```
configured MIC + instrument allow-list
  → session-gated monitoring (verified exchange calendar)
  → canonical candidate (MIC:SYMBOL, universe/exchange checked)
  → forecast + strategy evaluation
  → proposal
  → independent NM-08 assessment
  → NM-10 paper execution
  → portfolio + ledger
  → AT-05 exit supervision through NM-08/NM-10
  → reconciliation and realized P&L
  → forecast outcome evaluation after its horizon
  → advisory RL feedback (no promotion)
```

Invalid, stale, out-of-universe, ambiguous and high-risk candidates still fail before an executable proposal. Duplicate-order prevention and one-cycle-at-a-time serialization are unchanged.

---

## 4. Tests added or strengthened

| Test | Purpose |
| --- | --- |
| `test_novamarket_at03.py::test_attribute_based_instruments_are_eligible_on_nse_and_nyse` | A read-only instrument record can be selected on both NSE and NYSE; a XNSE candidate inside a configured XNYS cycle is rejected. |
| `test_novamarket_at03.py::test_an_instrument_lookup_with_another_identity_fails_closed` | A lookup returning a different `MIC:SYMBOL` produces `NO_ELIGIBLE_CANDIDATES`, not a proposal. |
| `test_novamarket_at02.py::test_instrument_lookup_with_another_identity_fails_closed` | Monitoring rejects an ambiguous identity before persisting it as eligible. |
| `test_novamarket_autonomy.py::test_resume_without_a_mode_restores_the_mode_that_was_paused` | Pause stores the prior mode; omitted resume restores autonomous with acknowledgement, and an unacknowledged autonomous resume is refused. |
| `test_novamarket_autonomy.py` API control test | Omitting `mode` on `/market/autonomy/resume` restores the paused mode. |
| `test_novamarket_at06_runtime_integration.py::test_unclean_restart_inside_lease_window_retries_after_expiry` | The status exposes an active bounded retry with its deadline while the foreign lease lives, then clears it after the lease expires and the mode is restored. |
| `test_novamarket_at06_end_to_end.py::test_20_step_isolated_paper_workflow` (strengthened) | Explicit valid-candidate/NM-08 provenance, stale-candidate rejection with no proposal/order, distinct entry and exit NM-08 decision IDs, NM-10 order linkage, portfolio-wallet cash equality, realized loss, and no replay after restart. |
| `test_novamarket_gateway.py::test_20m_is_an_advertised_and_accepted_intraday_interval` | Capability schema and runtime settings accept 20m. |
| `test_novamarket_features.py::test_twenty_minute_feature_resolution_is_accepted_with_the_existing_intraday_set` | FeatureSettings, FeatureRequest and aliases accept 20m while the existing 1m/5m/15m/30m set remains accepted. |
| Existing tests used as Stage A evidence | Exchange/universe filtering, closed-market deferral, 20m exact aggregation/ingestion/scheduling, XNYS daily open-session ingestion, lease retry, immediate first tick, forecast outcome timing, NM-08/NM-10 provenance and advisory feedback. |

---

## 5. Verification results

All commands were run from `novamarket/` with the repository's existing parent virtual environment. No user `data/` directory was used or modified; tests use temporary/isolated stores.

### Stage A — focused regression

Command:

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket_at02.py tests/test_novamarket_at03.py \
  tests/test_novamarket_at04.py tests/test_novamarket_at05.py \
  tests/test_novamarket_at06.py tests/test_novamarket_at06_end_to_end.py \
  tests/test_novamarket_at06_runtime_integration.py \
  tests/test_novamarket_marketdata.py tests/test_novamarket_multitimeframe.py \
  tests/test_novamarket_scheduler.py tests/test_novamarket_autonomy.py \
  tests/test_novamarket_gateway.py tests/test_novamarket_features.py \
  -q -p no:cacheprovider
```

Result: **377 passed, 2 skipped, 9 subtests passed** (exit 0, 231.11 s) on 25 November 2026. The count increased from the earlier 375 because the identity and 20m regressions are now included.

### Stage A — browser verification (closes D6 and D8)

The real Vite page was rendered against an isolated backend and exercised with Playwright using the installed Edge/Chromium browser at 1440×1000, locale `en-IN`, browser timezone `Asia/Kolkata`. Only the monitoring-session and status-lease payloads were substituted; the Autonomy page, formatting utilities, routing and React rendering were the production code. Full scenario detail is in `OFFLINE_INTEGRATION_VERIFICATION_REPORT.md`.

Command:

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe \
  tmp/stage_a_ui_acceptance/verify_d6_d8.py
```

Result: **39 checks, 0 failed** (exit 0). Key rendered values:

| Scenario | Actual rendered value |
| --- | --- |
| NSE / Asia/Kolkata | `25 Nov 2026, 06:45 pm IST`; next open `26 Nov 2026, 09:15 am IST` |
| NYSE / America/New_York, browser at Asia/Kolkata | `25 Nov 2026, 08:15 am GMT-5`; next open `25 Nov 2026, 09:30 am GMT-5` |
| US fall-back, 1 Nov 2026 | local `01 Nov 2026, 01:30 am GMT-4`; next open `02 Nov 2026, 09:30 am GMT-5` |
| Invalid/missing timestamps and zones | `—` for both local time and next open; no browser-zone fallback |
| Future lease, 30/44/45/90 s | `in 30 seconds`, `in 44 seconds`, `in 1 minute`, `in 2 minutes` |
| Past lease, 30/44/45 s; zero; invalid | `30 seconds ago`, `44 seconds ago`, `1 minute ago`, `now`, `—` |

Runtime status, pause, resume, lease-retry text, exchange selection, the separate Market Overview universe selector, the reliability lease and exit `Last scan` relative time all rendered. No page errors or failed API responses occurred. The browser's automatic `/favicon.ico` request is the only 404 and is recorded as a non-functional limitation.

### Stage B — isolated end-to-end paper lifecycle

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket_at06_end_to_end.py -q -p no:cacheprovider
```

Result: **1 passed** (exit 0, 1.83 s) after strengthening the assertions. The 20-step workflow now explicitly covers a valid candidate and its independent NM-08 approval, a stale candidate rejected with no proposal or order, NM-10 entry/exit fills, portfolio-wallet cash equality before/after both legs, distinct entry/exit NM-08 decision IDs, exit supervision, passed reconciliation, a realized loss, forecast measurement after target, verified advisory RL feedback, and `automatic_policy_promotion=false`, with no order/exit replay after restart. **Fixture evidence, not live-market evidence.**

### Type, frontend and API reference checks

| Check | Command | Result |
| --- | --- | --- |
| Mypy | `PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src` | **Success, no issues in 728 source files** (exit 0) |
| Frontend production build | `cd web && npm run build` | **PASS**, exit 0 (`tsc && vite`, built in 7.45 s) |
| API reference sync | `PYTHONPATH=src ../.venv/Scripts/python.exe scripts/generate_api_reference.py --check` | **in sync (936 routes)**, exit 0 |

The frontend build emitted only the existing dynamic/static import warning and the large-chunk warning; there were no type or build errors.

### Full NovaMarket suite

The suite was run to completion in **44 bounded file groups** with a 1,800 s guard. The final run had **44/44 groups exit 0**, covering 215 files and all 8,157 runtime outcomes. No test was deselected and no group timed out.

| Measure | Result |
| --- | --- |
| Groups | **44 / 44 exit 0** |
| Passed | **8,154** |
| Failed | **0** |
| Skipped | **3** (two live-provider opt-ins, one optional-SQLAlchemy module skip) |
| Subtests | **6,426** |
| Runtime outcomes | **8,157** (8,156 collected + one module-level skip) |

The prior 17 failures and their fixes are recorded in `FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`. That pass also exposed and fixed a genuine radar event-loop starvation defect: synchronous regex extraction during a refresh could make the API stop answering and caused browser E2E timeouts. No failure is attributable to D6/D8 or the Stage B lifecycle.

---

## 6. Remaining limitations

1. **No real-market acceptance in this pass.** No live provider ingestion, live session decision, live paper order or multi-hour soak was run for this report. The preceding acceptance report's real-market conclusion is not retroactively changed.
2. **Browser verification used controlled API payloads.** D6/D8 were rendered by the real page, but the monitoring-session and lease fields were deterministic fixtures. No live NSE/NYSE session or natural clock passage was used; a real-market session remains Stage C.
3. **The browser is the installed Edge/Chromium on this host.** The check proves the IANA-zone and relative-time rendering in Chromium with `en-IN` formatting; it is not a cross-browser matrix.
4. **Fixture market data is deterministic.** The end-to-end test proves wiring, authority boundaries and accounting, not forecast profitability or live-data availability.
5. **20m is derived from Yahoo 5m bars.** It is exact only for complete four-bar groups. A provider with native 20m bars would produce its own provenance; no gaps are filled.
6. **Long-duration scheduling/cadence during a live session remains unproven.** The immediate first tick is unit/integration tested, not observed over a full market day.
7. **Auto-start is an operator-set application policy.** It is not an implicit resume of a paused mode; emergency stop disables it. Historical owner-decision documents that say auto-start does not exist predate the AT-06 implementation and are superseded on that point.
8. **`/favicon.ico` returns 404 in the development browser.** The page defines no favicon; this is pre-existing, non-functional and unrelated to D6/D8. Application console output, page errors and API responses were otherwise clean.
9. **The broader suite is green on the current worktree.** The final 44-group run is **8,154 passed, 0 failed, 3 skipped, 6,426 subtests**. The three skips are intentional: two live-provider tests gated by `NOVAMARKET_LIVE_TESTS=1` and the `test_database.py` optional-SQLAlchemy module skip. The 17 prior failures, including the resolved static-UI/NovaForge/evaluation/automation cases and the radar event-loop defect, are documented in `FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`. The broader suite green status does **not** imply a real-market acceptance or Stage C result.

---

## 7. Acceptance criteria still requiring real-market validation

A fresh real-market acceptance run should verify, with recorded evidence:

- configured `XNSE` and `XNYS` instruments survive monitoring → decision → forecast/strategy → NM-08 → NM-10 → exit → ledger with real provider bars;
- no cross-exchange instrument appears in a configured cycle;
- a real 20m bar set is ingested, scheduled and consumed without boundary or leakage errors;
- XNYS daily bars arrive while the US session is open and a daily forecast succeeds only when valid data exists;
- pause/resume restores the previously intended mode under the live lease and health checks;
- a restart inside the old lease window auto-starts only after the bounded expiry retry, without stealing a live owner's lease;
- an actual eligible signal can traverse the paper path and produce a reconciled trade and advisory RL evidence, with `automatic_policy_promotion=false`.

Exchange-local time and next-open rendering for NSE, NYSE and the US fall-back transition are no longer deferred: they are browser-verified in this report. The remaining list is the real-market acceptance scope, not an open Stage A item.

Until those checks are performed against real data, this report's status is **Stage A and Stage B PASS on software/fixture and browser evidence**, not a real-market acceptance PASS.
