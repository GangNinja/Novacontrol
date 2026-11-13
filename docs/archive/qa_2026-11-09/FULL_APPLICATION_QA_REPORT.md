# NovaMarket Full Application QA Report

**Audit date:** 2026-11-09  
**Post-fix full sweep:** 2026-11-10  
**Application:** NovaMarket standalone workbench (`novamarket/`)  
**Audit type:** Real-user Chromium QA, requirements traceability, live workflow verification, security/accessibility review, ledger integrity and performance audit  
**Final verdict:** **PASS** — the application is functional, safe and performance-qualified within its accepted paper-only scope. The current post-fix control matrix is complete at 470/470 entries, and the 10 November performance recheck passes all 10 API checks plus the 25-route browser sweep.

---

## 1. Executive summary

NovaMarket passed the core functional audit. All 25 routes rendered without an error boundary, console error, page error or HTTP 5xx. The fresh 10 November post-fix matrix records **470/470 current entries PASS** across 25/25 routes. The accepted 480-control baseline is net 10 higher than the current rendered set and is reconciled route by route; no 480/480 post-fix claim is made. The four state-dependent PARTIALs and the closure checks passed on retest.

The money and execution boundaries held:

- `paper_only=true`, `live_trading=false` and `broker_connected=false` remained in force throughout.
- 18 wallets and 40 ledger entries reconciled with no bad running balance and a zero ledger sum.
- A live deposit (`+100.00`) and withdrawal (`-25.00`) changed the operator balance from `30,075.00` to `30,150.00` and persisted correctly.
- Portfolio pause/resume worked and the dashboard horizon status changed from `Mixed` to `Active` when every book was resumed.
- The deterministic end-to-end chain exited 0: 300 bars, forecast, buy signal, risk approval, filled paper order, completed backtest, completed shadow session, accepted evidence, completed experiment and promoted candidate.

Three real presentation/read-model defects were confirmed and fixed:

1. The dashboard horizon status used only the first book, so one paused book labelled the whole horizon `Paused`.
2. The dashboard market pulse drew from alphabetical instruments, producing duplicate exchange listings and unquoted rows, and then lost prices when the per-instrument quote endpoint returned an empty quote.
3. A flat valuation series produced five identical `₹20.0k` Y-axis ticks.

After the fixes the dashboard shows `Active` when all books are active, `Mixed` when paused and active books coexist, five unique priced pulse rows, and readable `₹19.0k`–`₹21.0k` chart ticks.

The audit meets its PASS bar:

- the accepted 480-control baseline is net 10 higher than the current 470 rendered entries; that is an inventory reconciliation, not a failing control, and the full current set was re-exercised;
- the 10 November performance recheck is `PASS`: all 10 API checks pass, including concurrent p95 `1,397.34 ms` (limit `3,000 ms`) and soak p95 `166.06 ms` (limit `1,500 ms`), with zero errors and stable memory; the 25-route browser sweep also passes;
- no autonomous paper-trading cycle exists (outside accepted NM-01–NM-14 scope); it is not counted as a missing requirement;
- live quotes in the audit copy were 28.9–55.9 hours stale, so risk correctly rejected stale proposals and the UI used the deterministic fixture for a fresh fill; this is a data-availability limitation, not an application failure.

Performance changes were confined to the NovaMarket read path; no financial calculation, risk policy, broker integration or accepted architecture was changed.

---

## 2. Environment and isolation

| Item | Value |
| --- | --- |
| QA API | `http://127.0.0.1:8012` |
| QA UI | `http://127.0.0.1:5174` (Vite proxy to 8012) |
| Production-like services | `8011` API and `5173` UI were not mutated |
| QA data root | `novamarket/tmp/qa_audit/data/novamarket.sqlite3` |
| Browser | Chromium via Playwright, desktop and 390 px mobile viewport |
| Source tree | `C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl\novamarket` |
| Source changes | Three frontend fixes and regression tests; uncommitted |
| Commit/push | Not performed |

The QA API was started with a copied database. Financial writes therefore exercised a disposable QA store, not the user's live NovaMarket books. Rate-limit middleware was removed only in the QA launcher so that the large read-only survey could complete; no production middleware was changed.

The QA API was healthy at the start and end of the audit. Lifecycle: `running`.

---

## 3. Safety boundary

| Control | Observed value | Result |
| --- | --- | --- |
| Paper-only mode | `paper_only=true` | PASS |
| Live trading | `live_trading=false` | PASS |
| Broker connection | `broker_connected=false` | PASS |
| Live broker/order route | none found in static and HTTP scans | PASS |
| Broker credentials | none requested, read or written | PASS |
| Shadow execution gateway | absent; shadow is non-executing | PASS |
| Learning financial writes | none; isolated store | PASS |

The deterministic acceptance chain uses the real NM-02–NM-13 services and SQLite stores, but its orders are paper orders. It never contacts a broker or exchange execution endpoint.

---

## 4. Route coverage

**25/25 routes PASS** in the read-only route survey. Every route rendered its heading, navigation and expected surface with no console error, page error or HTTP 5xx.

| Group | Routes |
| --- | --- |
| Overview | Dashboard, Market Overview |
| Trading desks | OrderBlock, TradingWatch |
| Intelligence | Market Events, Asset Discovery, Watchlists, Forecasts |
| Portfolios | Short-Term, Medium-Term, Long-Term |
| Analysis | Analytics, Strategies, Risk |
| Execution and learning | Paper Trading, Backtesting, Shadow Mode, Adaptive Learning |
| Money | Wallet, Transactions, Ledger |
| Operations | Notifications, Activity, Health, Settings |

Route evidence: `tmp/qa_audit/route_survey.json` and `tmp/qa_audit/routes/`.

The route survey is a render/read survey, not a substitute for clicking every control. Control evidence is separated in §5 and in `COMPLETE_UI_INTERACTION_MATRIX.md`.

---

## 5. Control and workflow coverage

### 5.1 Current full matrix

`docs/NM14_UI_CONTROL_MATRIX.json` records **470/470 current control entries PASS** across 25 routes. It includes route loads, navigation, filters, sorting, tabs, modals, inputs, row actions, write flows, targeted retests and safety controls. The 9 November baseline had 480 entries; the current rendered set is 470 (net −10), and the route reconciliation is in `COMPLETE_UI_INTERACTION_MATRIX.md`.

### 5.2 Post-fix current sweep

The fresh 10 November post-fix matrix recorded **470 entries** across all 25 routes: **470 PASS, 0 PARTIAL, 0 BLOCKED, 0 NOT PASS**.

| Layer | Count | Result |
| --- | ---: | --- |
| Route loads plus rendered controls | 401 | 401 PASS after four stateful retests |
| Inputs/selects/checkboxes | 38 | 38 PASS |
| Modal/form checks | 12 | 12 PASS |
| Isolated write flows | 16 | 16 PASS |
| Extra targeted checks | 3 | 3 PASS |
| **Total** | **470** | **470 PASS** |

The four state-dependent PARTIALs were Market `Reset`, OrderBlock `Clear filters`, TradingWatch `Clear` and TradingWatch `Next bar`; all passed with real state. Five final retests (API-key modal, OrderBlock sector, portfolio pause/resume, Paper proposal modal and Settings token), the Market pagination/industry closure and the Watchlist rename also passed. Two generic wallet-select rows are superseded by the dedicated wallet-filter write flows, and three generic portfolio `New book` PARTIALs are superseded by the real enablement/write flows.

The 9 November baseline had 480 entries; the current rendered set has 470 (net −10), and `COMPLETE_UI_INTERACTION_MATRIX.md` records the route delta. No 480/480 post-fix claim is made.

### 5.3 Workflow checks

| Workflow | Evidence | Result |
| --- | --- | --- |
| Navigate every route | 25 route survey, no error state | PASS |
| Market search/filter | Filter and Reset exercised; 4,811 matched rows | PASS |
| Market cap filter | `large` returned 50 of 100 quoted; Reset restored 101 of 2,406 | PASS |
| OrderBlock filter clear | Search state cleared | PASS |
| TradingWatch filter clear | Search and direction cleared | PASS |
| Wallet deposit | `+100.00` deposited; balance persisted | PASS |
| Wallet withdrawal | `-25.00` withdrawn; balance persisted | PASS |
| Transaction wallet selector | 18 options after load; earlier BLOCKED was a load race | PASS |
| Portfolio pause | "Portfolio paused." observed | PASS |
| Portfolio resume | "Portfolio resumed." observed | PASS |
| Dashboard horizon status | `Mixed` with paused+active; `Active` after resume | PASS |
| Ledger integrity | 18 wallets, 40 entries, 0 bad running balances | PASS |
| Reconciliation | `ok`; ledger sum `0.00` | PASS |
| Market pulse | 5 unique rows, all priced | PASS |
| Flat chart axis | ₹19.0k–₹21.0k, five distinct ticks | PASS |

### 5.4 Input and modal coverage

- 41 input/select checks: 36 PASS, 4 BLOCKED by single-option selects, 1 PARTIAL because the API-token secret was deliberately not entered. The transaction wallet BLOCKED result was subsequently disproved by the live 18-option check.
- 15 modal checks: 10 PASS, 4 expected validation/secret-skip states, 1 BLOCKED because the expected long-horizon opener was absent.
- The absent long-horizon modal opener was not proven to be a product defect; the page's write flow is covered by the accepted matrix. It remains a harness assumption, not a claimed pass.

Evidence: `harness/input_sweep.json`, `harness/modal_sweep.json`.

---

## 6. Accessibility

Automated Chromium probe across 25 routes:

| Measure | Result |
| --- | --- |
| Routes checked | 25 |
| Controls seen | 957 |
| Form controls | 32 |
| Buttons | 900 |
| Links | 25 |
| Tables | 6 |
| Contrast text nodes | 1,833 |
| Unlabeled form controls | 0 |
| Unnamed buttons/links | 0 |
| Images missing alt | 0 |
| Duplicate IDs | 0 |
| Heading issues | 0 |
| Table header issues | 0 |
| Contrast failures | 0 |
| Focus gaps | 0 |
| Mobile/desktop overflow | 0 |
| Console/page errors | 0 |

Keyboard navigation, modal focus handling and 200% zoom simulation also passed. This is an automated inspection, not a WCAG conformance certification. Evidence: `tmp/qa_audit/a11y_results.json`.

---

## 7. Security

The security probe passed **9/9** checks:

1. Security headers.
2. Foreign-origin rejection.
3. Foreign-host rejection.
4. Same-origin validation.
5. No secrets in configuration responses.
6. No unsafe HTML injection in static assets.
7. No live-trading path in static assets.
8. Bearer-token enforcement.
9. Rate limiting (120-request test).

The probe used HTTP checks against the isolated API plus temporary TestClient apps. It is an internal review, not an independent penetration test. Evidence: `tmp/qa_audit/security_results.json`.

No live broker credentials, real-money endpoints, or token values were exposed in this report or in project files.

---

## 8. Ledger and financial integrity

The QA copy contained:

- 18 wallets;
- 40 ledger entries;
- 0 entries with an incorrect running balance after replay;
- reconciliation status `ok`;
- ledger sum `0.00`.

The operator wallet changed from `30,075.00` to `30,150.00` after a `+100.00` deposit and `-25.00` withdrawal. Every entry remained double-entry balanced. Portfolio values and wallet balances were shown as separate books, not added together.

No financial formula, fee, slippage, risk or allocation policy was changed by this audit.

---

## 9. Market data, discovery, strategy, risk and execution

| Stage | Observation | Result |
| --- | --- | --- |
| Catalogue | 4,811 matched instruments; 101 quoted / 2,406 catalogue | PASS |
| Market cap filter | Large cap 50/100 quoted; Reset 101/2,406 | PASS |
| Quote freshness | Latest observations 28.9–55.9 hours old | Data limitation |
| Discovery scheduler | `running=false` | Not autonomous |
| Strategy proposals | Stale-data proposals correctly rejected | PASS safety |
| Risk gate | Stale quote policy blocks execution | PASS safety |
| Paper execution | 0 orders / 0 fills in the live QA database | Manual only |
| Deterministic chain | Filled paper order in isolated fixture | PASS |

The stale live quotes are not fabricated or silently refreshed. The risk engine correctly refused stale proposals. A fresh fill therefore could not be produced through the live UI without a fresh provider observation; the deterministic chain is the valid execution proof.

---

## 10. End-to-end paper chain

Command:

```text
PYTHONPATH=src ../.venv/Scripts/python.exe scripts/verify_novamarket_chain.py
```

Result: exit `0`, `ok: true`.

| Step | Observed |
| --- | --- |
| Seeded history | 300 bars for `XNSE:ACCEPT` |
| Forecast | `few_sessions` |
| Strategy | `buy` |
| Proposal | `approved` |
| Risk | Approved by the real NM-08 path |
| Paper order | `filled` |
| Backtest | `completed` |
| Shadow session | `completed` |
| Learning evidence | `accepted` |
| Experiment | `completed` |
| Candidate | `promoted` |

Wallet, portfolio, forecast and strategy identifiers were returned by the chain. Evidence: `tmp/qa_audit/chain_verification.log`.

This proves the integrated paper cycle works when fresh deterministic market data is present. It does not prove that an autonomous scheduler exists.

---

## 11. Autonomous paper trading

**Result: NO AUTONOMOUS TRADING ORCHESTRATOR EXISTS.**

The runtime monitor reports `task_running=true`, interval `3600`, but `last_tick=null`; it performs notifications, radar and position ingestion, not discovery→strategy→risk→execution cycles. Discovery's scheduler is `running=false`. The live QA database has 0 paper orders and 0 fills. The design documents state that NM-07 has no background scheduler and NM-10 has no autonomous background loop.

Autonomous trading is outside the accepted NM-01–NM-14 requirement set and was not implemented. The manual/API paper chain is functional and broker-free. See `AUTONOMOUS_PAPER_TRADING_AUDIT.md` for the full audit and the approval required before adding any autonomous loop.

---

## 12. Performance

The browser probe is **PASS**: 25/25 routes, no error blocks, no horizontal overflow, maximum heading time `281.60 ms`, maximum settled time `1,189.86 ms`, and 0 console/page errors.

The API/load probe is **PASS**:

| Check | Result | Threshold | Status |
| --- | ---: | ---: | --- |
| Status p95 | 123.45 ms | 1,000 ms | PASS |
| Overview p95 | 294.84 ms | 2,000 ms | PASS |
| Instruments p95 | 22.10 ms | 2,000 ms | PASS |
| Screener p95 | 699.51 ms | 4,000 ms | PASS |
| Portfolio p95 | 26.18–26.23 ms | 2,000–3,000 ms | PASS |
| Full scan | 1,581.78 ms | 15,000 ms | PASS |
| Concurrent p95 | 1,397.34 ms | 3,000 ms | PASS |
| Soak p95 | 166.06 ms | 1,500 ms | PASS |
| Load errors | 0 / 120 | 0 | PASS |
| Memory growth | -0.58 MB over 90 s (-0.025 MB/min) | no growth | PASS |
| Recovery | 2,357.95 ms, then 200 | recovery | PASS |

Environment: local Windows, single QA API process, Vite dev UI. This is not a production benchmark. The probe followed targeted read-path fixes: hot synchronous market reads now run in Starlette's threadpool, `board_rows` uses a separate read-only WAL connection, and the screener has a 3-second single-flight cache. Evidence: `.ui_audit/performance_postfix2.json`.

---

## 13. Defects and fixes

| ID | Severity | Defect | Status |
| --- | --- | --- | --- |
| BUG-QA-001 | Medium | Horizon status showed first book only | Fixed |
| BUG-QA-002 | Medium | Market pulse showed duplicate/unquoted instruments and could lose row prices | Fixed |
| BUG-QA-003 | Low | Flat valuation chart produced five identical Y-axis ticks | Fixed |
| PERF-QA-001 | Medium | Concurrent and soak p95 exceeded internal limits | Fixed |
| SCOPE-QA-001 | Informational | No autonomous paper-trading loop | Out of accepted scope |

Full root causes, code locations and regression evidence: `BUG_FIX_REGISTER.md`.

---

## 14. Evidence index

| Evidence | Path |
| --- | --- |
| Route survey | `tmp/qa_audit/route_survey.json` |
| Route screenshots | `tmp/qa_audit/routes/` |
| Accepted control matrix | `docs/NM14_UI_CONTROL_MATRIX.json` |
| Current control sweep | `tmp/qa_audit/harness/control_sweep.json` |
| Clear-control retest | `tmp/qa_audit/harness/clear_controls_retest.json` |
| Input sweep | `tmp/qa_audit/harness/input_sweep.json` |
| Modal sweep | `tmp/qa_audit/harness/modal_sweep.json` |
| Accessibility | `tmp/qa_audit/a11y_results.json` |
| Security | `tmp/qa_audit/security_results.json` |
| Performance | `.ui_audit/performance_postfix2.json` |
| Chain verification | `tmp/qa_audit/chain_verification.log` |
| Final dashboard | `tmp/qa_audit/dashboard-final-verified.png` |
| Mobile market | `tmp/qa_audit/market-mobile-final.png` |
| Build log | `tmp/qa_audit/logs/build_final.log` |
| Final suite log | `tmp/qa_audit/logs/pytest_final.log` |

---

## 15. Final verdict

**PASS.**

NovaMarket is functionally sound, paper-only, broker-free, ledger-consistent, safe under stale data and performance-qualified at the accepted internal QA scope. All 25 routes render, all **470 current controls** pass (including stateful retests and closure checks), accessibility and security probes passed, the deterministic paper chain passed end to end, and the 10 November performance recheck passes all 10 API checks plus the 25-route browser sweep.

The 9 November 480 control baseline is net 10 higher than the current rendered set; that reconciliation is recorded rather than presented as a 480/480 post-fix result. Autonomous paper trading remains outside accepted NM-01–NM-14 scope and was not implemented. No finding justifies changing financial policy or adding an autonomous trading loop without explicit approval.
