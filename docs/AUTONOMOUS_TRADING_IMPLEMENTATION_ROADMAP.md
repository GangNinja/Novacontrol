# Autonomous Paper Trading — Implementation Roadmap

**Status:** **AT-01 IMPLEMENTED** (runtime and safety shell only). AT-02–AT-06 are not approved and not started; they remain planning only. This is not an approved NM-15 phase and must not be referred to as one. Where this document conflicts with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md` (owner-approved 10 November 2026), the owner decisions win.
**Date:** 9 November 2026
**Basis:** The accepted NM-01–NM-14 baseline, the autonomous audit (`AUTONOMOUS_PAPER_TRADING_AUDIT.md`) and the source-verified design in `AUTONOMOUS_TRADING_ARCHITECTURE.md`.

---

## 1. Sequencing rule

The roadmap is ordered by **safety dependency**, not by feature appeal:

```text
AT-01  runtime and safety shell
  -> AT-02  session-aware monitoring and discovery
  -> AT-03  decisions and risk (monitoring only)
  -> AT-04  autonomous paper execution
  -> AT-05  position supervision and exits
  -> AT-06  recovery, observability and full acceptance
```

`AT-04` and `AT-05` can be developed behind an internal release gate, but the operator-facing `autonomous` mode is **not exposed** until `AT-06` has passed the full lifecycle, crash-recovery and kill-switch acceptance. Until then, operators can use `monitoring`; the order path is exercised only by the dedicated integration/E2E harness with a temporary database.

Each milestone must be independently testable and must leave the previous behavior intact. No milestone changes a financial calculation, ledger rule, risk policy value or valuation rule.

---

## 2. Milestone summary

| ID | Title | Primary outcome | Complexity | Exposes autonomous orders? |
|---|---|---|---|---|
| AT-01 | Autonomous Runtime and Safety Shell | Modes, lease, config, status, controls, no trading | High | No |
| AT-02 | Session-Aware Monitoring and Discovery | Verified-session scans, cycle records, no orders | Medium | No |
| AT-03 | Decision and Risk Pipeline | Signals/proposals/NM-08 decisions, no orders | Medium | No |
| AT-04 | Autonomous Paper Execution | Approved proposals → NM-10 orders/fills, idempotent | High | Only in test harness |
| AT-05 | Position Supervision and Exits | Protective exits, paused policy, learning evidence | High | Only in test harness |
| AT-06 | Recovery, Observability and Acceptance | Restart recovery, breakers, kill, full UI, all 20 scenarios | High | Yes, after acceptance |

Complexity is relative engineering effort, not a calendar estimate.

---

## 3. AT-01 — Autonomous Runtime and Safety Shell

### Objective

Add a durable autonomy manager with explicit operating modes, a single-cycle lease, validated configuration and authoritative status/control APIs. It does not scan, propose or execute.

### Existing components to reuse

- `NovaMarketService` lifecycle and `_run_runtime`/`_runtime_tick`.
- `NovaMarketModule` application registration.
- Existing `EventBus`, `MarketEventPublisher` and status patterns.
- Same-origin middleware, FastAPI error conventions and the ordinary `require_auth` principal — **plus** the dedicated autonomy-operator credential required by OD-05.
- React `useApi`, `Card`, `Metric`, `StatusBadge`, `Button`, `ConfirmDialog`.
- Existing SQLite data directory and schema-version conventions.

### New components

- `autonomy/models.py`, `store.py`, `runtime.py` (AT-01); `config.py` remains future design.
- `AutonomyRuntime` mode/state/lease/recovery shell.
- `autonomy_config`, `autonomy_runtime`, `autonomy_cycles` schema v1.
- `web/src/pages/Autonomy.tsx` shell.

### Files/modules likely affected

- New: `src/novacontrol/novamarket/autonomy/{__init__,config,models,store,runtime}.py`.
- Modified: `src/novacontrol/novamarket/service.py` (lazy runtime, tick hook, status, enable/disable/shutdown), `src/novacontrol/api/app.py`, `src/novacontrol/api/models.py`, `src/novacontrol/api/route_consumers.py`, `docs/API.md`.
- UI: `web/src/App.tsx`, `web/src/nav.ts`, `web/src/pages/Autonomy.tsx`, `web/src/pages/Dashboard.tsx`.

### Database changes

Schema v1, new tables only:

- `autonomy_config` (one validated config, version);
- `autonomy_runtime` (mode/state/lease/heartbeat/last errors);
- `autonomy_cycles` (durable cycle intent with unique `cycle_key`, even if no scan yet).

No existing table is altered. The DDL is idempotent and versioned through `AutonomyStore`.

### API changes

- `GET /market/autonomy/contracts`
- `GET /market/autonomy/status`
- `GET /market/autonomy/health`
- `POST /market/autonomy/start`
- `POST /market/autonomy/pause`
- `POST /market/autonomy/resume`
- `POST /market/autonomy/stop`

Configuration read/write (`GET`/`PUT /market/autonomy/config`) is deferred with AT-02. AT-01 may persist `autonomous` as a **state-only shell**; the `409 autonomy_not_released` gate applies to future order/scan work, not to the mode shell.

### UI changes

- New Autonomy nav entry and page shell.
- Mode/state/lease/health card.
- Start Monitoring, Pause, Resume and Stop controls, with confirmations.
- Dashboard autonomy status card.
- No order or decision tables yet.

### Dependencies

None beyond the accepted baseline. This milestone must be merged before any scan work.

### Failure scenarios

- duplicate start requests;
- service disabled/shutting down;
- invalid config;
- two processes attempting to acquire a lease;
- stale lease after a crash;
- paper-only flag assertion failure;
- database write failure;
- UI double-click.

### Tests

**Unit**

- mode transition table and invalid transitions;
- config validation (blank portfolios, invalid fractions, unknown fields, version conflict);
- lease acquire/renew/release/expiry;
- runtime status serialization;
- paper-only assertion.

**Integration**

- service starts with autonomy disabled;
- `start(monitoring)` sets mode and starts no order;
- `pause`/`resume`/`stop` persist across a service reconstruction;
- API auth, origin, 409/422 contracts;
- no call to `ExecutionEngine.submit` in this milestone (spy/mock assertion).

**E2E**

- open the Autonomy page, start monitoring, observe `running/monitoring`, pause, resume, stop, reload and verify the persisted state;
- Dashboard card reflects the same mode.

### Acceptance criteria

- One and only one autonomy runtime exists per service instance.
- Every mode change is persisted before the response.
- No order, signal or proposal is created.
- Existing test suite and `docs/API.md --check` pass.
- A forced restart shows `stopped/starting/recovering` honestly; it never claims `running` without a lease/tick.
- The UI cannot select a live mode.

### Estimated complexity

**High.** New durable state, lifecycle integration, API and UI, but no financial path.

---

## 4. AT-02 — Session-Aware Monitoring and Discovery

### Objective

Make autonomy scan on the verified Indian exchange calendar and persist each scan, while remaining order-free. Establish the scheduler contract and the “no scan means no decision” behavior.

### Existing components to reuse

- `marketdata/session_schedule.plan_market_refresh`.
- `marketdata/calendar.ExchangeCalendar`/`calendar_by_mic`.
- `AssetDiscoveryEngine.run_cycle`, `candidates`, `_cycle_lock`.
- `StrategyEngine._instruments` (holdings + candidates).
- `AutonomyRuntime.tick` and `AutonomyStore`.

### New components

- `autonomy/scheduler.py` (`CyclePlan`, `SessionGate`, due-scope calculation).
- `AutonomyOrchestrator._scan` skeleton.
- Cycle listing/detail endpoints.
- Scan tab UI.

### Files/modules likely affected

- New: `src/novacontrol/novamarket/autonomy/scheduler.py`.
- Modified: `service.py` (tick calls scheduler), `discovery/sessions.py` (delegate XNSE/XBOM to verified calendar, or leave and gate before calling), `api/app.py`, `api/models.py`, `route_consumers.py`, `docs/API.md`.
- UI: `Autonomy.tsx` Scans tab; `Dashboard.tsx` last scan.

### Database changes

Schema v1 already contains `autonomy_cycles`; populate it. Add no columns unless the acceptance needs a `skip_reason` not already in `status`/`summary`; prefer storing the reason in `summary`/`error` rather than a migration.

### API changes

- `POST /market/autonomy/scan`
- `GET /market/autonomy/cycles`
- `GET /market/autonomy/cycles/{cycle_id}`
- `GET /market/autonomy/health` (data/calendar/lease detail)
- Status gains `last_successful_scan_at`, `next_due_at`, cycle counts.

### UI changes

- Scans tab: cycle ID, time, horizon, status, session, data state, scanned/signals/errors.
- Health card for calendar coverage/session and data freshness.
- Scan Now button is available only in monitoring and only with `submit_orders=false`.

### Dependencies

AT-01.

### Failure scenarios

- weekend/holiday/NSE early close;
- calendar year outside coverage (must be monitoring-only);
- stale or missing bars;
- provider error;
- discovery already running;
- duplicate cycle key;
- process restart during a scan;
- DB lock.

### Tests

**Unit**

- `SessionGate` for open/closed/weekend/holiday/unknown coverage;
- due-scope cadence per horizon;
- cycle key determinism;
- calendar `is_open`/`next_session_open` integration;
- `scan` with `force` cannot bypass session/coverage.

**Integration**

- seeded data, scheduled scan produces one cycle and discovery candidates;
- weekend/holiday produces `skipped` and zero candidates/orders;
- two concurrent scan requests produce one cycle and one skip;
- restart leaves a `running` cycle as `interrupted`, not `completed`.

**E2E**

- run a scheduled scan in a session fixture; verify the Scans tab and last scan;
- force a weekend fixture; verify `skipped_session`/holiday reason and no order;
- force stale data; verify `skipped`/degraded, no order.

### Acceptance criteria

- Scheduled scans occur only when the verified calendar says open.
- Outside 2026 coverage, autonomy never submits an order; the UI says coverage is unverified.
- No duplicates from repeated clicks or overlapping ticks.
- No signal/proposal/order exists solely because a scan was forced.

### Estimated complexity

**Medium.** Calendar/session semantics and one new scheduler, but existing discovery does the heavy work.

---

## 5. AT-03 — Decision and Risk Pipeline (Monitoring)

### Objective

Run the existing strategy engine in monitoring mode, persist autonomy decisions and risk outcomes, and display the full decision/risk chain. No proposal is forwarded and no order is created.

### Existing components to reuse

- `StrategyEngine.run()`, `evaluate()`, `propose()`, `_resolve_conflicts()`.
- `risk/engine.py` `assess()` and risk decision store.
- `portfolio/models.HORIZON_POLICIES` for max positions/cash preflight.
- Existing strategy and risk pages/components.

### New components

- `autonomy/decisions.py` (or orchestrator decision mapping).
- `autonomy_decisions` schema v2.
- Decision API and UI.

### Files/modules likely affected

- New: `src/novacontrol/novamarket/autonomy/orchestrator.py` (the decision phase), `autonomy/decisions.py` if separate.
- Modified: `service.py` orchestration entry; `api/app.py`, `api/models.py`, `route_consumers.py`, `docs/API.md`.
- UI: `Autonomy.tsx` Decisions tab; links to Strategies/Risk.

### Database changes

Schema v2 adds `autonomy_decisions` with `cycle_id`, strategy/instrument, action, status, signal/proposal/risk/order IDs, reason and timestamps. A unique key prevents duplicate observations for the same cycle/signal/action.

### API changes

- `GET /market/autonomy/decisions`
- `GET /market/autonomy/decisions/{decision_id}`
- Status cycle counts include decisions/risk results.

### UI changes

- Decisions table: time, instrument, strategy, action, status, reason, risk outcome; row details link to the proposal/risk decision.
- Clear “Monitoring — no order submitted” banner.
- Risk approved/rejected/restricted counters.

### Dependencies

AT-02.

### Failure scenarios

- risk service unavailable;
- risk `requires_review`/`insufficient_data`;
- strategy evaluation exception for one instrument;
- conflict review blocks a proposal;
- max-position guard;
- 20-instrument cap;
- proposal expired before display;
- duplicate signal on the same data cutoff.

### Tests

**Unit**

- signal → `open/add/reduce/close/hold` mapping;
- approved/rejected/restricted/pending status mapping;
- max-position and notional pre-filters;
- conflict policy `review` produces no proposal;
- HOLD never creates a proposal.

**Integration**

- seeded instrument with enough bars → signal → proposal → NM-08 decision;
- risk denial is recorded with the reason and no order;
- duplicate strategy run on the same cutoff reuses the signal/proposal;
- 21 candidates only evaluates the configured batch.

**E2E**

- run monitoring scan, open decisions, inspect approved/rejected rows; verify zero paper orders for the session.

### Acceptance criteria

- Every actionable signal has a durable decision row.
- Every decision links to a signal and, where created, a proposal and risk decision.
- Risk rejection is visible and reasoned.
- No decision reaches `submitted`; the execution order count remains zero.
- Existing strategy/risk test suites pass unchanged.

### Estimated complexity

**Medium.** Most logic exists; the work is the composition, durable read model and safety gate.

---

## 6. AT-04 — Autonomous Paper Execution

### Objective

Forward approved proposals through the existing NM-10 engine, track order/fill state, process open autonomy orders, and prove idempotency and fail-closed behavior. The operator-facing autonomous mode remains gated until AT-06.

### Existing components to reuse

- `StrategyEngine.forward()` and `NovaMarketService._forward_strategy_to_execution()`.
- `ExecutionEngine.submit()`, `_validate()`, `_revalidate_risk()`, `process()`, `cancel()`, `recover_pending()`.
- `ExecutionStore` unique proposal ID, fills, reservations and reconciliation.
- `PortfolioEngine`/`WalletEngine` for the resulting position and ledger.

### New components

- `AutonomyOrchestrator._execute()` and `_process_open_orders()`.
- Execution decision states and trace links.
- Optional actor plumb-through.

### Files/modules likely affected

- `src/novacontrol/novamarket/autonomy/orchestrator.py`.
- `src/novacontrol/novamarket/service.py` (optional `actor` on forward).
- `src/novacontrol/novamarket/autonomy/models.py`/`store.py` for decision order/fill fields.
- `api/app.py` not necessarily new; the existing execution reads serve the order data.
- UI: `Autonomy.tsx` Orders/Fills tab and links to Paper.

### Database changes

No new table; `autonomy_decisions` already has order/fill IDs. If the v2 table omitted `fill_ids`, add a JSON `fill_ids` column in schema v2.1; prefer storing a JSON array as text.

### API changes

No order-creation endpoint. Status/decisions expose order/fill links. The existing `/market/execution/orders`, `/fills`, `/reconciliations` remain authoritative.

### UI changes

- Orders/Fills tab shows decision-linked orders.
- “Open in Paper” links.
- Banner: autonomous orders are paper-only and appear in the existing Paper page.
- No duplicate order controls on the autonomy page.

### Dependencies

AT-03. Requires a **test-only release gate** to exercise forward before the operator gate opens. The gate must not be settable through the public API or a stored user preference; it is an internal constant/tests hook, removed at AT-06 acceptance.

### Failure scenarios

- duplicate submit/replay;
- proposal expired between approval and forward;
- risk decision expired or state version changed;
- execution unavailable;
- insufficient cash/position;
- stale reference price;
- partial fill;
- rejected/failed order;
- open limit/stop order at shutdown;
- restart with a pending fill.

### Tests

**Unit**

- approved-only forwarding;
- no forward for restricted/pending/expired/lower-status proposals;
- actor prefix and open-order filter;
- bounded per-cycle submissions;
- no retry of terminal order.

**Integration**

- temp DB: approved proposal → order → fill → portfolio position and wallet ledger entry;
- duplicate submit returns the same order and does not add a fill;
- insufficient cash/position rejected without an order;
- partial fill remains open and is processed safely;
- execution failure leaves a failed order and no retry;
- restart calls `recover_pending` and reconciles.

**E2E**

- test harness starts an internal autonomous cycle, observes one order and one fill, then verifies the Paper page and portfolio;
- second identical cycle produces no second order.

### Acceptance criteria

- Every autonomous order has proposal, risk decision and actor provenance.
- No order exists without an approved/unexpired risk decision.
- Duplicate submission is a replay, not a second order.
- A failed execution is terminal for that proposal; no blind retry.
- Manual paper orders are untouched.
- No `PortfolioEngine.apply` call is reachable from autonomy.

### Estimated complexity

**High.** The composition is small, but the correctness proof is mostly idempotency, cross-store consistency and failure handling.

---

## 7. AT-05 — Position Supervision and Exits

### Objective

Monitor open positions and create stop-loss, target, trailing, time, strategy-confirmed, and optional portfolio-risk exits through the same NM-08/NM-10 path. Define the paused safety policy and record learning evidence.

### Existing components to reuse

- `PortfolioEngine.detail()`, positions, average cost/opened_at, valuation and reconciliation.
- `portfolio/horizons.py` holding periods/stop profiles.
- `StrategyEngine` proposal/risk/store.
- `RiskEngine` and `ExecutionEngine`.
- `LearningService.record_evidence()` and `monitor()`.

### New components

- `autonomy/exit_supervisor.py`.
- `StrategyEngine.propose_autonomous_exit()` and shared `_propose_with_risk()`.
- `autonomy_exit_state` schema v3.
- Exit API/UI and paused policy.
- Learning evidence hook.

### Files/modules likely affected

- New: `src/novacontrol/novamarket/autonomy/exit_supervisor.py`.
- Modified: `src/novacontrol/novamarket/strategies/engine.py` (proposal seam only), `autonomy/store.py`, `api/app.py`, `api/models.py`, `route_consumers.py`, `docs/API.md`.
- UI: `Autonomy.tsx` Exits/Positions tabs; Strategy performance links.

### Database changes

Schema v3 adds `autonomy_exit_state` (portfolio, instrument, high-water mark, last price/date, last exit attempt/kind/decision). No existing table changes.

### API changes

- `GET /market/autonomy/positions`
- Config `exit_policies` and `paused_exit_policy`.
- Decision/status expose exit counts and links.
- No new order endpoint.

### UI changes

- Positions tab with cost, price, unrealized P&L, stop, target, trailing high-water mark, last exit decision.
- Exits tab with trigger reason, risk decision and order link.
- Paused banner explains protective vs monitor-only behavior.
- Strategy performance and learning alerts links.

### Dependencies

AT-04. The operator gate remains closed until AT-06.

### Failure scenarios

- stale/missing exit price;
- high-water mark not persisted;
- duplicate exit signal while an exit order is open;
- partial fill/remaining position;
- risk rejects exit (must be visible; no bypass);
- position already closed;
- time exit at session boundary;
- paused policy changes mid-run;
- learning evidence duplicate.

### Tests

**Unit**

- stop/target/trailing/time/strategy/portfolio-risk predicates;
- high-water mark monotonicity;
- fresh-price refusal;
- duplicate exit suppression;
- full vs partial quantity/notional;
- paused protective vs monitor-only.

**Integration**

- buy fill → price drop → stop proposal → risk → exit order → position reduced/closed;
- price rise → target; trailing high-water update; time exit with fake clock;
- risk rejection of an exit remains visible and no order is created;
- duplicate cycles do not create two exit orders;
- learning `paper` evidence is recorded for a terminal trade and duplicate payload is idempotent.

**E2E**

- three horizon fixtures: short/day, medium/swing, long-term; each reaches an exit trigger and a paper close;
- paused with protective exits closes a position; paused with monitor-only does not;
- P&L and realized values match existing portfolio methods.

### Acceptance criteria

- All six exit categories are represented, with the optional portfolio-risk exit explicitly enabled.
- No exit uses stale data or direct ledger/position mutation.
- No duplicate exit order for one position/trigger.
- `paused` blocks new entries and follows the configured exit policy.
- Learning evidence is recorded only after a terminal fill, never from an unexecuted signal.
- Existing risk/execution tests pass unchanged.

### Estimated complexity

**High.** Exit semantics, high-water state and the proposal seam are subtle; the financial path itself is reused.

---

## 8. AT-06 — Recovery, Observability and Full Acceptance

### Objective

Make the lifecycle survivable and operable: restart recovery, lease recovery, circuit breakers, kill switch, complete observability, full UI and the 20-scenario acceptance suite. Open the operator autonomous gate only after all acceptance gates pass.

### Existing components to reuse

- `ExecutionEngine.recover_pending()`, `reconcile()`.
- `PortfolioEngine.reconcile()`.
- `EventBus`, activity/audit, existing Health/Activity pages.
- Existing QA harness, browser and performance patterns.

### New components

- `autonomy/recovery.py` or runtime recovery methods.
- Health/breaker/kill endpoints.
- Full Autonomy page tabs.
- Release gate check and acceptance script.

### Files/modules likely affected

- `autonomy/runtime.py`, `store.py`, `orchestrator.py`.
- `service.py` shutdown/recovery.
- `api/app.py`, `api/models.py`, `route_consumers.py`, `docs/API.md`.
- `web/src/pages/Autonomy.tsx`, `Dashboard.tsx`, `Trading.tsx`, `System.tsx`.
- Tests under `tests/test_novamarket_autonomy_*.py`.
- New docs/acceptance evidence under `novamarket/docs/` (not source).

### Database changes

No new tables. Recovery uses existing `autonomy_runtime`, `autonomy_cycles`, `autonomy_decisions`, `autonomy_exit_state`. A schema-version check refuses to run on an unknown newer version.

### API changes

- `POST /market/autonomy/kill`
- `GET /market/autonomy/activity`
- Full health/breaker fields.
- `start(autonomous)` release gate opens only after all acceptance checks pass.

### UI changes

- Kill switch with exact confirmation.
- Health/error/breaker panel.
- Activity/event links.
- Recovery banner and “resume requires review” state.
- Complete decision/order/position/exit trace.
- No redesign of the existing dashboard.

### Dependencies

AT-01 through AT-05.

### Failure scenarios

- process kill at each phase (discovery, risk, submit, fill, exit);
- stale lease owner;
- two processes with the same config;
- DB lock/contention;
- risk/execution/provider outage;
- kill during an in-flight submit;
- restart with an open order;
- calendar coverage rollover;
- duplicate UI actions;
- long-running memory/performance drift.

### Tests

**Unit**

- lease expiry/recovery decisions;
- breaker thresholds/reset;
- kill gate;
- recovery state transitions;
- status honest reporting.

**Integration**

- kill -9-style subprocess termination then restart; interrupted cycle marked; pending fill recovered; no duplicate order;
- stale lease cannot be stolen while valid;
- kill cancels only autonomy actor orders and preserves fills;
- one clean monitoring cycle before autonomous resume;
- all failure injections.

**E2E**

- all 20 scenarios in `AUTONOMOUS_TRADING_TEST_STRATEGY.md`;
- three horizons;
- UI controls and reload persistence;
- no live/broker route;
- performance/soak.

### Acceptance criteria

- All 20 lifecycle scenarios pass on a temporary database with no external provider.
- Kill switch stops submissions immediately and is auditable.
- Restart never auto-submits before recovery and a clean cycle.
- No duplicate order/fill/position/ledger under retries or crash.
- Full UI reflects server state after reload; no stale DOM claim.
- `docs/API.md --check`, mypy views, focused test suite and web build pass.
- Paper-only flags remain true/false/absent exactly as defined.

### Estimated complexity

**High.** The reliability proof is the main work.

---

## 9. Dependency graph and review gates

```text
AT-01 ──> AT-02 ──> AT-03 ──> AT-04 ──> AT-05 ──> AT-06
  │          │          │          │          │          │
  └ safety   └ calendar └ risk     └ idempot. └ exits    └ recovery
    shell      gate       evidence   evidence   evidence   + release
```

- AT-01 review: no financial path.
- AT-02 review: no order path; calendar proof.
- AT-03 review: no forward; risk provenance.
- AT-04 review: idempotency and cross-store proof; no operator gate.
- AT-05 review: exit safety; paused policy; learning non-promotion.
- AT-06 review: full lifecycle, recovery and kill; then release.

---

## 10. Out of scope for this roadmap

- live trading, broker connectivity or real-money execution;
- target-weight autonomous rebalancing through `PortfolioEngine.apply`;
- new risk policy values or a second risk engine;
- automatic learning approval/promotion;
- a new frontend framework, chart library or state manager;
- a new message queue, Redis, Celery or external scheduler;
- NovaControl core or NovaForge changes;
- changing NM-10 fees, slippage, ledger or valuation rules.

The roadmap is the minimum sequence that reaches a safe, testable autonomous paper lifecycle. It is a proposal for review, not an approved phase.
