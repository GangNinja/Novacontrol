# NovaMarket Bug Fix Register

**Audit date:** 2026-11-11  
**Closure date:** 2026-11-13  
**Scope:** NM-01–NM-14 and AT-01–AT-06 post-acceptance re-audit  
**Status:** **All actionable findings closed.** No open P0/P1, financial-correctness, security or authorization defect. Six environmental/evidence limitations and three accepted limitations/observations remain. Operational release acceptance remains **PARTIAL**.

---

## 1. Summary

| ID | Severity | Type | Title | Status | Verification |
| --- | --- | --- | --- | --- | --- |
| BUG-QA-001 | P2 | Read-model/UI aggregation | Horizon status used only the first book | **FIXED (prior)** | regression test |
| BUG-QA-002 | P2 | Read-model/data selection | Market pulse showed duplicate/unquoted rows | **FIXED (prior)** | regression test |
| BUG-QA-003 | P3 | Chart presentation | Flat valuation series produced identical Y-axis ticks | **FIXED (prior)** | regression test |
| BUG-QA-429 | P2 | UX/error handling | 429 rendered as “Backend offline” | **FIXED** | interception + build |
| TEST-QA-ROUTE | P3 | Test contract | Route test expected 221 decorators; actual 258 | **FIXED** | targeted + consolidated suite |
| DOC-QA-API | P3 | Documentation contract | `docs/API.md` out of sync with route registry | **FIXED** | 6 tests / 630 subtests |
| QA-HARNESS-MOBILE | P3 | Test harness | Mobile probe raced the 220 ms sidebar transition | **FIXED — false positive** | 26-route a11y rerun |
| NM-R1-01 | P1 (prior) | Autonomy runtime | Lease heartbeat never started | **FIXED (prior)** | live 75 s + regression |
| NM-R1-02 | P1 (prior) | Autonomy recovery | Recovery retained the caller lease | **FIXED (prior)** | live recovery + regression |
| NM-R1-03 | P2 (prior) | UI stale state | Mode change refreshed only the header | **FIXED (prior)** | browser pause/resume |
| NM-R1-04 | P3 | UI guard | Run decision pipeline enabled while paused | **FIXED 2026-11-13** | browser + 17 UI contract tests |
| NM-R1-05 | P2 | UI completeness | No dedicated recovery action/unresolved list | **FIXED 2026-11-13** | browser + API 200 |
| NM-R1-06 | P2 config | Authentication | API/operator tokens unset in QA environment | **FIXED 2026-11-13** | 401 / 403 / 200 |
| NM-R1-07 | Info | Documentation | Shell status fields are release-gate fields | **ACCEPTED** | documented |
| NM-R1-08 | P3 | Verification gap | No live provider outage/failover drill | **PARTIAL** | 21 deterministic provider tests; no live drill |
| OBS-QA-ZOOM | P3 | Responsive observation | 200% CSS-zoom simulation overflow on 3 routes | **ACCEPTED** | not reproduced in real zoom |
| OBS-QA-MARKET-LATENCY | Info | UX observation | Market table settles in ~2 s after search | **ACCEPTED** | loading state; no data loss |
| QA-COVERAGE | Process | Evidence scope | Not every historical 470 control re-operated | **PARTIAL** | historical matrix preserved |
| QA-PAGINATION | Process | Evidence scope | Current market pagination not re-proven | **PARTIAL** | prior stateful retest only |
| QA-VALUE-CROSSCHECK | Process | Evidence scope | Not every chart/table value re-diffed | **PARTIAL** | render/a11y pass only |
| QA-LIVE-LIFECYCLE | External state | Evidence gap | No current order/fill/position lifecycle | **BLOCKED** | market closed; 0 records |
| QA-AUTONOMY-LIVE | Runtime state | Evidence gap | No continuous autonomous cycle | **NOT DEMONSTRATED** | runtime disabled at rest |

No P0/P1 product defect is open. No financial formula, risk policy, allocation, fee, slippage, ledger rule, paper-only flag or broker boundary was changed during closure.

---

## 2. NM-R1-04 — paused decision affordance (fixed 2026-11-13)

**Severity:** P3 — misleading enabled control.  
**Type:** incomplete functionality / UI guard.  
**Page:** Autonomy.

### Original behavior

`Run decision pipeline` was disabled only by operator permission and lease validity. In paused mode it appeared enabled, the page gave no explanation, and only the backend refused the run with 409.

### Root cause

The page duplicated only part of the server gate. It did not incorporate `mode === paused` or `mode === disabled` into the decision action.

### Fix

- `web/src/pages/Autonomy.tsx` now derives one explicit `decisionBlockedReason` for paused, disabled and invalid-lease states.
- The button is disabled when that reason exists: `disabled={!controlsEnabled || Boolean(decisionBlockedReason)}`.
- A `role="status"` paragraph says `Decision run unavailable.` and explains why.
- Paused copy explicitly states that resume is required while protective AT-05 exits and reconciliation remain active.
- Server-side enforcement is unchanged.

### Verification

- Browser: start monitoring, pause, button disabled and explanation visible; recovery returned the page to disabled.
- `tests/test_novamarket_workbench_ui.py`: paused/disabled copy, disabled condition and status role pinned.
- No financial impact; the backend 409 remains authoritative.

---

## 3. NM-R1-05 — recovery and reliability visibility (fixed 2026-11-13)

**Severity:** P2 — UI completeness.  
**Type:** frontend/backend integration.  
**Page:** Autonomy.

### Original behavior

The page showed only an aggregate `Paper pipeline: healthy/needs attention` line. It did not expose the dedicated reliability report, recovery state, pending reconciliation or unresolved records, and had no recovery action.

### Root cause

`GET /market/autonomy/reliability` and `POST /market/autonomy/recover` existed and were API-tested, but no UI presentation layer consumed them.

### Fix

`web/src/pages/Autonomy.tsx` now renders the existing report:

- runtime mode/state, desired mode, started time, heartbeat, lease expiry;
- scheduler health/running state and last failure domain;
- pipeline counts from discovery through paper fills;
- supervised positions, pending orders, fills/partials, open exits, reconciliation pending and learning incomplete;
- recovery last action/time, recovered/reconciled counts, auto-resume flag and preserved pending orders;
- failure domains, health reasons and last error;
- unresolved reconciliation records with an explicit empty state;
- loading, stale-report warning and terminal error states.

The `Recover and disable` action calls the existing `POST /market/autonomy/recover` with the dedicated operator token. A confirmation dialog states that recovery reconciles existing work, never resumes and never replays unresolved work. On completion, all autonomy cards refresh.

No second recovery service, scheduler, endpoint or financial path was added.

### Verification

- Browser probe: panel visible; pipeline health, scheduler health, lease, unresolved empty state and recovery action present.
- Recovery returned HTTP 200 and runtime `disabled`; response reported `auto_resumed=false`, `paper_only=true`, `live_trading=false`, `broker_connected=false`.
- After clearing the operator token, recovery and decision actions disabled while ordinary API authentication still read the panel.
- Zero console errors.
- Five new contract tests cover endpoints, labels, loading/empty/error/stale states and operator-only confirmation.

---

## 4. NM-R1-06 — authentication configuration (fixed 2026-11-13)

**Severity:** P2 configuration.  
**Type:** security/authorization configuration.  
**Environment:** isolated QA API `127.0.0.1:8012`.

### Original behavior

No ordinary API token and no autonomy-operator token were configured. Reads were unauthenticated on loopback; operator controls were correctly locked.

### Fix

- Generated independent random values in ignored files:
  - `tmp/full_qa/api_token.txt`
  - `tmp/full_qa/operator_token.txt`
- The ignored launcher `tmp/full_qa/start_qa_api.py` reads them into:
  - `NOVACONTROL_API_TOKEN`
  - `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`
- No value was printed, committed, placed in source, or added to a frontend bundle.
- The existing fail-closed authorization logic was not weakened.
- `docs/CONFIGURATION.md` now documents the separate variable, the requirement that it differ from the API token, and the secure setup.

### Verification

| Request | Result |
| --- | --- |
| Reliability read without bearer token | **401** |
| Reliability read with ordinary token | **200** |
| Recovery with ordinary token only | **403** `autonomy_operator_forbidden` |
| Recovery with wrong operator token | **403** `autonomy_operator_forbidden` |
| Recovery with correct distinct operator token | **200**, `status=completed`, `mode=disabled`, `auto_resumed=false` |
| UI recovery with operator token | **PASS** |
| UI after clearing operator token | **controls locked**, panel still readable |

The server remains bound to `127.0.0.1`. Any non-loopback deployment must configure both tokens and a trusted interface.

---

## 5. Prior product fixes (preserved)

### BUG-QA-429 — 429 mislabeled as “Backend offline” (P2)

The header/Dashboard error path now distinguishes HTTP 429 from connectivity loss. Browser interception verified `rate_limited=true` and `offline=false`; the server-side limiter was not weakened. Build passed.

### TEST-QA-ROUTE — stale route count (P3)

`tests/test_novamarket.py` expected 221 `/market` route decorators; the actual registry has 258. The expectation was updated to 258 without removing the namespace assertion.

### DOC-QA-API — API reference out of sync (P3)

`docs/API.md` was regenerated from the route-consumer registry. Current check: **630 routes**, 6 tests / 630 subtests passed.

### QA-HARNESS-MOBILE — false mobile-overflow signal (P3)

The accessibility probe measured 150 ms after a viewport change while the sidebar transition is 220 ms. At 400 ms both `health` and `autonomy` measured 320 px with no overflowing visible element. The probe was corrected; no product CSS changed. This is the register's one false positive.

### BUG-QA-001 / BUG-QA-002 / BUG-QA-003 (P2/P2/P3)

The dashboard horizon, market-pulse and flat-chart fixes remain present and are covered by regression tests. They were not rewritten.

### NM-R1-01 / NM-R1-02 / NM-R1-03 (P1/P1/P2)

- Service-owned lease heartbeat remains active.
- Recovery releases the caller's live lease before store recovery.
- A successful control action refreshes status, monitoring, decisions, executions, exits, positions and reliability.

Focused tests passed in the 2026-11-13 consolidated run; the browser probe observed refreshed decision/reliability cards after start/pause and after recovery.

---

## 6. NM-R1-07 — accepted shell-status limitation

AT-01 shell status reports `trading_implemented=false`, `orders_possible=false`, `release_gate=closed` and `exit_supervision_implemented=false`. These are shell fields, not complete-pipeline fields; AT-04/05/06 expose the pipeline view. This is documented and accepted, not a bug.

---

## 7. NM-R1-08 — provider failure verification (partial)

### Already covered deterministically

- bounded retry and recovery from 503;
- timeout retry and final typed failure;
- connection failure retry and final typed failure;
- HTTP 429 with `Retry-After` recovery and exhausted fail-closed;
- unknown symbol vs schema change;
- missing OHLCV rows skipped without fabrication;
- stale observation stored as stale;
- circuit open and single half-open probe;
- primary failure followed by secondary success;
- both providers failing with no fabricated record;
- provider disagreement visible and not averaged.

The focused deterministic run passed **21/21** tests.

### Not covered

No live provider outage, rate-limit response or failover was induced. Yahoo and CoinGecko liveness and a real quote were verified in the earlier NM-R1 audit. The deterministic suite proves application behavior, not current provider availability. This remains an environmental verification gap, not a known product defect.

---

## 8. Accepted observations and evidence gaps

### OBS-QA-ZOOM — 200% CSS-zoom observation

A CSS-zoom simulation previously showed horizontal overflow on `dashboard`, `market` and `settings`. It was not reproduced with real browser zoom and is not a confirmed accessibility failure. No fix was applied; the 320 px viewport pass is clean.

### OBS-QA-MARKET-LATENCY

The market table takes about two seconds to settle after a search. A loading state is present; results are not lost or fabricated. This is a UX latency observation, not a data-integrity defect.

### QA-COVERAGE

The historical 470-control matrix is preserved. The current pass inspected 1,140 controls for accessibility and operated the targeted controls, but did not re-operate all 470 historical entries. The matrix is not re-claimed as a fresh 470/470 run.

### QA-PAGINATION and QA-VALUE-CROSSCHECK

Market next-page behavior, every chart point and every table row were not re-proven in the current pass. These remain partial evidence, not regressions.

### QA-LIVE-LIFECYCLE — blocked

The market was closed and the isolated store had **0 paper orders, 0 fills and 0 open positions**. No live order → fill → position → protective exit → ledger reconciliation was forced.

### QA-AUTONOMY-LIVE — not demonstrated

The runtime is correctly `disabled/stopped` at rest, with no lease and no positions. AT-01–AT-06 tests pass, but no sustained live autonomous cycle was observed. The application must not be described as operationally proven autonomous.

---

## 9. Change inventory

Changed in this closure:

- `web/src/pages/Autonomy.tsx` — decision gate/copy; reliability/recovery panel; recovery action.
- `tests/test_novamarket_workbench_ui.py` — five Autonomy UI contract tests.
- `tests/test_novamarket_marketdata.py` — connection-failure and 429/Retry-After tests.
- `docs/CONFIGURATION.md` — operator-token configuration.
- `NOVAMARKET_FINDINGS_CONSOLIDATION.md` — new findings register.
- `NOVAMARKET_REMEDIATION_REPORT.md` — remediation evidence.
- `NOVAMARKET_RELEASE_AUDIT_REPORT.md` — dated closure section appended; original verdict preserved.
- `docs/BUG_FIX_REGISTER.md` — this update.
- `docs/QA_COMPLETION_REPORT.md` — closure addendum.
- `tmp/full_qa/start_qa_api.py` — ignored launcher reads ignored token files.
- `tmp/full_qa/nm_r1_ui_probe.py` — ignored browser verification probe.

Previously preserved changes: `web/src/App.tsx`, `web/src/pages/Dashboard.tsx` (429 copy); `tests/test_novamarket.py` (258 route count); `docs/API.md` (regenerated); `tmp/full_qa/a11y_probe_26.py` (400 ms settle).

No commit, staging, push or tag was performed. No production database, broker credential, live-trading path or paper-only configuration was modified.

---

## 10. Register verdict

**REMEDIATION COMPLETE** for actionable software findings.  
**PARTIAL** for operational release acceptance because the live paper lifecycle, continuous autonomy and live provider failure drill remain environment-dependent.

No P0/P1, financial-correctness, security or authorization defect is open. The accepted NM-01–NM-14 and AT-01–AT-06 baseline is intact.

---

## 11. PI-01–PI-24 closure addendum (21 November 2026)

**Scope:** final engineering closure of PI-01 through PI-24. No PI-25 and no
feature was added.

| ID | Severity | Type | Title | Status | Verification |
|---|---|---|---|---|---|
| BUG-PI-CLOSE-01 (O-1) | P2 | Read path / consistency | `/holdings` served the stored fill price while `/valuation` refreshed to the latest observable, so the same position was reported at two prices | **FIXED 2026-11-21** | deterministic reproduction (100.00 vs 110.00), regression test, E2E re-run (both 100.50) |
| BUG-PI-CLOSE-02 | P3 | Test harness | LSTM unavailable-runtime test crashed on an empty matrix instead of reporting unavailability | **FIXED (prior)** | forced unavailable-backend test |
| BUG-PI-CLOSE-03 | P3 | Resource lifecycle | Shutdown returned before withdrawing lazily opened stores, leaving a SQLite handle open on Windows | **FIXED (prior)** | gateway shutdown test |
| BUG-PI-CLOSE-04 | P3 | Test contract | Route-contract test omitted `/market/training/datasets` | **FIXED (prior)** | set-equality route test |
| BUG-PI-CLOSE-05 | P2 | Review routing | Manual review fallback with no active book could target a closed portfolio | **FIXED (prior)** | rotation + no-active-book tests |
| ENV-PI-CLOSE-01 | Process | Test environment | 20 tests failed on a base install (13 LSTM + 7 gradient) solely because optional ML backends were absent | **RESOLVED** | extras installed; full suite 1907 passed / 0 failed |

### O-1 root cause and fix

A fill stores its execution price as the position's `last_price`. The holdings
read model returned that stored value unchanged, while valuation refreshed the
position to the newest observable quote/bar before valuing it. Both endpoints
claimed to report the book's current value, so the divergence was a read-path
defect, not an intended difference.

`PortfolioEngine.holdings()` now calls `_refresh_positions(record, now)` before
building its payload, exactly as valuation does. The regression test
`test_holdings_and_valuation_agree_on_one_price` checks the refreshed price and
that the sum of holdings market values equals valuation `invested`.

**Verification:** full suite **1907 passed, 0 failed, 0 errors, 2 skipped, 445
subtests in 736.50 s**; `mypy src` clean (721 files); API reference in sync (933
routes); 14 stores integrity-clean; paper E2E consistent at 100.50.

### Still open (not addressed by this addendum)

- The 20m → 5m → 1m decision hierarchy is not implemented; 1 minute is only the
  T−1m checkpoint and a data interval.
- PI-23 portfolio-horizon to forecast-horizon mapping and the AT-03 venue /
  instrument-identifier mismatch recorded in the README remain open.
- No live-market validation or sustained live paper session exists.

No commit, staging or push was performed as part of the audit itself; the
resulting tree is committed separately with the closure report.
