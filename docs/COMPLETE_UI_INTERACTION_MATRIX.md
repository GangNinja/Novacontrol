# NovaMarket Complete UI Interaction Matrix

**Audit date:** 2026-11-11  
**UI:** `http://127.0.0.1:5174` against isolated API `http://127.0.0.1:8012`  
**Basis:** 26-route Chromium sweep, 1,140-control accessibility inspection, 85 explicit current control operations, targeted write/modal workflows, and the preserved 470-entry historical matrix

---

## 1. Verdict

**Current route and interaction surface: PASS with evidence limits.**  
**Complete 470-control re-operation in this pass: PARTIAL.**

> **2026-11-13 closure:** NM-R1-04 and NM-R1-05 are fixed, and the dedicated operator token is configured in the isolated QA environment. This document remains the 2026-11-11 baseline; see `NOVAMARKET_REMEDIATION_REPORT.md` for closure evidence.

All **26/26 routes** opened with a heading, no error boundary and no console/page/request error. The current accessibility matrix inspected **1,140 interactive controls** and found no missing labels, unnamed buttons/links, heading skips, table-header gaps, contrast failures or focus gaps. A separate targeted layer explicitly operated **85 controls**, all PASS, plus **3 Autonomy controls BLOCKED** by the absent dedicated operator token.

The 10 November **470/470** matrix remains valid evidence for the 25 routes that existed then. It is not presented as a fresh 26-route result. The new `autonomy` route adds 50 current interactive nodes and was checked in the current pass, but its mutating controls remain locked at rest.

The paper-only boundary is unchanged: `paper_only=true`, `live_trading=false`, `broker_connected=false`.

---

## 2. Route inventory

The control count is the count of interactive DOM nodes observed by the accessibility probe. It includes repeated global navigation and is not a count of distinct user journeys.

| # | Route | Controls | Tables | Result | Current interaction note |
| ---: | --- | ---: | ---: | --- | --- |
| 1 | `dashboard` | 61 | 0 | PASS | Horizon cards, market pulse, chart, alert links, refresh |
| 2 | `market` | 58 | 0 | PASS | Search, five selects, sort, row selection, pagination partial |
| 3 | `orderblock` | 40 | 0 | PASS | Board filters and desk navigation |
| 4 | `tradingwatch` | 35 | 0 | PASS | Desk surface, filters, navigation |
| 5 | `events` | 39 | 0 | PASS | Event filters and empty state |
| 6 | `discovery` | 37 | 0 | PASS | Candidate surface, discovery action |
| 7 | `watchlists` | 71 | 1 | PASS | Create/add/remove; membership table |
| 8 | `forecasts` | 40 | 1 | PASS | Forecast table, sort, generate dialog |
| 9 | `portfolios/short` | 50 | 0 | PASS | Pause/resume, book actions |
| 10 | `portfolios/medium` | 36 | 0 | PASS | Empty-book creation enablement |
| 11 | `portfolios/long` | 36 | 0 | PASS | Empty-book creation enablement |
| 12 | `analytics` | 36 | 1 | PASS | Book-of-books table and range controls |
| 13 | `strategies` | 41 | 2 | PASS | Catalog and evaluation tables |
| 14 | `risk` | 42 | 0 | PASS | Stress dialog, alerts, policy controls |
| 15 | `paper` | 40 | 0 | PASS | Submit disabled without approved proposal; no order |
| 16 | `backtesting` | 37 | 1 | PASS | Existing result surface |
| 17 | `shadow` | 36 | 0 | PASS | Create-session validation dialog |
| 18 | `learning` | 40 | 0 | PASS | Governance/approval queue |
| 19 | `wallet` | 57 | 1 | PASS | Create/deposit/withdraw/reconcile |
| 20 | `transactions` | 37 | 1 | PASS | Wallet filter |
| 21 | `ledger` | 37 | 1 | PASS | Wallet filter |
| 22 | `notifications` | 60 | 0 | PASS | Dismiss/restore |
| 23 | `activity` | 37 | 0 | PASS | Activity/audit tabs, search |
| 24 | `health` | 37 | 1 | PASS | Lifecycle, dependencies, capability table |
| 25 | `settings` | 50 | 0 | PASS | Connection, boundaries, alert form |
| 26 | `autonomy` | 50 | 0 | PASS (read-only) | Operator controls locked without token |
| **Total** | **26 routes** | **1,140** | **10** | **26/26 load PASS** | **85 operated; 3 blocked** |

---

## 3. Interaction coverage by control family

| Family | Locations | Expected behaviour | Current result | Evidence |
| --- | --- | --- | --- | --- |
| Sidebar navigation | All routes | Route changes without reload/error; active state follows | PASS — 26 routes reached | route sweep |
| Breadcrumb / back-forward | All routes | Correct history and page context | PASS — direct URL, back/forward exercised | route sweep; browser navigation |
| Primary buttons | Dashboard, Market, Watchlists, Portfolio, Risk, Paper, Backtest, Shadow, Wallet, Autonomy | Open modal or submit intended action | PASS or correctly disabled/blocked | targeted workflows |
| Secondary/ghost buttons | Refresh, clear, cancel, filter reset | Refetch or reset without stale state | PASS — refresh and clear observed | route/workflow sweeps |
| Search inputs | Market, Watchlists, Events, Activity, Settings | Filter/case-insensitive input; clear restores | PASS — market search and clear | market trace |
| Selects | Market, Events, Transactions, Ledger, Analytics, Settings | Change result set, not just UI copy | PASS — market exchange, wallet filters | current browser pass |
| Checkboxes/radios | Settings, filters | Persist/toggle only intended state | PASS — toggle observed | input sweep |
| Text/number inputs | Settings, modals, Autonomy config | Accept valid input; reject empty/invalid | PASS (modal validation) / Autonomy blocked | modal sweep |
| Tabs | Activity/Audit, other tabbed cards | Switch panel without stale content | PASS | route sweep; a11y |
| Table sorting | Forecasts, Watchlists, Transactions, Ledger, Health | Reorder rows and expose `aria-sort` | PASS — current sort changes row order | market/forecast probes |
| Table row actions | Records, watchlists, portfolios, paper, autonomy | Open detail or perform row action | PASS where exposed; destructive actions gated | prior matrix; current |
| Pagination | Market, records | Next/previous changes record set without omissions | **PARTIAL** — current market next check not re-proven | prior stateful retest |
| Modal open/close | Watchlists, Risk, Paper, Backtest, Shadow, Wallet, Autonomy | Focus trapped; Escape closes; focus restored | PASS — `modal_pass=true` | a11y 26 |
| Confirmation dialogs | Stop/disable/pause/delete | Require explicit confirmation | PASS — buttons open confirmation; no unintended action | workflow sweep |
| Toasts/status copy | Mutations | Success only after backend success; 429 distinct from offline | PASS — 429 fix verified | `workflow_sweep.json` |
| Chart hover | Dashboard/Market/Analytics | Tooltip shows the plotted point | PASS render; full value cross-check not repeated | a11y/route sweep |
| Keyboard navigation | All routes, modal | Skip link, Tab order, Enter activation, Escape | PASS — focus visible, modal trap | a11y 26 |
| Empty states | Events, portfolios, paper, shadow, learning | Honest “no data”, not fake rows | PASS — observed | route sweep |
| Error states | All API pages | Show error with retry; do not show success | PASS — 429/copy path; API errors | route sweep; 429 test |

---

## 4. Explicitly operated controls

### 4.1 Current 85-pass layer

Four route groups were operated after the route-count fix:

| Group | Result | Notes |
| --- | ---: | --- |
| A | 16 PASS | Route loads, refresh, navigation and visible actions |
| B | 17 PASS | Inputs/selects and state changes |
| C | 28 PASS | Modal, form, table, row actions |
| D | 24 PASS | Records, wallet/ledger, filters and closure checks |
| **Total** | **85 PASS, 3 BLOCKED** | `.ui_audit/qa_controls_A-D.json` |

The three blocked controls are on `autonomy`:
- `Forget token` — disabled because no operator token was entered;
- `Start monitoring` — disabled because operator authorization is not configured/presented;
- `Run monitoring cycle` — disabled for the same gate.

These are correct fail-closed states, not missing handlers. The server would also reject a mutation without the dedicated token.

### 4.2 Target workflow layer

| Workflow | Result | Evidence |
| --- | --- | --- |
| Dashboard render/market pulse | PASS — 5 priced rows | `workflow_sweep.json` |
| Market search/filter/sort | PASS after settlement; pagination partial | current trace |
| Portfolio pause/resume/restore | PASS | targeted workflow |
| Paper submit without proposal | PASS — control disabled, no order | direct probe |
| Backtesting result surface | PASS | workflow sweep |
| Shadow create dialog | PASS — validation, no session | workflow sweep |
| Autonomy lock without operator | PASS — no enabled mutating control | workflow sweep |
| 429 copy | PASS — rate-limited, not offline | interception test |
| Responsive overflow | PASS — 12/12 checks | workflow sweep |

### 4.3 Write flows

**14 automated PASS**, **1 harness-BLOCKED**; the blocked portfolio toggle was manually rechecked and passed (`Resume` → pause → `Resume`).

PASS: watchlist create/add/remove, wallet create/deposit/withdraw/reconcile, risk stress, notification dismiss/restore, three portfolio new-book enablement checks, transactions wallet filter, ledger wallet filter.

---

## 5. Preserved 470-entry historical matrix

The 10 November 2026 matrix is preserved separately:

- `docs/NM14_UI_CONTROL_MATRIX.json` — 470 entries, 25 routes, all PASS at that time.
- `docs/NM14_UI_CONTROL_MATRIX.md` — human-readable version.
- `docs/archive/qa_2026-11-09/COMPLETE_UI_INTERACTION_MATRIX.md` — prior report.

It is historical evidence, not a claim of a fresh 470/470 run. The current route set has one additional route (`autonomy`). A future full acceptance run should reconcile the historical 470 entries plus the current Autonomy controls and record a fresh total.

---

## 6. Coverage classification

| Classification | Count / scope | Result |
| --- | --- | --- |
| Routes opened | 26/26 | PASS |
| Accessibility-scanned controls | 1,140 | PASS — 0 issue classes |
| Current explicit operations | 85 | PASS |
| Operator-token-gated controls | 3 | BLOCKED — correct fail-closed |
| Preserved historical controls | 470 | PASS on 10 Nov, not re-operated |
| Market pagination | 1 workflow | PARTIAL |
| Paper order/fill/exit | 0 records | BLOCKED by state |
| Chart value-by-value cross-check | all charts | PARTIAL |
| Every table row vs. API | 10 tables | PARTIAL |

---

## 7. Matrix verdict

**26/26 routes PASS for load and observable surface.**  
**1,140/1,140 accessibility-scanned controls PASS for labels, names, headings, tables, contrast and focus.**  
**85 explicit current controls PASS; 3 correctly BLOCKED by operator-token absence.**  
**The complete current control replay is PARTIAL; the historical 470/470 is preserved, not re-claimed.**

This matrix supports a **PARTIAL** overall QA verdict. It does not support a claim that every current control has been re-operated in this pass.
