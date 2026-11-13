# Autonomous Paper Trading — Component Mapping

**Status:** **AT-01 IMPLEMENTED** (runtime and safety shell only); every later-milestone row remains design and is not implemented. AT-02–AT-06 are not approved or started. Where this document conflicts with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md` (owner-approved 10 November 2026), the owner decisions win.
**Date:** 9 November 2026
**Purpose:** Map every required autonomous capability to the actual NovaMarket modules, methods and stores, and state exactly what is reused, modified or newly built.

A component is marked **reuse** only where its existing responsibility already matches. A component that needs a small extension is marked **modify**, with the exact reason. No accepted financial engine is replaced.

---

## 1. Summary

| Capability | Existing implementation | Disposition |
|---|---|---|
| Runtime lifecycle | `NovaMarketService` + `NovaMarketModule` + one `_run_runtime` task | **Reuse**, add autonomy tick |
| Exchange calendar and session | `marketdata/calendar.py`, `marketdata/session_schedule.py` | **Reuse**, apply to autonomy gate; fix discovery scheduler |
| Discovery | `discovery/engine.py` `run_cycle`, candidates, watchlists | **Reuse** |
| Strategy signals | `strategies/engine.py`, `strategies/catalog.py` | **Reuse** |
| Proposal and conflict handling | `strategies/engine.py` `run`, `propose`, `_resolve_conflicts` | **Reuse** |
| Risk | `risk/engine.py` `assess`, policy store, decision store | **Reuse**, no policy change |
| Paper execution | `execution/engine.py` `submit`, `process`, `cancel`, `recover_pending` | **Reuse** |
| Execution persistence | `execution/store.py` order/fill/reservation schema | **Reuse** |
| Portfolio and positions | `portfolio/engine.py` detail, positions, valuation, performance | **Reuse** for reads |
| Ledger | `wallet/engine.py`, `wallet/store.py` | **Reuse**, untouched |
| Backtesting / shadow / learning | `backtesting/`, `shadow/`, `learning/service.py` | **Reuse** |
| Autonomy manager | none | **New** |
| Autonomy scheduler | none | **New** (pure planner) |
| Autonomous exit supervisor | none | **New**, plus one narrow proposal seam |
| Durable autonomy state | none | **New** store/tables |
| Autonomy API/UI | none | **New** page + route group |
| Target-weight autonomous rebalancing | `PortfolioEngine.propose/apply` exists but bypasses NM-10 | **Not reused**; deliberate gap |
| Generic scheduler | `scheduler.InMemoryScheduler` | **Not used**; not durable/recurring |

The design adds one orchestration package and a small number of precise extensions. It does not add a second implementation of any of the bolded existing services.

---

## 2. A — Autonomous Runtime Manager

### Existing

- `NovaMarketService` (`src/novacontrol/novamarket/service.py`) owns the subsystem lifecycle and status.
- `NovaMarketModule` (`src/novacontrol/novamarket/module.py`) registers it in the application runtime.
- `NovaMarketService.start_runtime()` creates the one `novamarket-runtime` task.
- `_run_runtime()` and `_runtime_tick()` are the existing continuous monitor.
- `runtime_status()` reports task/interval/pause/tick/error/refresh plan.
- `src/novacontrol/api/app.py` calls `start_runtime()` during application lifespan.
- `EventBus`, `MarketEventPublisher` and the existing activity list provide observability.

### New

- `autonomy/runtime.py`: `AutonomyRuntime`, owning mode, runtime state, lease, heartbeat, tick dispatch, recovery and circuit breaker.
- `autonomy/config.py`: `AutonomyConfig` with validated bounds and a `config_version`.
- `autonomy/models.py`: `AutonomyMode`, `RuntimeState`, `CycleStatus`, records and status DTOs.
- `autonomy/store.py`: durable runtime/config/cycle/decision/exit tables.

### Modify

- `NovaMarketService.__init__`: add `self._autonomy`, `self._autonomy_error`.
- `NovaMarketService._ensure_autonomy()`: lazy construction analogous to `_ensure_execution()`.
- `NovaMarketService._runtime_tick()`: call `await self._autonomy.tick(now)` after existing housekeeping.
- `NovaMarketService.runtime_status()` / `status()`: include autonomy summary.
- `NovaMarketService.enable()` / `disable()` / `shutdown()`: construct/close autonomy, and ensure disable means `disabled` while application shutdown preserves the desired mode for recovery.
- `NovaMarketService.forward_strategy_proposal()`: accept an optional `actor` so autonomy forwards with an identifiable actor; the existing API call keeps its current behavior.

### Why

The runtime manager must not be a second task. The existing service already owns startup, shutdown and the event bus. Adding a small tick into it gives one task, one lifecycle and one failure surface.

---

## 3. B — Market Monitoring Scheduler

### Existing

- `marketdata/session_schedule.py` `plan_market_refresh()` returns interval, phase, session state, next check, operating window and provider note.
- `marketdata/calendar.py` `ExchangeCalendar`, `calendar_by_mic("XNSE")`, `state()`, `is_open()`, `next_session_open()`, `coverage()`, NSE/BSE 2026 holidays and early closes.
- `discovery/engine.py` has a bounded scheduled cycle loop with `asyncio.Event`, interval, cycle lock, `TriggerKind.SCHEDULED`, and a session check.
- `discovery/sessions.py` has declared weekday sessions for several exchanges, explicitly without holidays.

### New

- `autonomy/scheduler.py`: `CyclePlan`, `AutonomyScheduler.due_scopes()`, `session_gate()`, `next_due_at()`.
- Per-scope cadence from `AutonomyConfig`:
  - short-term: 15 minutes during the session (bounded by the verified market refresh);
  - medium-term: 60 minutes during the session;
  - long-term: once per trading day after the close (or a configured longer interval).
- A trading-session gate that returns `SKIPPED_SESSION` outside the verified session, `SKIPPED_HOLIDAY` on a holiday, and `MONITORING_ONLY` when the calendar year is outside its holiday coverage.

### Modify / fix

- Apply `marketdata.calendar` to the autonomy gate. This is mandatory.
- `discovery/sessions.py` should either delegate to `marketdata.calendar` for XNSE/XBOM or be bypassed by autonomy's gate. Its current weekday-only behavior is not sufficient for holidays. This is a **P1** gap.
- Do not start both `AssetDiscoveryEngine.start_scheduler()` and the autonomy scheduler as independent trading loops. Autonomy calls `run_cycle` itself. If the discovery scheduler is already running, `_cycle_lock` causes a skip, which is recorded rather than duplicated.

### Why

The verified calendar and refresh planner already exist. A second calendar or a second timer would be a source of disagreement. The autonomy scheduler is a pure decision function, not a new background service.

---

## 4. C — Opportunity Discovery

### Existing

- `AssetDiscoveryEngine.run_cycle(force=False, trigger=SCHEDULED, limit=None)` scans the declared universe, evaluates candidates and persists append-only evaluations.
- `candidates(limit=...)`, `evaluation(s)`, `watchlists`, `sessions_now()`.
- `StrategyEngine._instruments()` chooses requested IDs, then holdings, then up to 20 discovery candidates.
- Deterministic signal IDs use strategy/version/instrument/portfolio/data-cutoff/signal type.

### New

- `AutonomyOrchestrator._scan()`:
  1. read current held instruments;
  2. run discovery at most once per tick when due;
  3. read candidates up to `max_instruments_per_cycle`;
  4. pass holdings + candidates to `StrategyEngine.run`;
  5. record scanned IDs and skipped reasons.

### Modify

- None required to discovery for v1, except the session-vs-holiday issue above.
- A small non-financial extension to make the 20-name cap configurable is recommended in AT-02. The current hard slice in `_instruments` is real and must not be mistaken for a full-universe scan.

### Duplicate suppression

Two levels already exist:

- discovery evaluations and candidates are persisted and append-only;
- strategy signal IDs are deterministic for the same data cutoff, and proposals are replayed by `proposal_id`.

The autonomy layer adds a unique cycle key and a decision uniqueness constraint, so a repeated UI click or a restart cannot create a second cycle for the same scope and time window.

---

## 5. D — Autonomous Decision Engine

### Existing

- `strategies/catalog.py` implements six families: trend, mean reversion, momentum, volatility, event and forecast.
- `StrategyEngine.evaluate()` checks the active status and portfolio horizon, reads the latest usable quote and 200 bars, computes the strategy, and persists a signal.
- `StrategySignal` supports BUY, SELL, HOLD, EXIT and NO_SIGNAL.
- `StrategyEngine.run()` evaluates all requested active strategies, filters actionable measured signals, resolves conflicts and auto-proposes.
- `StrategyEngine.propose()` maps BUY/SELL/EXIT to `add_position`/`reduce_position`/`remove_position` and calls NM-08.
- `StrategyEngine.forward()` is the handoff to NM-10.

### New

- `autonomy/decisions.py` (or functions in `orchestrator.py`) mapping signals to the durable decision vocabulary `open|add|reduce|close|hold`.
- Preflight guards:
  - `max_positions` from `portfolio.models.HORIZON_POLICIES[horizon]`;
  - requested notional/pieces per cycle from `AutonomyConfig`;
  - no entry when the portfolio is not `active`;
  - no entry when the session is not open and the coverage is not verified.
- `strategies/engine.py` `propose_autonomous_exit()` for the exit supervisor, sharing `_propose_with_risk()` with `propose()`.

### Not changed

- No new strategy family is required for the lifecycle.
- No new risk action is introduced; `open`/`add` is an autonomy read-model distinction, not a new `RiskAction`.
- HOLD remains a recorded observation, not an order.
- Conflict policy defaults to `review`; autonomy does not silently choose a winner.

---

## 6. E — Autonomous Risk Management

### Existing

- `RiskEngine.assess()` is the independent gate. It persistently records decisions and deduplicates by decision ID.
- It projects the proposed action and measures:
  - position and sector concentration;
  - invested fraction and cash;
  - drawdown and volatility;
  - turnover and liquidity;
  - forecast/model-state eligibility and uncertainty;
  - event pressure;
  - data age and state age;
  - restricted instruments;
  - capital allocation/reservation.
- `risk/policies.py` evaluates policy limits; `risk/store.py` stores decisions, alerts, overrides and consumptions.
- `ExecutionEngine._validate()` and `_revalidate_risk()` recheck the decision and re-assess with the actual notional.

### New

- `AutonomyOrchestrator` preflight reads:
  - portfolio status and valuation;
  - session/data freshness;
  - risk service availability;
  - `HORIZON_POLICIES` max positions and cash reserve as pre-filters.
- A `RiskRejected` / `RiskRestricted` decision record with the NM-08 reason.

### Not changed

- No risk policy value is changed.
- No new risk score is calculated in autonomy.
- No override, emergency bypass or "trusted strategy" flag exists.
- A risk error is `insufficient_data`, not approval.

### Coverage note

NM-08 does not currently enforce the portfolio policy's `max_positions` count. Autonomy uses that count as a pre-filter; the authoritative risk engine still decides cash, exposure and all other limits. This is recorded as a high-priority gap because an autonomous loop could otherwise open more names than the accepted portfolio shape allows.

---

## 7. F — Autonomous Paper Execution

### Existing

- `ExecutionEngine.submit(proposal_id, ...)`:
  - replays a stored order for the same proposal;
  - validates the approved proposal and approved/unexpired NM-08 decision;
  - checks active portfolio, instrument, price, cash/position and restrictions;
  - revalidates risk with actual notional;
  - reserves, creates and processes the order.
- `process(order_id)` advances open/partial orders.
- `cancel(order_id, reason=...)` cancels remaining quantity and releases reservations.
- `recover_pending()` compensates a pending fill journal row after restart.
- `ExecutionStore` has unique `execution_orders.proposal_id`, order transitions, fills, reservations and reconciliation.
- `Order` carries `actor`, which autonomy uses as `autonomy:<instance_id>`.

### New

- `AutonomyOrchestrator._execute()`:
  - reads approved proposals from the strategy result;
  - checks status/risk/expiry;
  - calls `strategy.forward(proposal_id, actor=...)`;
  - records order/fill IDs on the decision;
  - reads and processes only open orders whose actor starts with `autonomy:`.
- A per-cycle cap on new submissions and a stop-on-execution-unavailable circuit breaker.

### Modify

- `NovaMarketService.forward_strategy_proposal(proposal_id, actor="")` and the internal `_forward_strategy_to_execution` already accept an actor; the public service wrapper currently does not. Add the optional parameter without changing the existing API response.
- No new execution route is needed. The existing `/market/execution/orders` remains the manual surface.

### Why no new execution service

`ExecutionEngine` already implements the hard parts: idempotency, risk revalidation, order state machine, fees, slippage, partial fills, reservations, ledger compensation and reconciliation. An autonomous layer that submits through it is adding orchestration, not execution.

### Explicitly excluded

- `PortfolioEngine.apply()` is not used for autonomous orders. It is a separate manual rebalance path that moves the book directly, outside NM-10.
- No direct `WalletEngine.transfer`, ledger posting or position write is allowed in autonomy code.

---

## 8. G — Position Monitoring and Autonomous Exits

### Existing

- `PortfolioEngine.detail()` and `positions()` return holdings, average cost, opened_at, last price, market value and weight.
- `portfolio/horizons.py` declares day/swing/long holding periods and stop-loss fractions.
- Existing strategies can emit SELL/EXIT.
- `ExecutionEngine` supports partial sells and full exits; `StrategyEngine.propose()` enforces SELL <= holding and EXIT == full holding value.

### New

- `autonomy/exit_supervisor.py`:
  - stop-loss, target, trailing stop, time exit, strategy-exit confirmation and optional portfolio-risk exit;
  - high-water-mark persistence;
  - fresh-price and duplicate-order guards.
- `autonomy_exit_state` table.
- `StrategyEngine.propose_autonomous_exit()`:
  - full close creates `EXIT`/`remove_position`;
  - partial reduction creates `SELL`/`reduce_position`;
  - uses the same proposal store, NM-08 assessment and NM-10 forward path.

### Modify

- Extract the common proposal/risk body of `StrategyEngine.propose()` into `_propose_with_risk()` so the normal and supervisor paths cannot diverge.
- Add a reserved `system:protective-exit` strategy identity for the signal/proposal provenance. This is not a user-configurable strategy and does not bypass any status/risk check.

### Guarantees

- no exit order without a usable timestamped price;
- no duplicate active exit proposal/order for the same position;
- no exit if the position is already closed or held by another strategy's pending full exit;
- every exit still passes NM-08 and NM-10;
- `paused` defaults to protective exits, `disabled`/kill never submits.

---

## 9. H — Portfolio and Capital Management

### Existing

- `PortfolioEngine.detail()`, `valuation()`, `performance()`, `positions()`, lifecycle status and reconciliation.
- `PortfolioPolicy` and `HORIZON_POLICIES` define max positions, max position/sector weight, cash reserve, drift, cooldown and turnover.
- `StrategyEngine._notional()` sizes BUY/SELL/EXIT from the accepted configuration.
- Ledger and wallet services are the only cash owners; portfolio positions are the only book.

### New

- Autonomy preflight guardrails:
  - portfolio active;
  - max positions for new entries;
  - per-cycle new-entry count/notional cap;
  - available cash and exposure remain NM-08 decisions.
- Read-only portfolio P&L and performance panels in the autonomy UI.

### Deliberate boundary

`PortfolioEngine.propose()/apply()` is a validated manual rebalance mechanism, not an order gateway. It changes positions and wallet cash directly. Autonomy does **not** call it. As a result, v1 does not provide autonomous target-weight rebalancing; it provides autonomous signal-driven allocation within the accepted strategy sizing and risk limits.

A future execution-aware allocator would need a new bridge that converts rebalance intents into approved proposals without double-counting cash or bypassing NM-10. That is not part of the minimum lifecycle and is recorded in the gap matrix.

---

## 10. I — Learning and Strategy Evaluation

### Existing

- `learning/service.py` `record_evidence(payload)` accepts `kind=backtest|paper|shadow`, validates provenance and stores evidence.
- `monitor(strategy_id, kind=..., window_days=...)` raises deterioration alerts.
- `dashboard()`, `alerts()`, `candidates`, `experiments`, `approve_candidate()`, `promote_candidate()`, `rollback()`.
- `StrategyEngine.performance(strategy_id)` and `PortfolioEngine.performance()` provide measured results.
- `backtesting/` and `shadow/` remain separate books.

### New

- `AutonomyOrchestrator._learning_evidence()` after a terminal fill/close:
  - builds a `paper` evidence payload with strategy, instrument, order/fill references, measured P&L and provenance;
  - calls `record_evidence`;
  - optionally calls `monitor` for the strategy.
- Autonomy UI shows strategy performance and learning alerts; it does not approve or promote.

### Not changed

- No automatic candidate approval.
- No automatic promotion or rollback.
- No change to risk policy from a learning result.
- No shadow or backtest book is treated as live evidence.

---

## 11. J — Observability and Dashboard Integration

### Existing backend

- `NovaMarketService.status()` and `workspace_overview()` expose subsystem, portfolio, execution, risk, learning and activity data.
- `/market/execution/status`, `/market/execution/orders`, `/market/execution/fills`, `/market/execution/reconciliations`.
- `/market/strategies/...`, `/market/risk/...`, `/market/portfolios/...`, `/market/learning/...`.
- `MarketEventPublisher` and the existing typed events.
- `ActivityPage`, `HealthPage`, `DashboardPage`, `PaperPage`, `StrategiesPage`, `RiskPage`, `ShadowPage`, `LearningPage`.

### New backend

- `/market/autonomy/*` route group (see `AUTONOMOUS_TRADING_API_DESIGN.md`).
- `AutonomyStore` cycle/decision records for durable readback.
- New typed event names for autonomy runtime/cycle/exit state, or reuse existing strategy/risk/execution/portfolio events plus the durable records.

### New frontend

- `web/src/pages/Autonomy.tsx`.
- `nav.ts`: new `Autonomy` item under Trading Intelligence.
- `App.tsx`: `case "autonomy"`.
- `DashboardPage`: autonomy summary card.
- `PaperPage`: banner/link to autonomy decisions and orders.
- `System.tsx`: autonomy health block.
- Reuse existing UI primitives and `useApi`; no new charting or state library.

### Trace format

Every new autonomy decision exposes the chain:

```text
cycle_id
  -> decision_id
     -> signal_id
     -> proposal_id
     -> risk_decision_id
     -> order_id
     -> fill_ids[]
     -> portfolio_id / instrument_id
```

The UI links each ID to the existing pages; it does not duplicate their calculations.

---

## 12. Existing components that must not be duplicated

| Existing owner | Rule |
|---|---|
| `WalletEngine` / `WalletStore` | Only owner of balances and ledger entries |
| `PortfolioEngine` / `PortfolioStore` | Only owner of positions, valuations and portfolio lifecycle |
| `RiskEngine` / `RiskStore` | Only owner of risk policies and decisions |
| `ExecutionEngine` / `ExecutionStore` | Only owner of orders, fills, reservations and execution reconciliation |
| `StrategyEngine` | Only owner of signals, proposals, conflicts and strategy performance |
| `AssetDiscoveryEngine` | Only owner of discovery cycles, candidates and watchlists |
| `LearningService` | Only owner of evidence, experiments and governance |
| `EventBus` / `MarketEventPublisher` | Only event path |
| `NovaMarketService` | Only subsystem lifecycle and API composition root |

The autonomy package may read these services and call their public methods. It may not maintain a parallel copy of their state.

---

## 13. Files likely affected by implementation

New:

- `src/novacontrol/novamarket/autonomy/__init__.py`
- `src/novacontrol/novamarket/autonomy/config.py`
- `src/novacontrol/novamarket/autonomy/models.py`
- `src/novacontrol/novamarket/autonomy/store.py`
- `src/novacontrol/novamarket/autonomy/scheduler.py`
- `src/novacontrol/novamarket/autonomy/runtime.py`
- `src/novacontrol/novamarket/autonomy/orchestrator.py`
- `src/novacontrol/novamarket/autonomy/exit_supervisor.py`
- `web/src/pages/Autonomy.tsx`

Modified (only where the design requires it):

- `src/novacontrol/novamarket/service.py` — tick, lazy runtime, status, forward actor.
- `src/novacontrol/novamarket/strategies/engine.py` — `_propose_with_risk()` and `propose_autonomous_exit()`.
- `src/novacontrol/novamarket/discovery/sessions.py` (or a gated caller) — holiday-aware scheduling.
- `src/novacontrol/api/app.py` — handlers.
- `src/novacontrol/api/models.py` — `ApiSurface` rows.
- `src/novacontrol/api/route_consumers.py` — consumer entries.
- `docs/API.md` — generated reference.
- `web/src/App.tsx`, `web/src/nav.ts`, `web/src/pages/Dashboard.tsx`, `web/src/pages/Trading.tsx`, `web/src/pages/System.tsx` — UI integration.

No change is proposed to:

- wallet/ledger accounting;
- portfolio valuation or NovaCoin conversion;
- risk policy values;
- market-data interpretation;
- NovaControl core or NovaForge;
- the existing accepted NM-01–NM-14 API contracts, except additive routes and the optional actor parameter.
