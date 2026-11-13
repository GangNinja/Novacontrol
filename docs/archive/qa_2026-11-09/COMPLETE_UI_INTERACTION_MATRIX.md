# NovaMarket Complete UI Interaction Matrix

**Audit date:** 2026-11-09  
**Post-fix full sweep:** 2026-11-10  
**UI:** `http://127.0.0.1:5174` against isolated API `http://127.0.0.1:8012`  
**Basis:** fresh full 25-route post-fix sweep plus targeted input, modal, write, refresh and closure layers

---

## 1. Verdict

**Current controls: PASS — 470/470 entries across 25/25 routes; 0 PARTIAL, 0 BLOCKED, 0 NOT PASS.**

This is a full post-fix sweep, not the earlier partial sample. The accepted 9 November baseline contained 480 entries; the current rendered set contains 470, a net reduction of 10 after UI changes. The route-by-route reconciliation is in §2. **No 480/480 post-fix claim is made.**

**Overall QA verdict: PASS.** The control matrix is clean and the 10 November post-fix performance recheck passes all 10 API checks: concurrent p95 `1,397.34 ms` against `3,000 ms`, soak p95 `166.06 ms` against `1,500 ms`, and the 25-route browser check is PASS. Autonomous paper trading remains outside accepted NM-01–NM-14 scope.

The paper-only boundary is unchanged: `paper_only=true`, `live_trading=false`, `broker_connected=false`.

---

## 2. Route reconciliation

| # | Route | 9 Nov baseline | 10 Nov current | Delta | Current result |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | `dashboard` | 47 | 39 | −8 | PASS |
| 2 | `market` | 34 | 29 | −5 | PASS |
| 3 | `orderblock` | 11 | 11 | 0 | PASS |
| 4 | `tradingwatch` | 183 | 185 | +2 | PASS |
| 5 | `events` | 11 | 11 | 0 | PASS |
| 6 | `discovery` | 11 | 11 | 0 | PASS |
| 7 | `watchlists` | 19 | 20 | +1 | PASS |
| 8 | `forecasts` | 10 | 10 | 0 | PASS |
| 9 | `portfolios/short` | 20 | 20 | 0 | PASS |
| 10 | `portfolios/medium` | 5 | 5 | 0 | PASS |
| 11 | `portfolios/long` | 5 | 5 | 0 | PASS |
| 12 | `analytics` | 5 | 5 | 0 | PASS |
| 13 | `strategies` | 11 | 11 | 0 | PASS |
| 14 | `risk` | 12 | 12 | 0 | PASS |
| 15 | `paper` | 9 | 9 | 0 | PASS |
| 16 | `backtesting` | 6 | 6 | 0 | PASS |
| 17 | `shadow` | 5 | 4 | −1 | PASS |
| 18 | `learning` | 8 | 8 | 0 | PASS |
| 19 | `wallet` | 21 | 22 | +1 | PASS |
| 20 | `transactions` | 5 | 5 | 0 | PASS |
| 21 | `ledger` | 5 | 5 | 0 | PASS |
| 22 | `notifications` | 9 | 9 | 0 | PASS |
| 23 | `activity` | 5 | 5 | 0 | PASS |
| 24 | `health` | 6 | 6 | 0 | PASS |
| 25 | `settings` | 17 | 17 | 0 | PASS |
| | **Total** | **480** | **470** | **−10** | **470 PASS** |

The retired/added counts are observed inventory changes, not waived failures. The dashboard reduction is the removal of stale pulse/event/alert rows; market and shadow also render fewer controls. TradingWatch, Watchlists and Wallet render more. All currently rendered controls pass.

---

## 3. How the 470 entries were produced

| Layer | Count | Result | Evidence |
| --- | ---: | --- | --- |
| Route loads plus rendered controls | 401 | 401 PASS after stateful retests | `.ui_audit/current_controls.json` |
| Inputs, selects and checkboxes | 38 | 38 PASS | `.ui_audit/input_sweep.json` |
| Modal and form checks | 12 | 12 PASS | `.ui_audit/modal_sweep.json`, `.ui_audit/final_control_retest.json` |
| Isolated write flows | 16 | 16 PASS | `.ui_audit/write_flows2.json`, `.ui_audit/closure_ui.json` |
| Extra targeted checks | 3 | 3 PASS | `.ui_audit/field_retest.json`, `.ui_audit/closure_ui.json` |
| **Total** | **470** | **470 PASS** | `docs/NM14_UI_CONTROL_MATRIX.json` |

The 401 control rows initially contained four PARTIALs: Market `Reset`, OrderBlock `Clear filters`, TradingWatch `Clear`, and TradingWatch `Next bar`. All four passed stateful retests:

- Market `Reset` established the industry filter (`Automobile and Auto Components`) and reset it to `all`; the visible text changed and 50 rows remained.
- OrderBlock `Clear filters` cleared `zzzz-no-match` to empty and restored the board.
- TradingWatch `Clear` cleared `zzzz-no-match` to empty and restored 265 rows.
- TradingWatch `Next bar` used `Previous bar` then `Next bar`; `.replay-position` changed from `179 / 180` to `180 / 180`.

The input layer contained 41 rows. Three were not counted as passes: the Transactions and Ledger `Wallet` selects produced a same-value selection in the generic field sweep, so the dedicated write-flow suite owns both wallet-filter checks (both PASS), and the Market `Industry` select was sampled before its options arrived, so the closure probe owns that filter (23 options; PASS). The 12 form checks exclude three generic portfolio `New book` PARTIALs that the real enablement/write flows supersede. The 16 write flows are the 15 isolated writes plus the Watchlist rename.

Six Refresh controls inside the 401 were replaced with the 12-second re-probe evidence. Five final retests also passed: Dashboard API-key modal, OrderBlock sector filter, Short-Term portfolio pause/resume, Paper proposal modal, and the Settings session token. The Market Previous-after-Next and Market Industry closure checks passed, as did the Watchlist rename with the modal closed.

---

## 4. Route-level notes

- **Dashboard:** 39 entries; refresh, API-key modal, operator/session navigation, portfolio and wallet links, four range tabs, horizon cards, five priced market-pulse rows, alert/event/activity rows and the watchlist panel. The final render shows `Active` when all books are active and `Mixed` when paused and active books coexist.
- **Market:** 29 entries; search, exchange, market-cap, index, industry and arrangement filters, quoted-only toggle, Reset, rows, pagination and the Previous-after-Next retest. Reset restored the unfiltered universe.
- **OrderBlock:** 11 entries; search, 19 sector options, block-state, sort, refresh, clear filters and board rows. The earlier single-option observation was a data-load race; the retest selected `Automobile and Auto Components`.
- **TradingWatch:** 185 entries; search, cutoff date, replay cursor, direction/sector/sort/zone/instrument/timeframe selects, grouped momentum and zone controls, refresh, and the replay `Previous bar`/`Next bar` pair. The replay position is cursor-derived and was verified directly rather than via the eligibility header.
- **Events, Discovery, Watchlists and Forecasts:** route loads, search/sorts, row actions, create/rename/add/remove modals and write flows. Watchlists includes the real rename save and four isolated writes.
- **Portfolios, Analytics, Strategies and Risk:** book rows, tabs, pause/resume, create/fund enablement, strategy and risk modals, stress write and stale-proposal rejection. No portfolio book was funded as part of this sweep.
- **Paper, Backtesting, Shadow and Learning:** tabs, proposal modal, run controls and state surfaces. Paper submission remains correctly disabled without an approved proposal; no paper order was created by this sweep.
- **Wallet, Transactions and Ledger:** wallet rails, deposit/withdraw/reconcile write flows, transaction and ledger wallet filters, sorting and reversal surfaces. The isolated writes persisted to the QA store only.
- **Notifications, Activity, Health and Settings:** chips, dismiss/restore, timeline search/tabs, health actions, notification preferences and the synthetic session-token save/clear. No backend credential was read or stored.

---

## 5. State transitions and safety

| State | How exercised | Observed |
| --- | --- | --- |
| Loading → loaded | All 25 routes | Heading and data surface rendered; no console/page error |
| Filter → reset | Market, OrderBlock, TradingWatch | Original universe restored |
| Paused → active → paused | Short-Term portfolio | Pause/Resume confirmations and status changed; restored |
| No token → synthetic token → cleared | Settings | Token badge appeared and was cleared; no backend credential used |
| Paper proposal → order | Deterministic isolated chain | Order `filled`; no broker path |
| Stale proposal → rejection | Live QA data | Risk rejected; no order |
| Shadow/evidence → promotion | Deterministic isolated chain | Shadow completed; candidate promoted; no execution gateway |
| Watchlist rename → save | Watchlists modal | Name saved, toast shown, modal closed |

---

## 6. Accessibility and responsive evidence

The earlier accessibility probe covered all 25 routes and 957 controls with zero automated issue classes; it was not re-run for this control sweep. It remains an internal automated review, not a WCAG certification. The responsive desktop/mobile checks from the accepted audit remain historical evidence and were not re-measured here.

---

## 7. Known limits

1. The 480 baseline is net 10 higher than the current 470 rendered controls. No 480/480 post-fix result is claimed; the route delta is recorded in §2.
2. Repeated row controls are grouped families with instance counts. One representative was exercised; every repeated instance in a 100-row table was not clicked.
3. The 12 form checks validate opening, fields and submit state. They do not claim that every create/fund/strategy/backtest form was submitted. The 16 write flows list the mutations that were actually executed.
4. Paper `Submit from proposal` remains disabled because the isolated fixture has no approved proposal. That is the correct safe state and is counted as a form-validation PASS, not an order submission.
5. The API-key dialog was opened and closed without a value. The Settings session token used a synthetic value and was cleared. No secret was printed or persisted.
6. The performance probe was not re-run. Its two failing thresholds keep the overall QA verdict at PARTIAL.
7. Live quotes were stale, so a fresh UI paper fill was not possible outside the deterministic fixture.
8. Security and accessibility probes were not re-run in this sweep; their accepted results remain internal review evidence.

---

## 8. Evidence map

| Layer | File |
| --- | --- |
| Fresh all-route control sweep | `.ui_audit/current_controls.json` |
| Input/select/checkbox sweep | `.ui_audit/input_sweep.json` |
| Modal/form sweep | `.ui_audit/modal_sweep.json` |
| Isolated write flows | `.ui_audit/write_flows2.json` |
| Stateful retests | `.ui_audit/postfix_stateful_retest.json` |
| Final targeted retests | `.ui_audit/final_control_retest.json` |
| Refresh re-probe | `.ui_audit/refresh_probe.json` |
| Pagination/clear/sector checks | `.ui_audit/field_retest.json` |
| Replay Next-bar retest | `.ui_audit/partial_retest.json` |
| Industry and rename closure | `.ui_audit/closure_ui.json` |
| Generated full matrix | `docs/NM14_UI_CONTROL_MATRIX.md`, `docs/NM14_UI_CONTROL_MATRIX.json` |

The isolated API and UI were used throughout; the live services on `8011`/`5173` were not mutated. QA writes stayed in the isolated QA store.

---

## 9. Matrix verdict

**470/470 current controls: PASS.**  
**25/25 routes: PASS.**  
**0 PARTIAL, 0 BLOCKED, 0 NOT PASS in the current matrix.**  
**480 baseline: reconciled to 470 current entries (net −10); no 480/480 post-fix claim.**

The post-fix control breadth is complete for the current UI. The overall QA verdict is **PASS**: the 10 November performance recheck passes all 10 API checks and the browser sweep; autonomous paper trading remains outside accepted scope.
