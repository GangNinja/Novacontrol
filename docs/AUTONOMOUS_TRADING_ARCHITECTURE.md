# NovaMarket — Autonomous Paper Trading Architecture

**Status:** **AT-01 IMPLEMENTED** (runtime and safety shell only). AT-02–AT-06 are not approved, not started and not implemented. Where this document conflicts with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md` (owner-approved 10 November 2026), the owner decisions win.

**Date:** 9 November 2026

**Scope:** Architecture and implementation planning only. No application source, schema, migration, runtime configuration, commit or push is changed by this document.

**Baseline:** NovaMarket NM-01–NM-14, including the 9 November 2026 acceptance report and the three preserved QA fixes (BUG-QA-001, BUG-QA-002, BUG-QA-003).

---

## 1. Executive position

NovaMarket already contains the financially important parts of a paper-trading system:

- market data ingestion, freshness and instrument metadata;
- discovery and candidate scoring;
- six strategy families, signals and risk-gated proposals;
- an independent NM-08 risk engine;
- a realistic NM-10 paper-execution engine with order states, partial fills, fees, slippage, reservations and recovery;
- portfolio, position, ledger and valuation accounting;
- backtesting, shadow trading and controlled learning.

It does **not** contain a continuously operating component that composes those services into an autonomous lifecycle. `AUTONOMOUS_PAPER_TRADING_AUDIT.md` reaches that verdict explicitly: the manual/API paper chain works, but there is no orchestrator that discovers, decides, risk-checks, executes, monitors, exits and records without a human invoking each step. The existing service runtime loop is a housekeeping loop: notifications, event refresh and held-instrument bar refresh. It does not scan for opportunities or place orders.

This architecture adds the missing **orchestration and lifecycle layer**. It does not add a second risk engine, execution engine, ledger, portfolio engine, discovery engine or learning engine. Every financial mutation continues through the accepted NM-03/NM-07/NM-08/NM-10 services.

The design is intentionally fail-closed:

> If the market session, market data, risk decision, execution state, portfolio state or runtime lease cannot be verified, no order is submitted.

Paper-only is not a configuration option in this design. There is no broker adapter, no live-order method, no live endpoint and no automatic switch.

---

## 2. Baseline evidence and filenames

The requested `NM14_FINAL_ACCEPTANCE_REPORT(4).md` is not present in this repository. The actual authoritative acceptance document is:

- `novamarket/docs/NM14_FINAL_ACCEPTANCE_REPORT.md` — 9 November 2026 addendum: PASS for internal acceptance scope; 25/25 routes; 480/480 recorded controls; 792 passed, 2 skipped, 938 subtests passed; paper-only boundary confirmed; no broker client or live-order path.

Supporting reports:

- `novamarket/docs/AUTONOMOUS_PAPER_TRADING_AUDIT.md` — verdict **NO**, no autonomous paper-trading orchestrator exists.
- `novamarket/docs/QA_COMPLETION_REPORT.md` — previous QA verdict **PARTIAL**; the 9 November acceptance addendum supersedes its counts.
- `novamarket/docs/FULL_APPLICATION_QA_REPORT.md`
- `novamarket/docs/REQUIREMENTS_TRACEABILITY_REPORT.md`
- `novamarket/docs/COMPLETE_UI_INTERACTION_MATRIX.md`
- `novamarket/docs/BUG_FIX_REGISTER.md`

`GIT_BASELINE_EVIDENCE.md` is also absent. The latest recorded commit at design time is `afa5ff64c7673553b333fd3cff07cf5dc7c832b7` (2026-10-29), while the working tree contains uncommitted QA and documentation changes. This document therefore treats the working tree plus `NM14_FINAL_ACCEPTANCE_REPORT.md` as the evidence baseline and records the missing Git evidence as a documentation gap, not as a functional gap.

The three QA fixes remain part of the baseline and are not touched by this design:

- BUG-QA-001: dashboard portfolio status display.
- BUG-QA-002: market-pulse instrument and price display.
- BUG-QA-003: flat valuation-chart axis padding.

---

## 3. What exists today

### 3.1 One service, one runtime loop

`NovaMarketService` (`src/novacontrol/novamarket/service.py`) owns the subsystem lifecycle. The application runtime registers it through `NovaMarketModule` (`src/novacontrol/novamarket/module.py`). The API process calls `NovaMarketService.start_runtime()` once during application startup (`src/novacontrol/api/app.py`).

`start_runtime()` creates one `asyncio.Task` named `novamarket-runtime`. `_run_runtime()` sleeps for the cadence returned by `plan_market_refresh()` and calls `_runtime_tick()`. `_runtime_tick()`:

- ticks notifications;
- refreshes the event radar when due;
- refreshes bars for currently held instruments when due.

It does **not** call discovery, evaluate strategies, assess proposals, submit orders or monitor exits.

`runtime_status()` currently reports `task_running`, `interval_seconds`, `paused`, `last_tick`, `last_event_refresh`, `last_position_refresh`, `last_error` and the refresh plan. This is the correct integration point for an autonomy tick, but it is not itself an autonomy manager.

### 3.2 Discovery has its own scheduler

`AssetDiscoveryEngine` (`src/novacontrol/novamarket/discovery/engine.py`) has:

- `run_cycle(force, trigger, limit)` with `_cycle_lock` and minimum-interval throttling;
- `start_scheduler()`, `_scheduler_loop()`, `stop_scheduler()` and `scheduler_status()`;
- session checks through `src/novacontrol/novamarket/discovery/sessions.py`.

That session table knows weekdays and exchange hours but explicitly does **not** know holidays. The newer verified calendar in `src/novacontrol/novamarket/marketdata/calendar.py` does know 2026 NSE/BSE holidays, early closes and coverage years. The discovery scheduler has not been migrated to it. This is a material gap for autonomy.

The generic `src/novacontrol/scheduler/scheduler.py` `InMemoryScheduler` is an application-level once-off task queue. It is not used by NovaMarket and is not suitable for a durable trading lifecycle. This design does not use it.

### 3.3 The manual paper chain is real and risk-gated

The accepted chain is:

```text
StrategyEngine.evaluate()
  -> StrategySignal
  -> StrategyEngine.propose()
  -> ProposedAction (status=created)
  -> RiskEngine.assess(add_position|reduce_position|remove_position)
  -> proposal approved / rejected / restricted / pending
  -> explicit POST /market/strategies/proposals/{id}/forward
  -> ExecutionEngine.submit()
  -> _validate() checks approved proposal, approved and unexpired NM-08 decision,
     active portfolio, usable market reference and cash/position availability
  -> _revalidate_risk() re-asks NM-08 with the actual order notional
  -> reserve cash/position -> Order -> process/fill -> wallet ledger + portfolio position
```

Important source facts:

- `StrategyEngine.run()` evaluates active strategies, resolves conflicts and creates proposals. It does not forward them.
- `StrategyEngine.propose()` maps BUY → `add_position`, SELL → `reduce_position`, EXIT → `remove_position`, and calls `RiskEngine.assess()`.
- `StrategyEngine.forward()` refuses anything except an approved, unexpired proposal with `risk_outcome == "approved"` and then calls the lazy service forwarder.
- `NovaMarketService._forward_strategy_to_execution()` calls `ExecutionEngine.submit(proposal_id, actor=...)`.
- `ExecutionEngine.submit()` is idempotent by `proposal_id`; `ExecutionStore` has a unique `execution_orders.proposal_id`. A repeated submission replays the stored order.
- `ExecutionEngine._validate()` rechecks the risk decision, portfolio status, market reference, position/cash and restrictions. `_revalidate_risk()` calls NM-08 again with the actual notional.
- `ExecutionEngine.process()` advances an open order; `recover_pending()` compensates a pending cross-store fill after a restart.
- `PortfolioEngine.apply()` is a separate manual rebalance path: it moves wallet cash and positions itself. It is **not** the NM-10 paper-order path. It must not be used by the autonomous engine for order execution.

This last distinction is central. The autonomous engine uses the strategy proposal → NM-08 → NM-10 path. It never uses `PortfolioEngine.apply()` as a shortcut.

### 3.4 The lifecycle is not autonomous

`AUTONOMOUS_PAPER_TRADING_AUDIT.md` records what is missing:

- no runtime mode state or start/pause/resume/stop contract for trading;
- no automatic transition from a signal to an execution request;
- no open-position exit supervisor;
- no cycle, decision or lease record that survives a restart;
- no kill switch or autonomy-specific health surface.

That is the gap this design fills.

---

## 4. Target architecture

### 4.1 Component view

```text
NovaControl application runtime
  └── NovaMarketModule
        └── NovaMarketService  (existing lifecycle, stores, API methods)
              ├── existing market data / discovery / forecast / portfolio /
              │   risk / strategy / execution / backtest / shadow / learning
              └── AutonomyRuntime  (new)
                    ├── AutonomyScheduler  (new, pure cadence planner)
                    ├── AutonomyOrchestrator  (new, composition only)
                    │     ├── AssetDiscoveryEngine.run_cycle()
                    │     ├── StrategyEngine.run()/evaluate()/propose()
                    │     ├── RiskEngine.assess()  (through StrategyEngine)
                    │     ├── StrategyEngine.forward() -> ExecutionEngine.submit()
                    │     ├── ExecutionEngine.process()/cancel()
                    │     ├── PortfolioEngine.detail()/positions()/valuation()
                    │     ├── ExitSupervisor  (new, exit policy evaluation)
                    │     └── LearningService.record_evidence()  (after fills)
                    └── AutonomyStore  (new, same SQLite database)
```

### 4.2 Responsibilities

| Component | Responsibility | Does not own |
|---|---|---|
| `AutonomyRuntime` | Mode, runtime state, lease, heartbeat, tick dispatch, recovery, circuit breaker, lifecycle API | Risk policy, execution, accounting |
| `AutonomyScheduler` | Decide whether a portfolio/horizon is due; use verified exchange calendar and market-refresh plan | Orders, signals |
| `AutonomyOrchestrator` | Compose existing services for one cycle: discovery, strategy run, risk proposals, execution forwarding, open-order processing, exit checks, records | A second strategy, risk, execution or portfolio engine |
| `ExitSupervisor` | Evaluate stop-loss, target, trailing, time and optional portfolio-risk exits; call the existing proposal/risk path | Direct wallet/position mutation |
| `AutonomyStore` | Durable runtime state, config, cycles, decisions, exit high-water marks, lease | Financial ledger or portfolio accounting |
| `AutonomyPage` (UI) | Status, controls, scans, decisions, orders, exits, health | Local financial calculations |

The orchestrator is deliberately thin. It does not compute a new risk score, a new position value, a new average cost, a new P&L or a new fill. It asks the owning service for each.

### 4.3 Single runtime loop

The design adds **no second long-running task**. `NovaMarketService._runtime_tick()` calls:

```python
await self._autonomy.tick(now)
```

after the existing notification/radar/held-instrument housekeeping. `AutonomyRuntime.tick()` uses the service's own `asyncio.Lock` and a short-lived database lease, and returns quickly when the mode is disabled, paused, not due, or the market is closed.

Consequences:

- the existing `start_runtime()` / `stop_runtime()` contract remains the only service monitor lifecycle;
- the application startup call to `start_runtime()` in `api/app.py` automatically gives autonomy its tick;
- `shutdown()` stops the autonomy runtime before the service withdraws, releasing the lease;
- no duplicate scheduler task can be started by clicking a UI button twice;
- the market-refresh cadence (15 minutes live by default) is the lower bound on scan frequency. The system does not claim tick-level trading.

### 4.4 New package layout

AT-01 created `__init__.py`, `models.py`, `store.py` and `runtime.py`. The remaining files in this listing are AT-02+ design and do not exist yet:

```text
src/novacontrol/novamarket/autonomy/
  __init__.py
  config.py        # AutonomyConfig, validation, defaults
  models.py        # modes, states, cycle/decision/exit records
  store.py         # AutonomyStore, schema version, lease SQL
  scheduler.py     # cadence and session/calendar decisions
  runtime.py       # AutonomyRuntime lifecycle, tick, recovery, circuit breaker
  orchestrator.py  # one cycle composition
  exit_supervisor.py
```

No existing package is copied into this directory. The modules import the existing engines through the service or through narrow, read-only callables.

---

## 5. The autonomous lifecycle

### 5.1 Start

An explicit operator action sets a desired mode. Starting is refused unless:

1. NovaMarket is operational (`READY/RUNNING/DEGRADED` per the existing lifecycle);
2. `NovaMarketService` is not shutting down;
3. the configured portfolio allowlist is non-empty;
4. NM-10 status reports `paper_only=True`, `live_trading=False`, `broker_connected=False`;
5. the risk and portfolio services can be opened;
6. the caller has acknowledged paper-only operation for `autonomous` mode.

`monitoring` may start without the autonomous acknowledgement because it cannot submit an order. `autonomous` requires an explicit acknowledgement in the request body. There is no UI or API path that switches to live.

The first tick after a start performs a **preflight**, not an order: calendar coverage, session state, data freshness, risk availability, execution availability, portfolio state and lease acquisition. A failed preflight enters `DEGRADED` or `PAUSED`; it never proceeds to submission.

### 5.2 Scan and discovery

For each tick, the scheduler decides which `(portfolio, horizon)` scopes are due. Discovery is global and is run at most once per tick:

1. `AssetDiscoveryEngine.run_cycle(force=False, trigger=SCHEDULED)` is called only when the verified exchange calendar says the market is open and the coverage year is known.
2. The orchestrator reads `candidates(limit=...)` and held instruments.
3. If discovery fails, the cycle records the error and continues only with held instruments for exit/protection; it does not invent candidates.
4. The discovery engine's own scheduler is not started by autonomy. If an operator has started it separately, its `_cycle_lock` prevents overlapping cycles; the autonomy cycle records a skip instead of duplicating work.

`StrategyEngine._instruments()` currently limits the evaluated set to 20 names and prefers requested IDs, then holdings, then discovery candidates. This is a real bound. Autonomy treats 20 as a per-cycle batch cap (configurable through a small, non-financial change described in the gap matrix) and rotates candidates across cycles; it does not claim a full-universe strategy evaluation in one tick.

### 5.3 Decision

`StrategyEngine.run(portfolio_id, instrument_ids=..., conflict_policy="review", actor="autonomy")` is the decision producer. It:

- evaluates active strategies that declare the portfolio's horizon (the engine already rejects a strategy whose `horizons` do not include the portfolio horizon);
- stores signals with deterministic IDs based on strategy, version, instrument, portfolio, data cutoff and signal type;
- resolves duplicate/opposite signals through the existing conflict table;
- creates proposals and asks NM-08 for a decision;
- returns signals, conflicts, proposals and errors.

Autonomy maps the result to a durable decision record:

| Signal / proposal | Autonomous action | Existing risk action |
|---|---|---|
| BUY, no held position | `open` | `add_position` |
| BUY, existing held position | `add` | `add_position` |
| SELL | `reduce` | `reduce_position` |
| EXIT | `close` | `remove_position` |
| HOLD / NO_SIGNAL | `hold` (no proposal) | none |

The `open`/`add` distinction is a reporting/decision distinction. It does not change the accepted risk action or execution side. `HOLD` is recorded but never becomes an order. A conflict under the default `review` policy produces no proposal for that instrument in that cycle; the conflict remains visible for an operator.

### 5.4 Risk

Autonomy never approves a proposal. It reads the NM-08 result and forwards only when all of these are true:

- `proposal.status == "approved"`;
- `proposal.risk_outcome == "approved"`;
- `proposal.risk_decision_id` is non-empty;
- `proposal.expires_at > now`;
- the risk decision is unexpired and bound to the same proposal;
- the proposal has no restrictions that NM-10 cannot enforce.

NM-08 remains authoritative for available cash, projected exposure, per-position and per-sector weights, drawdown, volatility, liquidity, forecast eligibility, event pressure, restricted instruments, data age and state age. `ExecutionEngine._validate()` performs a second independent revalidation with the actual order notional.

A risk `error`, `requires_review`, `insufficient_data` or `restricted` result is recorded with its reason and is never forwarded. The `restricted` case is especially important: NM-10 rejects a decision carrying unenforceable restrictions.

### 5.5 Paper execution

For an approved proposal, autonomy calls:

```python
strategy.forward(proposal_id, actor="autonomy:<instance_id>")
```

which reaches `ExecutionEngine.submit()`. The execution engine:

1. replays an existing order for the same proposal (idempotency);
2. validates the approved proposal and risk decision;
3. validates the active portfolio, usable market reference, cash/position and restrictions;
4. revalidates risk with the actual notional;
5. reserves cash or quantity;
6. creates the order and processes it (market orders fill; limit/stop orders remain open);
7. writes the fill journal, wallet entries and portfolio position through the existing cross-store sequence.

Autonomy then:

- records the order ID and status on the decision;
- records fills and the portfolio/ledger references;
- processes only open orders whose `actor` begins with `autonomy:`; manual operator orders are not touched;
- never calls `ExecutionEngine.submit()` again for a failed/terminal order;
- never edits a fill, ledger entry or position.

Open orders are processed on subsequent ticks with fresh data. Cancel/expiry/recovery remain NM-10 responsibilities.

### 5.6 Position monitoring and exits

Existing strategies can emit `SELL` or `EXIT`. Those signals use the same proposal/risk/execution path.

Protective exits are handled by `ExitSupervisor`, which is a policy evaluator, not an execution engine. It reads positions from `PortfolioEngine.detail()`, the average cost and `opened_at` from the position, and a usable price/bar window from the market-data engine. It evaluates:

- stop-loss versus average cost;
- target versus average cost;
- trailing stop versus a persisted high-water mark;
- time exit using the horizon profile's holding period;
- strategy exit when a strategy has already emitted `EXIT`;
- optional portfolio-risk exit when the configured drawdown threshold is breached.

To create an exit through the accepted path, the design adds one narrow method to the existing strategy engine:

```python
await strategy.propose_autonomous_exit(
    portfolio_id=...,
    instrument_id=...,
    kind="stop_loss" | "take_profit" | "trailing_stop" | "time_exit" | "portfolio_risk",
    fraction=Decimal("1") | Decimal("0.5") | ...,
    reason=...,
    actor="autonomy",
)
```

That method factors the existing proposal/risk step into `_propose_with_risk()` and creates an `EXIT` (full close) or `SELL` (partial reduction) signal/proposal with a reserved `strategy_id` of `system:protective-exit`. It does not bypass NM-08 or NM-10. `StrategyEngine.forward()` handles it exactly like any other approved proposal.

The supervisor persists its high-water mark, last attempt and last decision in `autonomy_exit_state`. It refuses to evaluate an exit on stale or missing data. It never creates a second active exit proposal for the same position while an exit order is open.

### 5.7 Portfolio and capital

The existing `PortfolioEngine` remains the accounting owner. Autonomous sizing uses the accepted strategy path:

- BUY notional: `notional_fraction` of portfolio total, as already implemented by `StrategyEngine._notional()`;
- SELL: 50% of the held market value by default, as already implemented;
- EXIT: the full held market value, as already implemented;
- NM-08: projected exposure, cash, position/sector weights, liquidity and drawdown.

The autonomy preflight additionally reads the portfolio's `HORIZON_POLICIES[horizon]` `max_positions` and `min_cash_fraction` as guardrails, so a new entry cannot exceed the accepted portfolio shape before risk even sees it. This is a pre-filter, not a new risk policy.

`PortfolioEngine.propose()/apply()` remains the manual target-weight rebalance path. It is not invoked by autonomy because it applies ledger transfers and positions directly rather than through NM-10. This is a deliberate boundary and is recorded as a gap for a future, separately reviewed execution-aware allocator. Autonomy therefore does **not** claim autonomous target-weight rebalancing in v1; it claims autonomous signal-driven capital allocation within the accepted risk and portfolio guardrails.

Realized/unrealized P&L, cash, exposure and performance are read from the existing portfolio/valuation/performance methods. Autonomy never recomputes them.

### 5.8 Learning

After a paper order reaches a terminal state, autonomy records evidence through the existing `LearningService.record_evidence()` with `kind="paper"`, the strategy ID, the order/fill references and the measured result. It can call `LearningService.monitor()` to raise deterioration alerts.

Autonomy never calls `approve_candidate()`, `promote_candidate()` or `rollback()`. Those remain operator/governance actions in the existing learning UI. A learning change can influence a strategy only after the existing approval/promotion flow, and every future order still passes NM-08 and NM-10.

---

## 6. Operating modes

| Mode | Market scan | Signals / proposals / risk | New entries | Protective exits | Open autonomy orders | Intended use |
|---|---|---|---|---|---|---|
| `disabled` | No | No | No | No | No new processing | Off |
| `monitoring` | Yes | Yes, including risk decisions | No | Recorded only; no order | No | Observe and validate |
| `autonomous` | Yes | Yes | Approved entries | Yes, through risk/execution | Yes | Full paper autonomy |
| `paused` | Protected scopes only | Exit evaluation only | No | Yes by default, subject to `paused_exit_policy` | Exit orders only | Stop new risk while protecting positions |

`monitoring` is not "autonomous with the order button hidden": it never calls `StrategyEngine.forward()` or `ExecutionEngine.submit()`.

`paused` blocks new entries. Protective exits are not new entries; leaving a stop unarmed while paused would be a safety regression. Owner decision OD-02 fixes `paused_exit_policy="protective"`; `monitor_only` is not a supported paused policy. `disabled` and the kill switch stop all autonomous orders.

---

## 7. Safety boundary

The following are hard invariants, not defaults:

1. **Paper only.** `ExecutionEngine.status()` returns `paper_only=True`, `live_trading=False`, `broker_connected=False`; `contract()` returns `live_broker=False`.
2. **No live path.** No route, configuration field or runtime flag can select a broker or submit a real order. Autonomy's start request has no live mode.
3. **No bypass.** Every order comes from an approved `ProposedAction` with an approved NM-08 decision; NM-10 revalidates it.
4. **No direct financial mutation.** Autonomy does not call `PortfolioEngine.apply()`, wallet transfer methods or ledger correction methods.
5. **Fail closed.** A stale price, unknown calendar coverage, unavailable risk engine, unavailable execution engine, lost lease or unreadable portfolio means no new order.
6. **No duplicate order.** The proposal ID and `execution_orders.proposal_id` uniqueness provide idempotency; autonomy never invents a second order ID.
7. **No automatic learning promotion.** Learning recommendations require the existing governance flow.
8. **No automatic restart into autonomous mode.** Owner decision OD-06: a restart always comes up `disabled` and requires an authorized operator to start the shell again. Auto-start and auto-resume do not exist (`resume_on_restart` is not a supported setting).

A start-up assertion checks the execution contract flags and refuses to bring the autonomy runtime out of `disabled` if any paper-only invariant is not true. This is a defensive assertion, not a replacement for the existing execution checks.

---

## 8. Persistence

`AutonomyStore` uses the **same SQLite database file** as the other NovaMarket stores (the service's existing data path). It gets its own schema version and tables; no existing table is altered. DDL is proposed only; it has not been executed.

Core tables:

- `autonomy_config` — one validated configuration row with a monotonically increasing `config_version`;
- `autonomy_runtime` — one row: mode, runtime state, lease owner/expiry, heartbeat, generation, last cycle, last successful scan/execution, last error, kill timestamp;
- `autonomy_cycles` — one row per attempted cycle, with a unique `cycle_key`, status, session/data state, counts, errors and timestamps;
- `autonomy_decisions` — one row per observed decision, linking signal/proposal/risk/order IDs and recording action, status and reason;
- `autonomy_exit_state` — per portfolio/instrument high-water mark, last evaluated price, last exit attempt and last decision.

The store never stores a wallet balance, ledger entry, position or fill. Those remain in their owning stores. Cross-store consistency continues to be handled by the existing execution fill journal and reconciliation, not by a new distributed transaction.

---

## 9. Concurrency, recovery and observability

Detailed rules are in `AUTONOMOUS_TRADING_STATE_MACHINE.md` and the test strategy. Summary:

- one service runtime task; one `asyncio.Lock` per autonomy cycle;
- one short-lived SQLite lease per cycle, acquired conditionally, renewed at phase boundaries and released in `finally`;
- deterministic cycle keys and proposal/order IDs;
- each store keeps its own lock and transaction; autonomy never holds a transaction across two stores;
- startup recovery calls `ExecutionEngine.recover_pending()`, `ExecutionEngine.reconcile()`, portfolio reconciliation and marks abandoned cycles `interrupted`;
- bounded retries only for idempotent reads; no blind retry of a write or a submit;
- circuit breakers for repeated data, risk, execution and lease failures;
- every cycle and decision is durable; every financial action also publishes the existing typed event and appears in the existing activity/audit views.

---

## 10. UI integration

The existing React workspace is extended, not redesigned:

- new `AutonomyPage` (`web/src/pages/Autonomy.tsx`) under a new `autonomy` route;
- a compact autonomy status card on `DashboardPage`;
- a cross-link banner on `PaperPage` to the autonomy decisions/orders;
- autonomy health in the existing `HealthPage` and autonomy events in `ActivityPage`;
- reuse `Card`, `Metric`, `DataTable`, `StatusBadge`, `Banner`, `Button`, `ConfirmDialog`, `Tabs` and `useApi` from the existing component library.

The page must provide start, pause, resume and stop controls, a clearly separated kill switch, mode and lease status, last/next scan, current evaluated instruments, signals, decisions, risk results, orders, fills, positions, exits, P&L, errors and links to the existing Paper, Risk and Learning pages. It polls authoritative endpoints; it never derives a price, P&L or order state locally.

---

## 11. Non-goals

- live trading, broker connectivity or real-money execution;
- changing existing financial calculations, ledger rules, valuation rules, NovaCoin conversion or risk policies;
- replacing the existing strategy, risk, execution, portfolio or learning engines;
- a second event bus, scheduler, database or dashboard;
- automatic target-weight rebalancing through `PortfolioEngine.apply()`;
- automatic learning approval, promotion or risk-policy changes;
- claiming exchange tick data from a delayed provider.

---

## 12. Source index

Primary source files verified for this design:

- `src/novacontrol/novamarket/service.py` — lifecycle, runtime loop, lazy subsystem construction, forward bridge, status.
- `src/novacontrol/novamarket/module.py` — application runtime adapter.
- `src/novacontrol/novamarket/discovery/engine.py` — discovery cycle and scheduler.
- `src/novacontrol/novamarket/discovery/sessions.py` — weekday-only discovery sessions.
- `src/novacontrol/novamarket/marketdata/session_schedule.py` — verified refresh planner.
- `src/novacontrol/novamarket/marketdata/calendar.py` — NSE/BSE 2026 calendar, holidays and coverage.
- `src/novacontrol/novamarket/strategies/engine.py` — evaluate, propose, conflict resolution, forward.
- `src/novacontrol/novamarket/strategies/catalog.py` — six strategy families.
- `src/novacontrol/novamarket/risk/engine.py` — independent risk assessment and decision storage.
- `src/novacontrol/novamarket/execution/engine.py` — submit, validation, risk revalidation, processing and recovery.
- `src/novacontrol/novamarket/execution/store.py` — order/fill/reservation schema and proposal uniqueness.
- `src/novacontrol/novamarket/portfolio/engine.py` — portfolio details, rebalance/apply/close, valuation.
- `src/novacontrol/novamarket/portfolio/models.py` — horizons, policies and accounting types.
- `src/novacontrol/novamarket/learning/service.py` — evidence, monitoring and governance.
- `src/novacontrol/api/app.py`, `src/novacontrol/api/models.py`, `src/novacontrol/api/route_consumers.py` — API registration.
- `web/src/App.tsx`, `web/src/nav.ts`, `web/src/pages/*.tsx` — existing UI surfaces.

**Implementation status:** architecture only. No source file, schema, migration or runtime setting was changed by this task.
