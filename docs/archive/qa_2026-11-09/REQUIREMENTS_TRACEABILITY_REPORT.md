# NovaMarket Requirements Traceability Report

**Audit date:** 2026-11-09  
**Requirements basis:** accepted NM-01–NM-14 scope (`docs/NM14_ACCEPTANCE.md`, `docs/NM14_FINAL_ACCEPTANCE_REPORT.md`)  
**Verdict:** **PASS for requirement implementation at the declared phase scope.** The QA completion verdict remains PARTIAL because verification breadth and performance do not meet the audit's PASS bar.

---

## 1. Purpose and method

This report maps each accepted NovaMarket requirement to its implementation, its automated coverage, and the real workflow evidence produced during this audit.

The trace uses three levels:

- **Implemented:** the requirement's production path exists in the codebase.
- **Verified:** the path was exercised through tests, the isolated HTTP API, Chromium, or the deterministic end-to-end chain.
- **Scope:** the accepted limitation that belongs to the requirement (for example, paper-only execution, or no background trading loop).

A requirement is marked PASS only where the accepted implementation exists and at least one non-mocked integration path or deterministic chain demonstrates the behavior. UI-only breadth limits are recorded in the completion report rather than being misreported as requirement failures.

---

## 2. Requirement summary

| ID | Requirement | Implementation | Verification | Status |
| --- | --- | --- | --- | --- |
| NM-01 | Architecture, lifecycle, permissions, capabilities, events, storage boundaries | `NovaMarketService`, module, event publisher, permission/capability registration, SQLite stores | Lifecycle `running`; route auth; status/contracts; shared event and audit observation | PASS |
| NM-02 | Market data, providers, freshness, quality, append-only history | Market-data repository, provider registry, observation and OHLCV stores | 4,811 matched instruments; 101 quoted; route filters; chain seeded 300 bars; stale-quote safety observed | PASS |
| NM-03 | Wallet and double-entry ledger | Wallet service, append-only ledger, reconciliation and reversal paths | 18 wallets; 40 entries; 0 bad running balances; sum `0.00`; live deposit/withdraw | PASS |
| NM-04 | Event radar: sources, extraction, verification, classification | Radar engines and event store | Events route renders recorded events; chain/learning consume evidence; no fabricated event | PASS |
| NM-05 | Discovery and watchlists, candidate scoring, scheduler | Discovery service, candidate store, watchlist lifecycle | Discovery and watchlist routes; watchlist controls; accepted discovery tests; scheduler status observable | PASS |
| NM-06 | Forecasting: estimators, calibration, model lifecycle | Forecast service and estimator/model stores | Forecast route; chain produced a `few_sessions` forecast with measured uncertainty | PASS |
| NM-07 | Three-horizon portfolios, allocation, valuation, accounting | Portfolio service with horizon policies and valuation store | Short/medium/long routes; 7 books; pause/resume; `Mixed`/`Active` horizon state; valuations | PASS |
| NM-08 | Independent risk engine with veto authority | Risk policy, decision, alert and stress services | Risk route; stale proposals correctly rejected; chain proposal approved by NM-08 | PASS |
| NM-09 | Strategy catalog, signals, proposals and lifecycle | Strategy service and proposal store | Strategies route; chain produced a real buy signal and proposal | PASS |
| NM-10 | Paper execution: orders, fills, fees, slippage, reconciliation | Execution engine, order/fill stores, wallet/portfolio gateways | Paper route; chain order `filled`; 0 live orders; no broker path | PASS |
| NM-11 | Point-in-time backtesting with fees, risk, reproducibility | Backtest engine and isolated store | Backtesting route; chain replay `completed`; no paper/wallet mutation | PASS |
| NM-12 | Non-executing shadow mode | Shadow service, cycles, projections, outcomes, guards | Shadow route; chain session `completed`; execution disconnected | PASS |
| NM-13 | Evidence-driven learning, experiments, approval, rollback | Learning service with isolated store and promotion workflow | Learning route; chain evidence `accepted`, experiment `completed`, candidate `promoted` | PASS |
| NM-14 | Integrated workspace, overview, activity/audit, UI | FastAPI market routes, overview aggregation, standalone workbench, event/audit observers | 25/25 routes; 480-control baseline; 144 post-fix controls; 9/9 security; 957-control accessibility probe; chain | PASS |

**Result: 14/14 accepted requirements implemented and verified at their declared scope.**

---

## 3. Requirement-by-requirement evidence

### NM-01 — Architecture and boundaries

The integrated application exposes the market service through the existing application, event bus, permission manager and capability registry. The service remains a separate domain; NM-14 did not add a second risk engine, portfolio manager, execution engine, scheduler or database.

Verified by:

- lifecycle `running`;
- authenticated route behavior and the security probe;
- `/market/status`, `/market/overview` and `/market/contracts`;
- event/activity/audit observation;
- chain `lifecycle: "running"`.

### NM-02 — Market data

The market-data repository is append-only and provider-backed. During the audit the isolated copy had 4,811 matched instruments, 101 quoted with stored observations and 2,406 catalogue instruments. The latest observations were 28.9–55.9 hours old; the UI did not invent a replacement price.

Verified by:

- Market route and filters;
- large-cap filter: 50/100 quoted;
- Reset: 101/2,406 quoted, 4,811 matched;
- deterministic 300-bar fixture in the chain;
- risk policy rejecting stale proposals.

### NM-03 — Wallet and ledger

Every balance is represented by ledger entries. QA replay found 18 wallets and 40 entries with zero incorrect running balances, reconciliation `ok`, and a zero ledger sum.

A live deposit of `+100.00` and withdrawal of `-25.00` moved the operator balance from `30,075.00` to `30,150.00`. This is a real persisted QA write, not a rendered mock.

### NM-04 — Event radar

The Events route renders recorded events and the dashboard's recent-intelligence list. The radar's evidence informs discovery, risk and learning. No event was invented when the source data was absent.

### NM-05 — Discovery and watchlists

Watchlists render with members and lifecycle actions. Discovery is exposed on its route and in status. The discovery scheduler reports `running: false`; this is a visible state, not a hidden autonomous loop.

### NM-06 — Forecasting

The Forecasts route renders stored forecasts. The acceptance chain produced a real `few_sessions` forecast after seeding 300 deterministic weekday sessions. No forecast was fabricated for an unavailable instrument.

### NM-07 — Portfolios

Seven short-term books existed during the audit. Pause and resume changed the server state and the UI showed the resulting status. The dashboard originally showed only the first book's status; this was corrected so it aggregates all books (`Mixed` when paused and active coexist, `Active` when all are active).

Portfolio values, cash and exposure are separate from the NovaCoin wallet and are never added together.

### NM-08 — Risk

Risk is the authoritative gate. The audit observed stale-quote alerts at 28.9 and 55.9 hours; the policy permits 24 hours, and proposals were rejected rather than forced through. The deterministic chain exercised the same service and returned `approved` for a fresh fixture.

### NM-09 — Strategies

The Strategies route displays the catalogue and proposal state. The chain returned signal `buy` and a proposal that was assessed by NM-08. The stale live proposals were not executed.

### NM-10 — Paper execution

The Paper route displays paper orders and proposal submission controls. The chain created a paper order through the real execution service; the order reached `filled`. No broker adapter or real-money route is present. In the live QA database, the absence of eligible fresh proposals correctly resulted in 0 orders and 0 fills.

### NM-11 — Backtesting

Backtesting uses isolated point-in-time data and its own store. The chain returned `completed`; the accepted suite covers fees, slippage, walk-forward folds, risk and reproducibility. Backtesting did not mutate paper orders, wallets or portfolios.

### NM-12 — Shadow mode

Shadow sessions are non-executing. The chain returned `completed` with no execution gateway. The route and status report `execution_connected=false` and no real-money path.

### NM-13 — Adaptive learning

The chain accepted evidence, completed an experiment and promoted a candidate. The learning store is isolated and has no financial write path. Approval and rollback behavior remain covered by the accepted learning suite.

### NM-14 — Integrated workspace

The workspace is served by the existing FastAPI application with no-cache headers, content-derived asset versions and 25 routes. The audit re-confirmed:

- 25/25 routes with no error boundary, console error or 5xx;
- 480/480 accepted baseline control entries;
- 144 post-fix controls with 3 harness false positives independently cleared;
- accessibility: 25 routes, 957 controls, zero automated findings;
- security: 9/9 checks;
- live market, wallet, portfolio, risk and activity surfaces.

The workspace is intentionally read-only for financial mutations; writes are explicit forms/API operations. No form was fabricated where none exists.

---

## 4. Non-requirements and scope boundaries

### 4.1 Autonomous paper trading

**Not a requirement in NM-01–NM-14.** The accepted design provides explicit proposals and explicit execution. The runtime monitor is not a trading loop; the discovery scheduler is stopped; there is no background cycle that creates orders. See `AUTONOMOUS_PAPER_TRADING_AUDIT.md`.

This absence is not mapped as a failed requirement. It would become a new feature request requiring approval because it changes architecture and financial behavior.

### 4.2 Paper-only execution

The accepted safety boundary is intentional:

- no broker adapter;
- no live exchange route;
- no real-money bank rail;
- paper orders only;
- shadow and learning have no execution gateway.

This boundary is satisfied, not incomplete.

### 4.3 Read-only workspace mutation boundary

Observation and navigation are read-only. Financial changes remain explicit operations. This is a declared design boundary, not a missing form.

---

## 5. Acceptance criteria mapping

| Criterion | Result |
| --- | --- |
| Paper-only flags preserved | PASS |
| No real-money credentials or live route | PASS |
| All accepted phases represented in status/workspace | PASS |
| Market data, discovery, forecasting, portfolio, risk, strategy and execution integrated | PASS |
| Wallet and ledger reconcile | PASS |
| Backtests isolated from paper accounting | PASS |
| Shadow mode non-executing | PASS |
| Learning approval/rollback present | PASS |
| Chain reproducible and deterministic | PASS (exit 0) |
| All 25 routes render | PASS |
| Every accepted control exercised at least once | PASS (accepted 480 baseline) |
| Every control re-exercised after final edits | **NOT COMPLETE** — 144 post-fix controls |
| Performance PASS | **PARTIAL** — 2 of 10 API checks fail |
| Autonomous cycle audited dynamically | **NOT APPLICABLE** — no cycle exists |

---

## 6. Gaps that affect confidence, not requirement status

| Gap | Effect | Disposition |
| --- | --- | --- |
| 144 post-fix controls rather than all 480 | Completion verdict cannot be PASS | Recorded as audit limitation; 23 unchanged routes retain accepted baseline |
| Concurrent p95 `6,271.96 ms` | Capacity under concurrent load is unproven | Open performance limitation; no architecture change made |
| Soak p95 `1,566.34 ms` | Slightly over internal 1,500 ms limit | Open performance limitation |
| Live quotes 28.9–55.9h stale | No fresh live paper fill through UI | Correct risk rejection; deterministic chain used for execution proof |
| Long-horizon modal opener absent in harness | One modal sweep BLOCKED | Not proven a product defect; accepted write flow covers the route |
| Secret field not submitted | One input check PARTIAL | Deliberate safety decision; no credential entered |

---

## 7. Conclusion

**Accepted requirements: 14/14 PASS at the declared implementation scope.**

No missing NM-01–NM-14 requirement was found. No accounting, risk-gate, ledger, broker-boundary or live-trading defect was found.

The application is not marked fully QA-complete because verification breadth after the final UI edits and two performance thresholds are incomplete. Those are audit-confidence findings, not silent requirement failures.
