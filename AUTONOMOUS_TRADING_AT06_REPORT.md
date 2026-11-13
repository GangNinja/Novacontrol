# AT-06 — Recovery, Observability and Final Acceptance

**Milestone:** AT-06  
**Project:** NovaMarket  
**Date:** 2026-11-11  
**Overall verdict:** **PASS**

AT-06 closes the autonomous paper-trading programme with an explicit 22-scenario lifecycle suite, restart/recovery acceptance, read-only operational reliability reporting, a safe recovery operation, paper-only and authorization evidence, reconciliation checks, performance measurements and final documentation. No broker, live order, real-money path, target-weight rebalancer, forced end-of-day liquidation or automatic learning promotion was added.

---

## 1. Prerequisite gate

| Prerequisite | Report | Verdict |
|---|---|---|
| AT-01 runtime and safety shell | `docs/AT-01_IMPLEMENTATION_REPORT.md` | PASS |
| AT-02 session-aware monitoring | `docs/AUTONOMOUS_TRADING_AT02_REPORT.md` | PASS |
| AT-03 decision and NM-08 risk pipeline | `AUTONOMOUS_TRADING_AT03_REPORT.md` | PASS |
| AT-04 paper execution and reconciliation | `AUTONOMOUS_TRADING_AT04_REPORT.md` | PASS |
| AT-05 exit supervision and learning evidence | `AUTONOMOUS_TRADING_AT05_REPORT.md` | PASS |
| AT-05 prerequisite closure | `docs/AT05_PREREQUISITE_CLOSURE.md` | CLOSED |

All five prerequisite reports show PASS. The AT-06 suite independently exercises the accepted engines rather than replacing them.

---

## 2. AT-06 implementation summary

AT-06 is an integration and reliability milestone. It adds only the missing operational and recovery surface.

### Added

- `src/novacontrol/novamarket/autonomy_observability.py`
  - read-only aggregation of the existing AT-01..AT-05 status documents;
  - stable failure-domain classification;
  - pipeline, financial, reconciliation, recovery and safety counts;
  - no engine, order path, financial mutation or second store.
- `GET /market/autonomy/reliability`
  - authenticated operational view;
  - reports runtime/scheduler health, pipeline counts, pending reconciliation, incomplete learning evidence, recovery state and safety authorities.
- `POST /market/autonomy/recover`
  - operator-token protected;
  - calls the existing runtime recovery, decision-run recovery, NM-10 recovery and exit reconciler;
  - always returns the runtime to `disabled`; never auto-resumes.
- `AutonomyExitSupervisor.recover()`
  - reuses `ExecutionEngine.recover_pending()` and `reconcile_decision()`;
  - reconciles durable partial/pending exits without submitting an order or replaying one.
- `ExecutionStore.autonomy_counts()` now reports authoritative order/fill totals alongside intent/event totals.
- The Autonomy safety card no longer presents the AT-01 shell's `release_gate` as the complete paper pipeline's health; it reads the AT-06 reliability view.
- API surface, route-consumer registry and `docs/API.md` updated.

### Deliberately not changed

- NM-08 remains the sole risk authority.
- NM-10 remains the sole paper-execution authority.
- Portfolio, wallet and ledger remain authoritative.
- Runtime modes, lease, one-cycle rule, 20-instrument cap, 15-minute data floor and verified-calendar fail-closed behaviour remain unchanged.
- Target and trailing supervision remain long-only.
- No AT-07 work was started.

---

## 3. Lifecycle evidence

The accepted lifecycle is:

```text
Runtime → Session Monitoring → Discovery → Strategy Evaluation → Signal
→ Proposal → NM-08 Risk Assessment → Execution Intent → NM-10 Paper Order
→ Fill → Ledger → Position → Exit Supervision → Exit Proposal
→ NM-08 Risk Assessment → NM-10 Exit Order → Exit Fill
→ Position Reconciliation → Learning Evidence
```

The AT-06 scenarios verify stable proposal/risk/execution/order/fill/exit/evidence identifiers and authoritative persistence at each financial transition:

- entry execution intent is durable before forwarding;
- every entry order is derived from a persisted risk-approved proposal;
- every exit decision carries the NM-08 assessment and NM-10 order;
- fills reference the authoritative NM-10 order and ledger transaction ids;
- positions and cash come from the portfolio engine after NM-10 settlement;
- learning evidence points back to the entry and exit records;
- duplicate triggers and repeated cycles reuse the existing records.

No UI value is used as acceptance evidence.

---

## 4. Recovery and observability

### Recovery operation

`POST /market/autonomy/recover`:

1. recovers the runtime lease and interrupts abandoned cycles;
2. marks abandoned decision runs failed through the existing decision store;
3. invokes the existing NM-10 `recover_pending()`;
4. reconciles durable execution intents and pending exit decisions;
5. reports unresolved records rather than declaring success;
6. leaves the runtime in `disabled` with no lease.

Recovery never retries an uncertain financial operation blindly. An execution or exit with no authoritative order is reported as unresolved or failed; no second order is synthesized.

### Reliability report

The report exposes, from authoritative records only:

- runtime mode/state, lease, active cycle, recovery required, scheduler health;
- discovered instruments, evaluations, signals, proposals, risk assessments;
- execution intents, orders, fills, exit decisions/orders/fills, learning evidence;
- pending orders, filled/partial counts, open exit decisions, reconciliation pending;
- failure domains (runtime, scheduler, lease, calendar, session, market data, discovery, monitoring, strategy, risk, proposal, execution, reconciliation, ledger, learning, authorization, database);
- recovery outcome and unresolved count;
- `risk_authority=NM-08`, `execution_authority=NM-10`, `live_orders_possible=false`.

### Kill switch

`POST /market/autonomy/stop` is the established kill switch. It stops the runtime and releases the lease, so new entries, new exits and new submissions are refused. It does **not** cancel already submitted NM-10 orders, because the accepted system defines no cancellation path. Pending authoritative orders remain available for reconciliation after an explicit recovery; they are not replayed.

---

## 5. Twenty-scenario acceptance

`tests/test_novamarket_at06.py` contains 22 explicit lifecycle scenarios over the real wallet, portfolio, risk, strategy, decision, execution, ledger, exit and learning stores. Only market/forecast providers are deterministic fixtures.

| ID | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Clean application startup | PASS | Safe `disabled/stopped` runtime, no lease |
| 2 | Application restart | PASS | Persisted monitoring start reopens disabled; no auto-resume |
| 3 | Monitoring mode | PASS | Risk-approved proposal exists; zero orders before entry |
| 4 | Autonomous mode | PASS | Autonomous runtime + persisted `rdec_…` NM-08 provenance |
| 5 | Approved entry | PASS | One NM-10 buy order, one fill, INR market reference |
| 6 | Rejected entry | PASS | `RISK_APPROVAL_INVALID`; zero orders |
| 7 | Duplicate proposal | PASS | Same order id; one order, one fill |
| 8 | Submission timeout | PASS | Unknown intent first, no order, reconciled retry, then one order |
| 9 | Partial entry fill | PASS | `partially_filled`, positive remaining quantity, ledger ids |
| 10 | Full entry fill | PASS | Position quantity equals fill; ledger reconciliation passes |
| 11 | Stop-loss trigger | PASS | One `stop_loss` exit decision, filled and reconciled |
| 12 | Target trigger | PASS | One `target_price` exit decision, filled |
| 13 | Paused mode | PASS | Decision pipeline refuses; no buy orders |
| 14 | Paused protective exit | PASS | Paused mode submits one protective exit |
| 15 | Partial exit fill | PASS | Positive remaining position; duplicate suppressed |
| 16 | Full exit fill | PASS | Position closed; reconciliation passed |
| 17 | Duplicate exit trigger | PASS | One decision and one sell order |
| 18 | Crash during execution | PASS | Supervisor recovery reconciles partial without a second order |
| 19 | Learning attribution | PASS | Entry and exit linked; `auto_promoted=false`; strategy unchanged |
| 20 | Kill switch | PASS | Runtime disabled; no exit submitted; entry refused |
| 21 | Trailing-stop trigger (AT-05 addendum) | PASS | One `trailing_stop` exit after high-water fall |
| 22 | Trailing high-water restart (AT-05 addendum) | PASS | Persisted high-water survives restart |

**AT-06 suite result: 25 passed** (22 lifecycle scenarios, two reliability-report tests, one sustained-cycle test).

---

## 6. Requirement matrix

| Requirement | Implementation evidence | Verification evidence | Verdict |
|---|---|---|---|
| AT-01..AT-05 PASS prerequisite | Prior reports and closures | All six reports present | PASS |
| End-to-end lifecycle | Existing AT-01..AT-05 composition | 22 scenarios | PASS |
| Clean startup safe default | `AutonomyRuntime` | Scenario 1 | PASS |
| Restart without auto-resume | durable store recovery | Scenario 2 | PASS |
| Scheduler/lease reliability | AT-01/AT-02 runtime | Runtime regressions + scenarios 1–2 | PASS |
| Interrupted-cycle recovery | runtime/store/decision/execution recovery | Scenario 18; `recover()` | PASS |
| Runtime modes | AT-01/AT-02 gates | Scenarios 3, 4, 13, 14, 20 | PASS |
| Kill switch | `stop` | Scenario 20 | PASS |
| NM-08 sole risk authority | AT-03/AT-05 | Scenarios 4, 6, 12, 14 | PASS |
| NM-10 sole execution authority | AT-04/AT-05 | Scenarios 5, 8, 11, 12 | PASS |
| Entry order provenance | durable proposal/intent/order | Scenarios 5, 7 | PASS |
| Exit order provenance | exit decision/risk/order | Scenarios 11, 12, 15, 16 | PASS |
| No duplicate orders/fills | durable idempotency | Scenarios 7, 8, 17, 18 | PASS |
| Partial fills | NM-10 authoritative quantities | Scenarios 9, 15 | PASS |
| Full fills | NM-10/portfolio/ledger | Scenarios 10, 16 | PASS |
| Cash/position/ledger reconciliation | existing accounting | Scenarios 10, 16 | PASS |
| NC/INR consistency | market reference currency | Scenarios 5, 10 | PASS |
| Learning links entry to exit | exit evidence builder | Scenario 19 | PASS |
| Learning advisory only | no promotion path | Scenario 19; contract | PASS |
| Duplicate learning/attribution | unique decision evidence | Scenario 19 | PASS |
| Operational observability | `autonomy_observability.py`, reliability route | `ReliabilityReportTests`; API surface | PASS |
| Failure classification | stable domain map | `ReliabilityReportTests` | PASS |
| Recovery observability | recovery result and unresolved list | Scenario 18; reliability report | PASS |
| Paper-only safety | constants/contracts | mypy; safety tests; route contract | PASS |
| No live/broker path | no broker imports/endpoints | AT-04/AT-05 safety tests | PASS |
| Security/operator separation | existing auth dependencies | 403/401 API tests | PASS |
| API route consistency | surface + consumers + generated docs | 223 market routes; docs in sync | PASS |
| UI consistency | Autonomy reliability line wired | TypeScript/Vite build | PASS* |
| Performance | startup/shutdown/cycle/recovery measurements | Section 7 | PASS |
| Sustained load | 60 duplicate cycles | Section 7 | PASS |
| Type checks | mypy both views | 573 files clean | PASS |
| Regression | focused AT-02..AT-06/API/NM groups | Section 8 | PASS |
| No commit/stage/push/tag | no Git mutation | `git diff --check` clean | PASS |

\* The AT-06 UI change was type-checked and built; a fresh authenticated browser interaction pass was not repeated in this AT-06 run. The prior AT-05 live Autonomy UI verification remains the browser evidence. This is a documented verification limitation, not a backend functionality gap.

---

## 7. Performance and sustained-load measurements

Conditions: Windows host, local SQLite stores, deterministic paper-market fixture, single process, real AT-01..AT-05 engines. These are application measurements, not a production capacity claim.

| Measurement | Result |
|---|---|
| Runtime startup | 1.95 ms |
| Runtime shutdown | 1.01 ms |
| Sustained exit-supervision mean | 27.68 ms/cycle |
| Sustained cycles | 60 |
| Total sustained elapsed | 1.6608 s |
| Crash recovery operation | 10.91 ms |
| Recovery result | 1 recovered, 1 reconciled |
| Sell orders after 60 cycles | 1 |
| Exit decisions after 60 cycles | 1 |
| Asyncio task count before/after | 1 / 1 |
| Traced memory current | 986,279 bytes |
| Traced memory peak | 1,071,385 bytes |

No duplicate exit order, decision growth, task growth or unbounded queue was observed. The sustained test is a deterministic duplicate-suppression load, not a 24-hour soak or live-provider benchmark.

---

## 8. Verification commands and exact results

```text
PYTHONPATH=src python -m pytest tests/test_novamarket_at06.py -q
25 passed, 1 warning
```

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_at06.py tests/test_novamarket_at05.py \
  tests/test_novamarket_at04.py tests/test_novamarket_autonomy.py -q
96 passed
```

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_at02.py tests/test_novamarket_at03.py \
  tests/test_novamarket_api.py::NovaMarketRouteContractTests -q
87 passed, 223 subtests passed
```

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_api.py tests/test_novamarket_workbench_ui.py -q
64 passed, 231 subtests passed
```

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_risk.py tests/test_novamarket_execution.py \
  tests/test_novamarket_wallet.py tests/test_novamarket_learning.py -q
182 passed, 22 subtests passed
```

```text
PYTHONPATH=src python -m mypy src
Success: no issues found in 573 source files

PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 573 source files
```

```text
python scripts/generate_api_reference.py --check
docs/API.md: in sync
```

```text
cd web && npm run build
✓ 1599 modules transformed
✓ built in 4.26s
```

`git diff --check` is clean.

---

## 9. Known limitations and deferred items

- Target/trailing stops support long positions only; short-position target/trailing behaviour is unsupported.
- Deterministic fixtures prove application and financial pathways, not live market-data provider reliability.
- Paper fills are simulated by NM-10 with a simulated clearing counterparty; no exchange-grade or tick-level execution is claimed.
- The kill switch does not cancel already submitted NM-10 orders; they remain authoritative and are reconciled by the recovery operation.
- Forced end-of-day liquidation remains excluded and unimplemented.
- Target-weight rebalancing remains excluded.
- Automatic learning promotion remains excluded.
- A fresh authenticated browser pass was not run for the one AT-06 UI line; the build and prior AT-05 live UI evidence are retained.
- AT-01's runtime-shell status fields remain shell-specific; complete pipeline state is reported by AT-04, AT-05 and the AT-06 reliability endpoint.
- No 24-hour production soak or live-provider failover was performed.

---

## 10. Final verdict

**AT-06: PASS.**  
**Complete AT-01 through AT-06 programme: PASS** subject to the documented limitations above.

No commit, staging, push or tag was performed.
