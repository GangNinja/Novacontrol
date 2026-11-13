# NovaMarket Full Application QA Report

**Current re-audit:** 2026-11-11  
**Original audit / post-fix sweep:** 2026-11-09 / 2026-11-10  
**Application:** NovaMarket standalone workbench (`novamarket/`)  
**Audit type:** real-browser Chromium QA, requirements traceability, workflow verification, accessibility, security, ledger integrity, performance and autonomy review  
**Baseline:** NM-01–NM-14 accepted 9 November 2026; AT-01–AT-06 accepted before this pass  
**Verdict:** **PARTIAL** — the accepted baseline is intact and no P0/P1 product defect remains open, but this pass did not re-operate every one of the historical 470 controls and could not demonstrate the live paper-order lifecycle or continuous autonomous trading because the market was closed and the runtime was disabled.

> **2026-11-13 closure:** NM-R1-04 and NM-R1-05 are fixed, and NM-R1-06 is configured in the isolated QA environment. This document remains the 2026-11-11 baseline; see `NOVAMARKET_REMEDIATION_REPORT.md` and `docs/QA_COMPLETION_REPORT.md` for current status.

---

## 1. Executive summary

All **26 current routes** opened and reached their own heading without an error boundary, console error, page error or failed API request. The isolated Chromium sweep is clean at **26/26**. The 26-route accessibility pass inspected **1,140 controls** and found **0 unlabeled controls, 0 unnamed buttons/links, 0 heading-order issues, 0 contrast failures, 0 focus gaps, 0 desktop/mobile overflow and 0 console/page errors**.

The application is not being declared fully PASS. The reasons are evidence scope, not a newly discovered financial defect:

- the historic **470/470 control matrix** is a 25-route result from 10 November; this pass did not re-operate all 470 entries after the later Autonomy work. It adds **85 explicit current control operations (85 PASS)** and **3 operator-token-gated controls BLOCKED**;
- the production/isolated database contains **0 paper orders, 0 fills and 0 open positions**, and the market was closed, so a complete paper order → fill → position → exit lifecycle could not be demonstrated in this pass;
- the Autonomy runtime is **disabled/stopped** at rest, so continuous autonomous operation was not demonstrated;
- no live provider outage, rate-limit or failover drill was induced.

A real UX defect was found and fixed: a **429 rate-limit response was presented as “Backend offline.”** A stale route-count test (`221` vs. the actual `258` decorators) was also corrected, and the API reference was regenerated. No risk control, paper-only flag, financial formula, ledger rule or broker boundary was changed.

### Current result summary

| Area | Result | Evidence |
| --- | --- | --- |
| Route load and runtime errors | **PASS — 26/26** | `tmp/full_qa/route_sweep.json` |
| Consolidated regression | **PASS — 978 passed, 2 skipped, 991 subtests** | `.ui_audit/qa_full_regression2.log` |
| Accessibility | **PASS — 26 routes, 1,140 controls, 0 issue classes** | `tmp/full_qa/a11y_results_26_final.json` |
| Security probe | **PASS — 9/9 checks** | `.ui_audit/security_results.json` |
| Performance | **PASS — 10/10 API checks; browser sweep PASS** | `tmp/full_qa/performance.json` |
| Targeted API tests | **PASS — 139 passed, 284 subtests** | targeted pytest run |
| Frontend build | **PASS — 1,599 modules** | `npm run build` |
| Write workflows | **14/15 automated PASS; 1 harness-blocked, manually rechecked** | `.ui_audit/write_flows2.json` |
| Live paper lifecycle | **BLOCKED** — market closed, 0 orders/fills/positions | `/market/execution/status` |
| Continuous autonomy | **NOT DEMONSTRATED** — disabled/stopped | `/market/autonomy/status` |
| Current exhaustive control replay | **PARTIAL** — 85 current operations, not all 470 | `.ui_audit/qa_controls_*.json` |

---

## 2. Overall verdict

**PARTIAL.**

The application is functioning correctly at the route, API-integration, accessibility, security, regression and isolated-write levels. The accepted NM-01–NM-14 and AT-01–AT-06 baselines remain intact. There is no confirmed open P0 or P1 defect and no evidence of a live trading path.

A full PASS requires evidence this pass does not have: every current control re-operated, a live/isolated paper-order lifecycle including an exit, and a sustained autonomous cycle. Those are reported as **BLOCKED** or **PARTIAL**, not as passes. The historical 9–10 November PASS records are preserved under `docs/archive/qa_2026-11-09/`; they are not presented as fresh evidence.

---

## 3. NM-14 baseline

The accepted NM-14 report records a 25-route, 470-control post-fix PASS, 18 wallets / 40 ledger entries, a deterministic end-to-end paper chain, and three fixed UI/read-model defects. This re-audit preserved that baseline and rechecked the current tree independently:

- 26 routes now exist; the new route is `autonomy`;
- the route namespace test was stale at 221 and was updated to the actual 258 decorators;
- the 10 November 470-control matrix remains valid historical evidence for the 25 routes that existed then;
- no accepted financial policy, risk threshold, ledger rule or paper-only boundary was edited in this pass.

---

## 4. Repository and environment

| Item | Value |
| --- | --- |
| Branch | `novamarket/nm-01` |
| HEAD | `afa5ff64c7673553b333fd3cff07cf5dc7c832b7` |
| Initial working tree | 176 entries; no commit, staging, push or tag performed |
| QA API | `http://127.0.0.1:8012` (isolated `.ui_audit/data`) |
| QA UI | `http://127.0.0.1:5174` (Vite proxy to 8012) |
| Production API/UI | `127.0.0.1:8011` / `127.0.0.1:5173` — read-only inspection |
| OS | Windows, Chromium via Playwright, Python 3.13 |
| Safety flags | `paper_only=true`, `live_trading=false`, `broker_connected=false` |

The first live 5173 sweep produced five 429 pages because the 120 requests/minute limiter was exhausted by the rapid sweep. That was a test-rate artifact, but it exposed the real UX defect fixed below. The isolated API removed the limiter only for QA.

---

## 5. All routes tested

| # | Route | Load | Purpose-level result |
| ---: | --- | --- | --- |
| 1 | `dashboard` | PASS | PASS — 5 priced pulse rows, horizon cards, chart, alerts |
| 2 | `market` | PASS | PASS — search reaches 8 RELIANCE rows after settlement; NSE filter 50 rows; sort works; pagination only partial |
| 3 | `orderblock` | PASS | PASS — board and filters render; no error |
| 4 | `tradingwatch` | PASS | PASS — desk, filters and navigation render |
| 5 | `events` | PASS | PASS — empty-state copy is correct with no matching events |
| 6 | `discovery` | PASS | PASS — discovery surface renders |
| 7 | `watchlists` | PASS | PASS — create/add/remove persisted in isolated store |
| 8 | `forecasts` | PASS | PASS — stored forecast table renders |
| 9 | `portfolios/short` | PASS | PASS — pause/resume and restore verified |
| 10 | `portfolios/medium` | PASS | PASS — empty-book enablement verified |
| 11 | `portfolios/long` | PASS | PASS — empty-book enablement verified |
| 12 | `analytics` | PASS | PASS — book-of-books surface renders |
| 13 | `strategies` | PASS | PASS — catalog and evaluation surface renders |
| 14 | `risk` | PASS | PASS — stress test and alert controls render |
| 15 | `paper` | PASS | PASS — submit defaults disabled without an approved proposal; 0 orders |
| 16 | `backtesting` | PASS | PASS — existing result surface renders |
| 17 | `shadow` | PASS | PASS — create-session validation dialog works |
| 18 | `learning` | PASS | PASS — governance/approval queue renders; no promotion |
| 19 | `wallet` | PASS | PASS — create/deposit/withdraw/reconcile flows pass |
| 20 | `transactions` | PASS | PASS — wallet filter changes the result set |
| 21 | `ledger` | PASS | PASS — wallet filter changes the result set |
| 22 | `notifications` | PASS | PASS — dismiss and restore pass |
| 23 | `activity` | PASS | PASS — timeline and audit tabs render |
| 24 | `health` | PASS | PASS — dependencies, lifecycle and capability table render |
| 25 | `settings` | PASS | PASS — connection, boundaries and alert settings render |
| 26 | `autonomy` | PASS | PASS — all mutating controls locked without an operator token; no live path |

Direct URL access, navigation, refresh, repeated navigation and empty-state routes were exercised in the route sweep. Invalid-route handling was covered by the SPA fallback; no route produced an error boundary.

---

## 6. Page-by-page findings

### Dashboard

The dashboard renders the portfolio hero, trajectory chart, wallet summary, horizon cards, market pulse, intelligence, allocation, alerts, watchlists and activity. The fresh workflow check observed five priced pulse rows and no “Backend offline” copy. The 429 copy fix was verified here.

### Market

The market board loads 50 rows. A search for `RELIANCE` eventually returns 8 rows; the request `...?query=RELIANCE` returns HTTP 200. The table takes roughly two seconds to settle, during which a loading block is visible; this is not a missing-data defect. The NSE exchange filter returns 50 rows and the sort control changes the first row. The current pagination assertion was not re-proven and is marked **PARTIAL**.

### Portfolio, wallet and records

The isolated write flows verified watchlist create/add/remove, wallet create/deposit/withdraw/reconcile, notification dismiss/restore, three portfolio new-book enablement checks, and wallet filters on transactions and ledger. The one automated “BLOCKED” result was a harness artifact: the script looked only for `Pause` while the selected book was already paused; a targeted retest found `Resume`, paused, and restored the original state.

### Paper, backtesting, shadow and learning

The paper surface correctly has no orders/fills. The submit dialog is disabled when no approved proposal exists; a direct probe confirmed the disabled state, and no order was created. The backtesting result surface rendered. Shadow session creation validated required fields and closed without creating a session. Learning governance showed a clear approval queue and no automatic promotion.

### Autonomy

The route renders the runtime summary, operator-token field, lease, safety boundaries, monitoring, decision pipeline, NM-10 execution, AT-05 exit supervision and reliability line. `Start monitoring`, `Forget token` and `Run monitoring cycle` were disabled at rest; all are operator-token-gated. No runtime was started and no order was created.

---

## 7. Buttons and interactive controls

The accessibility pass walked **1,140 controls** across 26 routes. Static checks found 0 unlabeled form controls, 0 unnamed buttons, 0 unnamed links, 0 missing image alt text, 0 duplicate IDs, 0 heading-order issues, 0 table-header issues, 0 contrast failures and 0 focus gaps. Keyboard navigation reached the skip link, navigation, and route changes; the modal trapped focus, closed on Escape and restored focus.

This pass additionally operated **85 current controls**:
- 16 in group A, 17 in group B, 28 in group C and 24 in group D — all PASS;
- 3 Autonomy controls BLOCKED because no dedicated operator token was configured/presented.

Control families exercised included primary/secondary buttons, navigation, search inputs, selects, checkboxes, number inputs, tabs, table sort buttons, row actions, refresh buttons, modal open/close, confirmation dialogs, wallet filters and route navigation. Pagination remains separately marked PARTIAL. The exhaustive historical inventory remains `docs/NM14_UI_CONTROL_MATRIX.json` (470 entries, 25 routes); it is not claimed as a fresh 470/470 pass.

---

## 8. Filters, search and sorting

| Surface | Result | Evidence |
| --- | --- | --- |
| Market symbol search | PASS — `RELIANCE` returns 8 rows after approximately 2 s | direct UI trace; request HTTP 200 |
| Market exchange filter | PASS — NSE returns 50 rows; all rendered rows carry NSE | current browser pass |
| Market sort | PASS — first row changes after selecting Top gainers | current browser pass |
| Market pagination | **PARTIAL** — not re-proven after the current data/settle change | prior stateful retest only |
| Wallet filter (transactions/ledger) | PASS — result set changes to the selected wallet/book | `write_flows2.json` |
| Portfolio book selection | PASS — pause/resume state restored | targeted retest |
| Watchlist membership | PASS — create/add/remove persisted | `write_flows2.json` |
| Empty search/clear | PASS — clear restores the larger set | current browser pass / prior matrix |

No filter was found to silently discard valid rows. The market table’s two-second settle is a UX latency observation, not a data-loss defect.

---

## 9. Graphs and visualizations

Charts and SVG visualizations render on the dashboard, market, analytics, portfolio and other pages. The browser sweep found no chart error state and the accessibility pass found no missing accessible name on chart controls. The dashboard chart was checked for readable Y-axis values in the earlier post-fix baseline; this pass did **not** re-compare every plotted point to its backend series. Chart-value verification is therefore **PARTIAL**, not PASS.

No synthetic data was generated for this pass. The displayed market data remains provider/observation data; model-derived forecasts and simulated fills remain labelled as such by the existing UI.

---

## 10. Tables and data grids

The 26-route pass found **10 tables**: watchlists, forecasts, analytics, strategies (2), backtesting, wallet, transactions, ledger and health. All table headers were present and accessible; the sortable tables exposed `aria-sort`. No row duplicated into an error boundary. The wallet/ledger filters and watchlist write flows verified live data changes. A complete row-by-row comparison of every table against every API response was not repeated; that remains **PARTIAL** in this pass.

---

## 11. Workflow testing

| Workflow | Result | Notes |
| --- | --- | --- |
| A. Market intelligence | PASS | Dashboard and market board; search/filter/sort |
| B. Watchlists/discovery | PASS | Create/add/remove/persistence |
| C. Futures and options | PASS surface | Contract/expiry surface renders; no fresh full contract-count reconciliation |
| D. NovaCoin wallet | PASS | Create/deposit/withdraw/reconcile; no real money |
| E. Portfolio management | PASS | Pause/resume, empty-book creation prerequisites |
| F. Strategy engine | PASS surface | Catalog/evaluation surface; no fresh live proposal |
| G. Risk engine | PASS | Stress test dialog and existing risk alerts |
| H. Paper execution | **PARTIAL** | Submit disabled without an approved proposal; no order/fill created |
| I. Backtesting | PASS surface | Existing run surface; fresh full replay not run |
| J. Shadow mode | PASS surface | Create dialog validates and closes; no session retained |
| K. Adaptive learning | PASS | Governance queue clear; no automatic promotion |

The paper and live-market limitations are the principal reason the overall verdict is PARTIAL.

---

## 12. Financial calculation verification

- Wallet create/deposit/withdraw/reconcile workflows passed against the isolated store.
- Current isolated snapshot: **16 wallets, 36 ledger entries, 7 portfolios, 0 paper orders, 0 fills, 2 backtests, 0 shadow sessions, 0 learning experiments/candidates, 4 open risk alerts**.
- The consolidated suite includes wallet, ledger, portfolio, risk, execution, backtest, shadow and learning tests; **978 passed, 2 skipped, 991 subtests**.
- No P0 financial corruption or ledger mismatch was observed. A complete live paper fill/P&L reconciliation is **BLOCKED** because no order or position exists.

No financial formula was changed during this re-audit.

---

## 13. Frontend/backend integration

Write-flow checks verified the complete chain for watchlists, wallets, notifications, portfolio toggles and record filters: user action → API request → service/store → response → UI refresh. The isolated API logged successful requests; no “success” toast appeared on a failed mutation. The 429 UI fix explicitly keeps a rate limit distinct from an offline backend.

The regression suite covers the API surface and route contract. `docs/API.md` was out of sync with the route-consumer registry and was regenerated; `tests/test_api_reference_sync.py` then passed **6 tests / 630 subtests**.

---

## 14. Autonomous paper trading

AT-01–AT-06 are implemented and accepted: lease/mode runtime, session-aware monitoring, NM-09 → NM-08 decision recording, AT-04 NM-10 paper forwarding, AT-05 protective exits and AT-06 reliability/recovery. No live broker or live-order path exists.

Current runtime evidence:
- `/market/autonomy/status`: **disabled/stopped**, no lease;
- `/market/execution/status`: **0 orders, 0 fills**, paper-only;
- no open positions, no shadow session, no learning candidate;
- no continuous autonomous cycle was run in this pass.

A single manual/API paper trade and the deterministic chain are proven by the accepted baseline and tests; **continuous autonomous operation is not demonstrated**. This is reported as a limitation, not as a pass. See `docs/AUTONOMOUS_PAPER_TRADING_AUDIT.md`.

---

## 15. Performance and reliability

The isolated API performance probe passed **10/10 checks**:

| Check | Measurement |
| --- | --- |
| Concurrent load | 120 requests, 12.34 req/s, p95 1,794.86 ms (limit 3,000 ms), 0 errors |
| Soak | 30 s, p95 184.71 ms, 0 errors |
| Memory | 167.88 → 170.23 MB, +2.35 MB |
| Recovery | interrupted request timed out; recovery 2,439.9 ms; status 200 after |
| Full scan | first 2,571.25 ms; cached 15.44/10.52 ms |
| Browser | 25-route sweep, max heading 261.82 ms, max settle 1,175.61 ms; 0 console/page errors |

This is a local development-server measurement, not a production capacity certification. The browser performance sweep covers 25 routes; the separate route sweep covers all 26.

---

## 16. Responsive design and accessibility

- All 26 routes were rechecked at 320 px; final `mobile_overflow_routes = 0`.
- **1,140 controls** checked; **0** accessibility issue classes.
- Modal focus trap, Escape close, focus restoration, skip link, navigation labels and heading hierarchy passed.
- A transient overflow signal on `health` and `autonomy` was traced to the accessibility probe measuring 150 ms into the sidebar’s 220 ms transition. After a 400 ms settle, both pages measure 320 px with no overflowing visible elements. The probe was corrected; this was a harness defect, not a product defect.
- A 200% CSS-zoom simulation earlier showed horizontal overflow on dashboard, market and settings. This was not re-verified in a real browser zoom and remains an open P3 observation, not a claimed accessibility failure.

This is not a WCAG conformance certification.

---

## 17. Bugs discovered

| ID | Severity | Type | Title | Status |
| --- | --- | --- | --- | --- |
| BUG-QA-429 | P2 | UX/error handling | 429 rendered as “Backend offline” | **FIXED** |
| TEST-QA-ROUTE | P3 | Test contract | Route namespace expected 221, actual 258 | **FIXED** |
| DOC-QA-API | P3 | Documentation contract | `docs/API.md` out of sync after new routes | **FIXED** |
| QA-HARNESS-MOBILE | P3 | Test harness | 150 ms mobile probe raced the 220 ms sidebar transition | **FIXED** |
| OBS-QA-ZOOM | P3 | Responsive observation | 200% CSS zoom simulation overflow on 3 routes | **OPEN** |
| QA-COVERAGE | Process | Evidence gap | Not all 470 historical controls re-operated | **PARTIAL** |
| QA-LIVE-LIFECYCLE | External/state | Evidence gap | No open market, orders or positions | **BLOCKED** |
| QA-AUTONOMY-LIVE | State | Evidence gap | Runtime disabled; no continuous cycle | **NOT DEMONSTRATED** |

No P0 or P1 defect was confirmed. Full details and the prior NM-R1 register are in `docs/BUG_FIX_REGISTER.md`.

---

## 18. Root causes

- **429 copy:** the dashboard/header error path treated every fetch failure as connectivity loss, mapping HTTP 429 to “Backend offline.”
- **Route test:** the test pinned an old decorator count; the actual route registry had grown to 258.
- **API reference:** new autonomy routes were added to the consumer registry without regenerating `docs/API.md`.
- **Mobile probe:** the probe changed viewport and measured after 150 ms, while the sidebar has a 220 ms transform transition, producing a transient `scrollWidth` above the viewport.
- **200% zoom observation:** complex tables and fixed minimum widths still produce horizontal scrolling under a 200% CSS-zoom simulation; not yet reproduced with browser zoom.

---

## 19. Fixes applied

1. `web/src/App.tsx` and `web/src/pages/Dashboard.tsx`: an HTTP 429 now renders rate-limit/busy copy, never “Backend offline”; retry guidance remains.
2. `tests/test_novamarket.py`: expected decorator count updated from 221 to 258.
3. `docs/API.md`: regenerated from the route-consumer registry (630 routes).
4. `tmp/full_qa/a11y_probe_26.py`: mobile measurement waits 400 ms for the sidebar transition.
5. No financial, risk, ledger or paper-only configuration was modified.

---

## 20. Verification of each fix

| Fix | Verification |
| --- | --- |
| 429 copy | Browser interception test: `rate_limited=true`, `offline=false`; build passed |
| Route count | Targeted suite: 139 passed, 284 subtests; consolidated suite 978 passed |
| API reference | `generate_api_reference.py --check` in sync; 6 tests / 630 subtests passed |
| Mobile probe | Final 26-route a11y: 1,140 controls, 0 overflow, 0 console errors |
| Build | `npm run build`: 1,599 modules, JS 602.85 kB / gzip 160.88 kB |

The earlier dashboard horizon, market-pulse and flat-chart fixes remain in the historical evidence and regression suite; they were not reverted.

---

## 21. Remaining issues

1. **Live paper lifecycle** — no order/fill/exit could be executed in the current closed-market, zero-position state.
2. **Continuous autonomy** — runtime was disabled; no sustained cycle, scheduler/lease soak or recovery-after-restart was demonstrated live.
3. **Provider outage/failover** — no live provider failure drill; deterministic provider tests only.
4. **Exhaustive control replay** — the current pass covers 85 explicit operations plus 1,140 accessibility-scanned controls, not a fresh 470/470.
5. **Pagination and full table/chart cross-checks** — not re-proven for every current table in this pass.
6. **200% CSS-zoom observation** — open P3; not a confirmed product regression.
7. **Operator-token configuration** — the isolated environment has no dedicated token, so restricted controls are correctly locked and cannot be exercised.
8. **Prior NM-R1 open items** — paused-mode decision button, recovery UI visibility and authentication configuration remain documented in the prior register.

---

## 22. Requirements compliance

NM-01–NM-14 and AT-01–AT-06 are traced in `docs/REQUIREMENTS_TRACEABILITY_REPORT.md`. The accepted implementation is present and the regression suite passes; the application-level runtime limitations above prevent a full PASS. No new requirement was invented and no accepted requirement was removed.

---

## 23. Test results

| Test | Result |
| --- | --- |
| Consolidated NovaMarket suite | **978 passed, 2 skipped, 607 warnings, 991 subtests** in 384.91 s |
| Targeted API/UI suite | **139 passed, 284 subtests** |
| Route sweep | **26/26 clean** |
| Accessibility | **PASS**, 26 routes / 1,140 controls |
| Performance | **PASS**, 10/10 API checks |
| Security | **PASS**, 9/9 checks |
| Frontend build | **PASS** |
| `mypy` | **PASS**, no issues in 573 source files |
| API reference check | **PASS**, 630 routes |
| Git hygiene | No commit/stage/push/tag; `git diff --check` clean |

The two skipped tests were reported by pytest; the run did not include `-rs`, so their individual skip reasons are not itemized here. They were not forced to run.

---

## 24. Safety verification

- `paper_only=true`
- `live_trading=false`
- `broker_connected=false`
- no live execution endpoint or broker order path
- no real-money credentials
- no production database mutated by the QA writes
- all write flows used the isolated `.ui_audit/data` store
- security probe: security headers, foreign origin/host rejection, same-origin validation, config-secret scan, unsafe HTML scan, live-path scan, token enforcement and rate limit all pass

The 429 UX fix does not weaken the server-side limiter.

---

## 25. Final recommendations

1. Run the next acceptance pass with a verified open session (or the deterministic fixture harness explicitly labelled) so a paper order, fill, position, protective exit and ledger reconciliation can be demonstrated end to end.
2. Configure a dedicated `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN` in an isolated environment and perform a bounded monitoring/autonomous cycle with a seeded, clearly synthetic fixture; do not enable live trading.
3. Re-run the full current control matrix on the 26-route build, including pagination and table/chart value checks, before claiming 470/470 or 520/520.
4. Keep the 429 copy and add a browser regression for 429/offline distinction.
5. Resolve the open P3s from `BUG_FIX_REGISTER.md` (zoom, paused decision button, recovery UI visibility) in a separate approved change.
6. Do not treat deterministic fixtures as live-provider reliability evidence; a provider outage drill is still outstanding.

---

## Evidence index

| Artifact | Path |
| --- | --- |
| Route sweep | `tmp/full_qa/route_sweep.json` |
| Accessibility (26 routes) | `tmp/full_qa/a11y_results_26_final.json` |
| Performance | `tmp/full_qa/performance.json` |
| Security | `.ui_audit/security_results.json` |
| Write flows | `.ui_audit/write_flows2.json` |
| Current controls A–D | `.ui_audit/qa_controls_A-D.json` |
| Consolidated regression | `.ui_audit/qa_full_regression2.log` |
| Build | `web/dist/` (generated; not committed) |
| Historical 470 matrix | `docs/NM14_UI_CONTROL_MATRIX.json` |
| Prior accepted reports | `docs/archive/qa_2026-11-09/` |
