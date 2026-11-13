# Autonomous Paper Trading — Gap Matrix

**Status:** Design review, reconciled 10 November 2026 with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md`. The AT-01 runtime/safety-shell gaps are closed; every other gap remains open and AT-02–AT-06 are not approved or started.
**Date:** 9 November 2026
**Purpose:** Enumerate every gap between the accepted NM-01–NM-14 baseline and a safe autonomous paper-trading lifecycle, with evidence, severity, resolution and dependent milestone.

---

## 1. Severity definitions

| Severity | Meaning |
|---|---|
| **Critical** | No autonomous lifecycle, or a path that could submit an unsafe/duplicate order or bypass risk/accounting |
| **High** | Reliability, control, session/data safety or a required lifecycle capability is missing |
| **Medium** | Observability, operability, testability or a bounded limitation |
| **Low** | Documentation/repository evidence hygiene; no runtime effect |

---

## 2. Existing capabilities that are not gaps

These are working, source-verified assets and must be reused rather than rebuilt:

| Capability | Evidence |
|---|---|
| Market-data ingestion, bars, quotes, freshness | `marketdata/engine.py`, `marketdata/freshness.py`, `marketdata/store.py` |
| Verified NSE/BSE session and 2026 holiday calendar | `marketdata/calendar.py`, `marketdata/session_schedule.py` |
| Discovery and candidate scoring | `discovery/engine.py`, `discovery/scoring.py` |
| Six strategy families and signal generation | `strategies/catalog.py`, `strategies/engine.py` |
| Conflicts, proposals and risk requests | `strategies/engine.py` `run`/`propose`/`_resolve_conflicts` |
| Independent risk policies and decisions | `risk/engine.py`, `risk/policies.py`, `risk/store.py` |
| Paper order validation and risk revalidation | `execution/engine.py` `submit`/`_validate`/`_revalidate_risk` |
| Order/fill/reservation persistence and reconciliation | `execution/store.py`, `execution/reconciliation.py` |
| Portfolio, position and valuation accounting | `portfolio/engine.py`, `portfolio/models.py` |
| Wallet and ledger | `wallet/engine.py`, `wallet/store.py` |
| Backtesting, shadow and learning governance | `backtesting/`, `shadow/`, `learning/service.py` |
| API auth, origin checks, route registry | `api/app.py`, `api/models.py`, `api/route_consumers.py` |
| React workspace and UI primitives | `web/src/App.tsx`, `web/src/nav.ts`, `web/src/pages/` |

The gap is orchestration and lifecycle, not missing financial primitives.

---

## 3. Gap register

| ID | Gap | Evidence | Severity | Resolution | Milestone |
|---|---|---|---|---|---|
| G-01 | No autonomous orchestrator that composes discovery → decision → risk → execution → monitoring → exit | `AUTONOMOUS_PAPER_TRADING_AUDIT.md` verdict NO; no autonomy package; `StrategyEngine.forward` is manual | Critical | Add `AutonomyOrchestrator` over existing services; never a parallel engine | AT-01/04 |
| G-02 | No durable autonomy mode, runtime state, cycle or lease | `NovaMarketService.runtime_status()` only reports housekeeping; no autonomy tables | Critical | `autonomy_runtime`, `autonomy_cycles`, `AutonomyRuntime`, lease | AT-01 |
| G-03 | No trading start/pause/resume/stop/kill contract | `NovaMarketService.enable/disable` controls the subsystem, not trading; no autonomy routes | High | Additive `/market/autonomy/*` control API and state machine | AT-01/06 |
| G-04 | No last successful scan/execution, next due or autonomy health | `runtime_status()` has last tick/event/position refresh only | High | Persist and expose autonomy status/health | AT-01/02 |
| G-05 | The service runtime loop is housekeeping only | `_runtime_tick()` ticks notifications/radar/held bars; no discovery, strategy or order call | Critical | Call `AutonomyRuntime.tick()` from the one loop; add orchestrator phases | AT-02/04 |
| G-06 | Discovery's scheduled-session check is weekday-only and holiday-unaware | `discovery/sessions.py` explicitly claims no holiday calendar; `session_state()` uses weekdays | High | Gate autonomy with `marketdata.calendar`; migrate or bypass the discovery session check for scheduled cycles | AT-02 |
| G-07 | Verified calendar coverage is 2026 only | `marketdata/calendar.py` supports 2026 NSE/BSE holidays; outside coverage is unverified | High | Fail closed for new entries outside verified coverage; monitoring only until coverage is installed | AT-02 |
| G-08 | No per-horizon cadence or bounded scan-batch strategy | `_run_runtime` uses one refresh interval; `StrategyEngine._instruments()` caps at 20 and prefers holdings | High | `AutonomyScheduler` per horizon; bounded batch rotation; configurable cap after a small non-financial extension | AT-02 |
| G-09 | Signals never become orders automatically | `StrategyEngine.run` creates proposals; `forward` is a separate manual call | Critical | Forward only approved proposals in autonomous mode | AT-04 |
| G-10 | No autonomous open-order processing | `ExecutionEngine.process()` is manual/API-driven; runtime does not process open orders | High | Process only `actor=autonomy:*` open/partial orders each due cycle | AT-04 |
| G-11 | No protective exit supervisor | Strategies may emit EXIT, but there is no stop/target/trailing/time supervisor | Critical | `ExitSupervisor` plus `propose_autonomous_exit` through NM-08/NM-10 | AT-05 |
| G-12 | No persisted high-water mark or duplicate-exit state | No autonomy exit table; positions do not store trailing high-water state | High | `autonomy_exit_state`; one active exit intent per position | AT-05 |
| G-13 | Portfolio `max_positions` is not enforced by the risk engine or execution path | `HORIZON_POLICIES` has `max_positions`; risk checks per-position weight, not position count; `PortfolioEngine` applies it in its own selection | High | Autonomy preflight uses the horizon `max_positions` as a new-entry guard; NM-08 remains authoritative for risk values | AT-03 |
| G-14 | No autonomous target-weight rebalance path that respects NM-10 | `PortfolioEngine.propose()/apply()` moves positions/ledger directly; no order gateway | High | Do not use `apply()`; v1 uses existing strategy sizing + risk. Target-weight autonomy is a separate future design | Scope boundary |
| G-15 | No proposal seam for a non-strategy protective exit | `StrategyEngine.propose()` expects a stored signal and a strategy record; direct execution would bypass risk | High | Add `StrategyEngine.propose_autonomous_exit()` sharing `_propose_with_risk()`; never call execution directly | AT-05 |
| G-16 | No paper-trade evidence hook into learning | `LearningService.record_evidence()` exists, but no autonomous caller records terminal paper fills | Medium | Record `kind="paper"` evidence after terminal fills; no auto-approval/promotion | AT-05 |
| G-17 | No autonomy restart/recovery sequence | `ExecutionEngine.recover_pending()` and `reconcile()` exist, but nothing marks autonomy cycles interrupted or reconstructs exit state | Critical | Startup recovery: pending fills, reconciliation, interrupted cycles, no orders before a clean cycle | AT-06 |
| G-18 | No cross-process lease for cycles | Existing scheduler locks are in-process; a second process could run independently | Critical | Conditional SQLite lease with expiry and owner, plus per-cycle unique key | AT-01 |
| G-19 | No kill switch or autonomy circuit breakers | `NovaMarketConfig.paused` pauses housekeeping; no kill/breaker semantics | Critical | Add kill endpoint/state, data/risk/execution breakers, no-order gates | AT-06 |
| G-20 | No autonomy API registration or docs | No `/market/autonomy/*` routes in `ApiSurface`, `ROUTE_CONSUMERS` or `docs/API.md` | High | Add handlers + surface + consumers; regenerate API docs | AT-01 |
| G-21 | No autonomy UI surface or trace | Existing pages show existing orders/proposals but no mode/lease/cycle/decision/exit page | High | New `AutonomyPage`, Dashboard card, Paper/Health/Activity links; no redesign | AT-01/06 |
| G-22 | No autonomy event vocabulary/durable activity | Existing typed events cover strategy/risk/execution, not autonomy runtime/cycle/exit | Medium | Add autonomy events and/or durable cycle/decision activity; keep the one event bus | AT-01/06 |
| G-23 | No autonomy lifecycle tests, especially three horizons and crash recovery | No autonomy test modules; current baseline has no autonomous loop to test | High | Add unit/integration/E2E/fault/soak matrix (20 scenarios) | AT-06 |
| G-24 | No `GIT_BASELINE_EVIDENCE.md` | File is absent; working tree has uncommitted QA changes; latest recorded commit `afa5ff64...` | Low | Treat the working tree + `NM14_FINAL_ACCEPTANCE_REPORT.md` as evidence; add a future baseline manifest, not a runtime fix | Documentation |
| G-25 | Requested `NM14_FINAL_ACCEPTANCE_REPORT(4).md` is absent | Actual authoritative file is `docs/NM14_FINAL_ACCEPTANCE_REPORT.md` | Low | Use the actual file; note the filename mismatch in the review | Documentation |
| G-26 | Cross-store execution/portfolio/ledger sequence has known recovery complexity | `ExecutionEngine` fill journal/compensation exists; the book is not one transaction | High | Reuse `recover_pending()`/`reconcile()`; no new cross-store transaction; autonomy never writes these stores | AT-04/06 |
| G-27 | Market data is delayed/EOD, not exchange tick data | `session_schedule` explicitly says 15-minute refresh cadence, not tick data | High | Disclose the limitation; 15-minute floor; no intraday tick claims; paper fills use stored references | Design constraint |
| G-28 | Autonomy order provenance is not filterable through the existing execution API | `Order.actor` exists; `/market/execution/orders` filters status/portfolio but not actor | Medium | Store order IDs on autonomy decisions and link to `/orders/{id}`; do not add a second order API | AT-04 |
| G-29 | The one runtime interval is the floor for scans | `plan_market_refresh()` defaults to 15-minute live/probe intervals | Medium | Set autonomy cadence at/above the floor; document that a 5-minute scan is not claimed | AT-02 |
| G-30 | No retention/limit policy for autonomy cycles/decisions | New tables would grow indefinitely | Medium | Bounded list limits; retention/compaction policy or documented cap | AT-06 |
| G-31 | No start-up paper-only assertion in the autonomy layer | Execution reports the flags, but no autonomy preflight checks them | Medium | Refuse start if `paper_only/live_trading/broker_connected` are not the accepted values | AT-01 |
| G-32 | Public forward wrapper drops actor provenance | `NovaMarketService.forward_strategy_proposal()` calls `forward(proposal_id)` without actor | Low | Add optional `actor` parameter; existing API keeps working | AT-04 |

---

## 4. Gaps by milestone

| Milestone | Gap IDs |
|---|---|
| AT-01 | G-02, G-03 (control shell), G-04 (status), G-18, G-20, G-21 (shell), G-22 (runtime events), G-31 |
| AT-02 | G-05 (scan), G-06, G-07, G-08, G-20 (scan/health routes), G-21 (scan UI), G-29 |
| AT-03 | G-13, G-21 (decision UI), part of G-04 |
| AT-04 | G-01 (execution phase), G-09, G-10, G-26 (recovery hooks), G-28, G-32 |
| AT-05 | G-11, G-12, G-14 (boundary), G-15, G-16, G-21 (exit UI) |
| AT-06 | G-01 (full acceptance), G-17, G-19, G-21 (health/kill), G-22 (activity), G-23, G-30 |
| Documentation | G-24, G-25 |

`G-14` and `G-27` are **accepted scope boundaries**, not defects to hide. They must remain visible in the UI/limitations and must not be “fixed” by using the direct portfolio-apply path or by claiming tick data.

---

## 5. What already works versus what is new

### 5.1 Already works

- manual proposal → risk → paper order → fill → ledger/position;
- risk rejection/restriction and decision persistence;
- execution idempotency by proposal;
- partial fills, cancellation, expiry, reconciliation and pending-fill recovery;
- portfolio/valuation/P&L reads;
- discovery and strategy evaluation;
- learning evidence and governance;
- 25-route React workspace, API auth and contract tests.

### 5.2 New

- runtime/mode/lease/cycles/decisions;
- session-aware autonomy scheduler;
- automation of the approved-proposal forward path;
- open autonomy-order processing;
- exit supervisor and high-water state;
- restart recovery, breakers, kill and autonomy observability;
- autonomy API/UI/tests.

### 5.3 Must be modified, not duplicated

| Existing module | Modification | Why |
|---|---|---|
| `service.py` | tick hook, lazy runtime, status, shutdown, optional forward actor | One lifecycle and one task |
| `strategies/engine.py` | `_propose_with_risk()` + `propose_autonomous_exit()` | Exit can reuse proposal/risk path without a parallel proposal engine |
| `discovery/sessions.py` or caller | holiday-aware session gate | Weekday-only is insufficient for autonomy |
| `api/app.py`, `models.py`, `route_consumers.py` | additive routes and consumer entries | Existing API contract requires all five places |
| React app/pages | additive route/card/links | Preserve workspace architecture |

---

## 6. Residual risk after the roadmap

Even after AT-06, these limitations remain and must be disclosed:

1. **Delayed data.** Scans are bounded by the market-refresh cadence; this is not an exchange tick engine.
2. **2026 calendar coverage.** Autonomy is fail-closed outside the verified year; coverage must be maintained.
3. **20-instrument strategy batch.** The existing cap is respected; full-universe per-tick evaluation is not claimed.
4. **No target-weight autonomous rebalancing.** Strategy sizing and risk are used; `PortfolioEngine.apply` is excluded.
5. **Paper-only.** No broker, live order, real money or external execution exists.
6. **Learning is advisory.** No automatic promotion or risk-policy change.
7. **Operator responsibility.** A kill/stop does not undo fills or guarantee a market price; it only prevents new autonomous actions and optionally cancels open paper orders.
8. **Data/provider dependency.** A provider outage means no order, not a guessed fill.
9. **Uncommitted baseline.** The reviewed working tree is not a clean commit; a future implementation must be based on a recorded baseline.
10. **Cross-store accounting.** Recovery uses the existing journal/reconciliation, not an atomic distributed transaction.

These are not reasons to skip validation; they are the boundaries the acceptance report must state.
