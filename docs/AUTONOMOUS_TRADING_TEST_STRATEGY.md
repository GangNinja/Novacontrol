# Autonomous Paper Trading — Test Strategy

**Status:** Validation plan, reconciled 10 November 2026 with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md`. The AT-01 shell tests are implemented in `tests/test_novamarket_autonomy.py`; every test below for AT-02–AT-06 is still a plan, and those milestones are not approved or started.

---

## 1. Validation principle

A passing autonomy test must prove the **lifecycle**, not just that a button returns HTTP 200. The critical proof is:

> Given a fresh or restarted NovaMarket with seeded paper data, autonomous mode can scan, decide, risk-check, submit, fill, update the book, monitor, exit and record the result without a human invoking each workflow — and every failure path fails closed without a duplicate order.

Tests must not use a live market provider, broker, production database or real money. The default harness uses a temporary SQLite directory and deterministic fixtures.

---

## 2. Test levels

| Level | What it proves | Location / pattern |
|---|---|---|
| Unit | State transitions, pure schedulers, config validation, exit predicates, lease rules | `tests/test_novamarket_autonomy_*.py` |
| Integration | Real `NovaMarketService` + real strategy/risk/execution/portfolio stores, temporary DB | `tests/test_novamarket_autonomy_integration.py` |
| API | Auth, same-origin, request validation, status codes and response links | FastAPI `TestClient` |
| E2E lifecycle | 20 required scenarios, three horizons, restart and kill | Scripted service/browser harness |
| Reliability | Crash, lease, lock, breaker, provider/risk/execution outage | Fault injection |
| Safety | Paper-only invariants, no broker/live path, no direct financial mutation | Static + runtime assertions |
| Performance/soak | Bounded scans, memory/latency, no duplicate growth | 90-second and longer paper session |
| UI | Controls, reload persistence, no stale DOM, trace visibility | Existing Vite build + browser harness |

The current repository already uses Python `unittest`/`pytest`-style tests with temporary stores. Reuse that style; do not add a second test framework.

---

## 3. Test environment

### 3.1 Deterministic fixtures

- `FakeClock` with an Asia/Kolkata-aware UTC clock.
- Seeded NSE/BSE instruments with 200–300 daily bars and a current quote.
- A known 2026 trading day, weekend, NSE holiday and early-close day.
- A 2027 date outside the calendar's verified holiday coverage.
- A stale quote and a missing-bar instrument.
- An active short-term, medium-term and long-term portfolio with simulated capital.
- One strategy per horizon with enough history to produce an actionable signal.
- A second strategy that conflicts on the same instrument.
- A risk policy that can approve, reject, restrict and require review.
- A paper execution config with deterministic fill and partial-fill rules.

### 3.2 Isolation

- Every test uses `tempfile.TemporaryDirectory()` for the market, wallet, portfolio, strategy, risk, execution and autonomy databases.
- No test writes to `data/novamarket.sqlite3`, `.ui_audit/nse_import.sqlite3` or another QA artifact.
- No live HTTP provider, broker or exchange.
- The service is constructed with an explicit temp `data_dir` and a `JsonStateStore` (or the existing test store).
- The test event bus is local; event assertions read the recorded events.
- No test starts the real discovery scheduler or the real API server unless it is an API/UI test with a local port.

### 3.3 Test names

Planned modules (AT-02+, not implemented):

```text
tests/test_novamarket_autonomy_runtime.py
tests/test_novamarket_autonomy_scheduler.py
tests/test_novamarket_autonomy_store.py
tests/test_novamarket_autonomy_decisions.py
tests/test_novamarket_autonomy_execution.py
tests/test_novamarket_autonomy_exits.py
tests/test_novamarket_autonomy_recovery.py
tests/test_novamarket_autonomy_api.py
tests/test_novamarket_autonomy_e2e.py
```

Existing tests remain untouched and must continue to pass:

```text
tests/test_novamarket.py
tests/test_novamarket_api.py
tests/test_novamarket_strategies.py
tests/test_novamarket_risk.py
tests/test_novamarket_execution.py
tests/test_novamarket_portfolio.py
tests/test_novamarket_learning.py
tests/test_novamarket_workbench_ui.py
```

---

## 4. Unit tests

### 4.1 Runtime and modes

- every legal mode transition; every illegal transition refused;
- stop/kill idempotency;
- pause does not cancel the runtime;
- stop sets disabled;
- application shutdown preserves the desired mode but sets `stopped`;
- `start(autonomous)` without paper acknowledgement is refused;
- `start(autonomous)` when execution flags are not paper-only is refused;
- status reports `mode` and `runtime_state` independently;
- a state read cannot claim `running` without a recent lease/tick.

### 4.2 Scheduler and calendar

- open session, pre-open, post-close, weekend, holiday and early close;
- 2027 coverage unknown => `coverage_unverified`, no order;
- 15-minute floor from `plan_market_refresh`;
- short/medium/long cadence;
- due scope with no portfolio allowlist => no cycle;
- force scan does not bypass session/coverage/data/risk;
- deterministic `cycle_key` and duplicate-key rejection.

### 4.3 Configuration and store

- required non-empty portfolio allowlist;
- valid decimal fractions and bounded intervals;
- unknown fields and credential-shaped fields rejected;
- version conflict;
- lease acquire/renew/release/expiry;
- schema version mismatch fails closed;
- interrupted cycle recorded exactly once;
- database write failure yields no order.

### 4.4 Decision mapping

- BUY with no holding => `open`; BUY with holding => `add`;
- SELL => `reduce`; EXIT => `close`;
- HOLD/NO_SIGNAL => `hold` with no proposal;
- risk `approved`, `rejected`, `restricted`, `requires_review`, `insufficient_data`, error;
- conflict `review` => no proposal;
- max-position and per-cycle cap;
- duplicate signal/proposal is replayed, not duplicated.

### 4.5 Exit predicates

- stop-loss below cost;
- target above cost;
- trailing below high-water mark;
- high-water mark only rises;
- time exit after the horizon holding period;
- strategy EXIT confirmation;
- portfolio drawdown exit only when explicitly enabled;
- missing/stale price => no exit;
- duplicate active exit => no second proposal;
- full close vs partial reduce quantity/notional;
- risk rejection remains visible and no order is made.

---

## 5. Integration tests

Each test uses the real service and real engines, with a temporary database.

### 5.1 Monitoring

1. start NovaMarket and autonomy in `monitoring`;
2. seed an instrument with a clear BUY signal;
3. run one due cycle;
4. assert a signal, proposal and NM-08 decision exist;
5. assert no execution order exists.

### 5.2 Risk authority

1. configure a risk policy that rejects the proposal;
2. run the cycle;
3. assert the rejection reason is stored;
4. assert no forward and no order.

Repeat for `restricted`, `requires_review` and `insufficient_data`.

### 5.3 Execution

1. approve the proposal through NM-08;
2. run an autonomous test cycle (internal release gate);
3. assert one order, one or more fills, one wallet ledger effect and one position;
4. submit the same proposal again;
5. assert one order and no extra fill;
6. assert the decision links to the same order/fill IDs.

### 5.4 Partial fill and open order

- limit/volume fixture produces a partial fill;
- the order remains `partially_filled`;
- the next cycle processes only the autonomy order;
- reservations and remaining quantity are consistent;
- a cancel releases the remainder without deleting fills.

### 5.5 Stale data

- seed an old quote/bar;
- run a cycle;
- assert `skipped` or failed-closed decision, no order, and a health reason;
- verify that a fresh replacement allows the next cycle.

### 5.6 Portfolio/accounting

- after fill, read `PortfolioEngine.detail()`;
- assert cash, position, average cost and realized/unrealized values match the existing engine;
- reconcile the portfolio and the execution ledger;
- assert no autonomy code wrote a wallet/ledger row directly (spy or source review).

### 5.7 Learning evidence

- complete a trade;
- assert a `kind="paper"` evidence row with order/fill provenance;
- repeat the same terminal result;
- assert duplicate handling, not a second incompatible evidence record;
- assert no candidate is auto-approved or promoted.

---

## 6. End-to-end lifecycle scenarios

These are the 20 required scenarios. Each has a fixture, an action, expected observable results and at least one negative assertion.

| ID | Scenario | Expected result |
|---|---|---|
| E2E-01 | Start autonomous monitoring | mode `monitoring`, runtime `running`, no order |
| E2E-02 | Scan eligible instruments automatically | one durable cycle per scope; candidates evaluated; no manual per-instrument call |
| E2E-03 | Generate a valid opportunity | signal with measured data quality and a reference price |
| E2E-04 | Evaluate the opportunity | proposal/risk decision exists; HOLD/no-signal does not create an order |
| E2E-05 | Approve or reject through existing risk | approved/rejected/restricted/pending status and reason; no risk bypass |
| E2E-06 | Submit an approved paper order automatically | exactly one NM-10 order; actor is autonomy |
| E2E-07 | Record order and fill | order/fill IDs and timestamps linked on the decision |
| E2E-08 | Update portfolio and ledger | position and ledger match the execution engine; portfolio reconciles |
| E2E-09 | Monitor the open position | position/exit state visible; no duplicate entry order |
| E2E-10 | Detect stop-loss or target | supervisor decision with trigger and price |
| E2E-11 | Execute the paper exit | exit proposal/risk/order; position reduced or closed |
| E2E-12 | Update realized P&L | portfolio/strategy performance reflects the exit; no local recomputation |
| E2E-13 | Continue monitoring other instruments | next cycle still completes while the earlier position is open/closed |
| E2E-14 | Pause and resume | paused blocks entries; resume restores the chosen mode; no duplicate |
| E2E-15 | Restart NovaMarket and recover state | interrupted cycle marked; pending fill reconciled; no automatic order before a clean cycle |
| E2E-16 | Handle stale market data | no order; degraded health; reason recorded; fresh data recovers |
| E2E-17 | Handle failed execution | failed/rejected order recorded; no blind retry; later cycle is safe |
| E2E-18 | Prevent duplicate orders | repeated click/request/restart yields one order |
| E2E-19 | Respect holidays and session boundaries | skipped session/holiday; no order; next session works |
| E2E-20 | Activate the emergency kill switch | mode disabled; lease revoked; no new orders; optional cancellation; audit trace |

### 6.1 Horizon variants

For E2E-03 through E2E-12, run three fixtures:

- **Day/short:** intraday bars, tight stop, no overnight assumption, time exit at the configured short holding period;
- **Swing/medium:** multi-day bars, wider stop/target, horizon-appropriate holding period;
- **Long-term:** daily bars, long horizon, lower turnover, time exit measured in the configured period.

The test must assert that a strategy not declared for the portfolio horizon is not evaluated (`StrategyEngine.evaluate` already rejects it), rather than silently applying a short-term strategy to a long-term portfolio.

### 6.2 Acceptance rule

The lifecycle is not accepted unless all 20 scenarios pass in one seeded run and the three horizon variants pass for the entry/exit subset. A single manual `forward` call is not evidence of autonomy; the test must record a cycle that produced the order without an operator invoking proposal/forward/execute.

---

## 7. Reliability and fault injection

| Fault | Injection | Expected |
|---|---|---|
| Process kill before submit | terminate a test subprocess at phase boundary | cycle `interrupted`; no order |
| Process kill after submit before fill | kill during processing | `recover_pending` handles pending fill; no duplicate |
| Lease conflict | second runtime instance with same DB | second cannot run; no duplicate cycle |
| Stale lease | expire lease; new process | recovery acquires; marks old cycle |
| DB locked | hold a write transaction | cycle fails closed; no order; retry bounded |
| Provider unavailable | adapter raises | no signal/order; degraded health |
| Risk unavailable | assess raises | no order; reason visible |
| Execution unavailable | submit raises | no order; breaker opens |
| Partial fill | volume cap | remainder preserved; no duplicate |
| Kill during in-flight submit | set kill before forward gate | no new submit; already-created order is reconciled |
| Restart with open order | reconstruct service | open order remains; autonomy processes only its actor |
| Calendar rollover | 2027 date | monitoring only; no order |
| Repeated cycle | same cycle key | one cycle, skip/replay |
| Invalid config | bad fractions/blank portfolios | 422; no mode change |

The fault harness must use the real `ExecutionEngine` and a temporary DB. Mocking the entire execution layer would not test the idempotency and compensation logic that matters.

---

## 8. Safety tests

### 8.1 Static and contract

- `ExecutionEngine.status()`/`contract()` assert `paper_only=True`, `live_trading=False`, `broker_connected=False`.
- No autonomy module imports a broker, exchange client, CCXT, broker SDK or `PortfolioEngine.apply`.
- No autonomy module calls `WalletEngine.transfer`, ledger correction or `PortfolioStore.upsert_position`.
- No autonomy API route accepts `live`, `broker`, `api_key` or a real-money field.
- `AutonomyConfig` rejects `paper_only=false` and unknown fields.
- The UI has no live toggle.
- A scan/forward assertion proves the only execution call is `ExecutionEngine.submit`/`process` through the strategy forwarder.

### 8.2 Authorization

- unauthenticated requests return 401;
- foreign-origin mutations are rejected by the existing middleware;
- a caller cannot set an arbitrary actor that changes execution behavior;
- a caller cannot cancel another actor's order through autonomy;
- config changes require auth and version concurrency;
- kill requires the exact confirmation string.

### 8.3 Financial invariants

- order count equals unique proposal count for autonomy;
- fill quantity never exceeds order quantity;
- no negative cash/position from an autonomous path;
- ledger reconciliation passes after every fill/exit;
- portfolio valuation is unchanged for a no-fill cycle;
- risk rejection never creates a reservation;
- an expired proposal cannot be forwarded.

---

## 9. Performance and long-running validation

### 9.1 Bounded-cycle tests

- 20-instrument batch stays within the configured cycle budget;
- 100 cycles do not grow autonomy tables unbounded (retention policy or explicit limit);
- one slow provider call does not block the entire event loop beyond `max_cycle_seconds`;
- one strategy exception does not prevent other instruments from being evaluated;
- scan cadence is not faster than the market-refresh floor.

### 9.2 Soak

- 90 seconds with seeded data, no external provider, all cycles complete or skip cleanly;
- longer paper-session soak (for example, a full simulated trading day accelerated by a fake clock) with zero duplicate orders;
- memory/RSS drift is measured, not assumed;
- event/row growth is recorded;
- no unbounded retry loop.

The performance numbers in `NM14_FINAL_ACCEPTANCE_REPORT.md` are baseline evidence for the existing app, not a claim about the new autonomy path. The autonomy path must publish its own measurements.

---

## 10. UI validation

- Start, pause, resume, stop and kill controls update the server and survive a page reload.
- The page distinguishes `mode` from `runtime_state` and shows the lease/last tick.
- Scans, decisions, risk results, orders, fills, positions and exits link to the existing authoritative pages.
- A monitoring session shows “no order submitted,” even when proposals are approved.
- A paused session shows the protective-exit policy and does not show an enabled “new entry” action.
- Error/degraded states show a reason and a next step; they are not an empty success state.
- No local price/P&L/order calculation; the displayed values match the server response.
- A browser reload does not show a stale mode or an old lease as current.
- The dashboard card and Paper banner do not replace the existing dashboard or Paper functionality.

Use the existing browser harness and a local Vite build. A screenshot alone is not a functional test; inspect the DOM/network and the server state.

---

## 11. Commands and gates

The planned gates, to be run after implementation, are:

```bash
cd novamarket
PYTHONPATH=src python -m pytest tests/test_novamarket_autonomy_*.py -q
PYTHONPATH=src python -m pytest \
  tests/test_novamarket.py tests/test_novamarket_api.py \
  tests/test_novamarket_strategies.py tests/test_novamarket_risk.py \
  tests/test_novamarket_execution.py tests/test_novamarket_portfolio.py \
  tests/test_novamarket_learning.py -q
PYTHONPATH=src python -m pytest tests/ -q
PYTHONPATH=src python -m mypy src
PYTHONPATH=src python -m mypy src --platform win32
PYTHONPATH=src python scripts/generate_api_reference.py --check
cd web && npm run build
```

The full suite can exceed a single tool window; split it into file groups if needed, preserving each command's exit status. The 9 November acceptance report records 792 passed, 2 skipped, 938 subtests for its focused suite; that is the baseline, not a substitute for the new autonomy tests.

---

## 12. Acceptance matrix summary

| Area | Required evidence |
|---|---|
| Safety | paper-only flags, no broker/live path, no direct book mutation |
| Autonomy | a cycle produces an order without manual forward/execute |
| Risk | approved/rejected/restricted/error paths all visible; no bypass |
| Idempotency | duplicate request/restart produces one order and one fill set |
| Portfolio | position/ledger/valuation reconcile after entry and exit |
| Exits | stop, target, trailing, time, strategy, optional portfolio-risk |
| Sessions | holiday/weekend/early close/coverage-unknown fail closed |
| Recovery | crash/lease/open-order recovery with no duplicate |
| Control | start/pause/resume/stop/kill and reload persistence |
| Observability | decision-to-order trace, last scan/execution, errors |
| UI | existing workspace extended; no local calculations; no stale claims |
| Performance | bounded cycle, no leak/duplicate growth, recorded measurements |

No autonomous claim is accepted from a manual chain or from an HTTP 200 alone. The proof is a durable cycle with linked signal → proposal → risk → order → fill → position → exit records.
