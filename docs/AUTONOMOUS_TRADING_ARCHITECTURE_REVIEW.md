# Autonomous Paper Trading — Architecture Review

**Review date:** 9 November 2026
**Review type:** Source-grounded design review, not an implementation review
**Verdict:** **AT-01 (runtime and safety shell) is implemented and verified.** AT-02–AT-06 are not approved and not started; the product remains non-autonomous and no order path exists. Where this review conflicts with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md` (owner-approved 10 November 2026), the owner decisions win.

---

## 1. Executive findings

1. NovaMarket has a strong, working **manual** paper-trading chain: strategy → proposal → independent NM-08 risk → NM-10 paper execution → ledger/position → backtest/shadow/learning.
2. It does **not** have an autonomous loop. The audit verdict is correct: `AUTONOMOUS_PAPER_TRADING_AUDIT.md` says no orchestrator exists, and no source contradicts that.
3. The missing work is orchestration, lifecycle, scheduling, exit supervision, recovery and observability — not new financial mathematics.
4. The accepted baseline is suitable to build on. Reusing it avoids a second risk/execution/ledger and preserves the paper-only guarantee.
5. A safe autonomous lifecycle is feasible with a new `autonomy` package, a small strategy-engine proposal seam, additive API/UI, and no change to the accepted financial engines.
6. The largest unresolved product boundary is **target-weight autonomous rebalancing**. `PortfolioEngine.apply()` bypasses NM-10, so v1 must not use it. The proposed lifecycle is signal-driven, not target-weight-driven.
7. The largest operational boundary is **calendar/data coverage**. The verified holiday calendar covers 2026; outside it, autonomy must fail closed. The discovery session helper is weekday-only and cannot be the autonomy gate.
8. The largest implementation risk is **idempotency and recovery across existing stores**. The existing execution journal and reconciliation are the right foundation; a new distributed transaction would be the wrong answer.
9. The largest UI risk is a false “autonomous” claim. The UI must distinguish monitoring from autonomous, show the lease/health, and never infer an order or P&L locally.
10. No source, schema, migration, runtime setting or commit was changed by this design task.

---

## 2. What already works

Verified against source and the 9 November acceptance report:

- market data, instruments, bars, freshness and official EOD snapshots;
- discovery, candidate scoring and watchlists;
- six strategy families and deterministic signals;
- conflict handling, proposals and risk requests;
- versioned NM-08 risk policies and persisted decisions;
- NM-10 order validation, risk revalidation, fees, slippage, partial fills and reservations;
- wallet/ledger accounting and portfolio/position valuation;
- backtesting, shadow mode and learning governance;
- 25-route React workspace, API auth, origin checks and route contract tests;
- three preserved QA fixes.

The baseline acceptance report is **PASS for internal acceptance scope**, not an external certification. It explicitly retains limitations: delayed/EOD data, 2026 holiday coverage, BSE bars missing for some symbols, model-derived option IV, and no broker/live path.

---

## 3. What is missing

| Missing capability | Why it matters | Where addressed |
|---|---|---|
| Autonomy runtime/modes/lease | No safe start/stop or duplicate prevention | AT-01 |
| Trading scan cadence | The current task is housekeeping | AT-02 |
| Automatic forward | Signals never become orders without a human | AT-04 |
| Open-order processing | Manual `process` is not a lifecycle | AT-04 |
| Exit supervisor | No stop/target/trailing/time protection | AT-05 |
| Exit high-water state | Trailing stops cannot be correct without it | AT-05 |
| Restart recovery | A crash must not duplicate or resume blindly | AT-06 |
| Kill/breakers | An operator needs an emergency stop | AT-06 |
| Autonomy API/UI/trace | Operators cannot see/control the lifecycle | AT-01/06 |
| End-to-end proof | No autonomous cycle has ever run | AT-06 |
| Max-position preflight | Portfolio policy count is not enforced by risk | AT-03 |
| Paper evidence hook | Completed trades should inform learning | AT-05 |

---

## 4. Architecture assessment

### 4.1 Strengths

- **One financial truth per domain.** Risk, execution, portfolio, wallet and learning each have one owner.
- **Strong existing failure semantics.** `ExecutionEngine` already refuses stale data, unapproved proposals, expired decisions, insufficient cash/position and unenforceable restrictions.
- **Idempotency is already designed.** Proposal ID + execution order uniqueness is the right foundation.
- **Recovery primitives exist.** `recover_pending()` and reconciliation cover the hard cross-store case.
- **UI foundation is reusable.** The workspace already has the needed cards, tables, status badges and auth.

### 4.2 Weak points

- **No durable autonomy state.** Mode in `NovaMarketConfig.paused` is not a trading lifecycle.
- **Two time concepts are conflated today.** Service runtime, discovery scheduler and market refresh cadence are separate; the design must not add a third independent timer.
- **Holiday knowledge is split.** The verified calendar is newer than the discovery session table.
- **The 20-instrument cap** is real; autonomy must batch/rotate, not claim a full scan.
- **The manual portfolio apply path** is financially convenient but execution-unsafe; the design correctly excludes it.
- **No autonomy-specific tests** exist because there is no autonomy.

### 4.3 Feasibility

The design is feasible without new infrastructure:

- one existing asyncio runtime task;
- one new SQLite store in the existing data directory;
- existing FastAPI and React;
- no Redis, Celery, broker SDK or external scheduler;
- no changes to financial calculations or policies.

The main engineering effort is correctness at the boundaries, not new financial logic.

---

## 5. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Duplicate order after retry/restart | Medium | Critical | Proposal/order uniqueness; cycle key; no blind submit retry; recovery by order lookup |
| Stale price used for entry/exit | Medium | Critical | Session/data gates; NM-08/NM-10 freshness; no fallback price |
| Holiday/weekend order | Medium | High | Verified calendar gate; fail closed outside coverage |
| 20-instrument cap hides opportunities | High | Medium | Bounded rotation; document; optional non-financial cap extension |
| Calendar coverage rolls past 2026 | High (time) | High | Coverage is explicit and never extrapolated (OD-04); extend verified coverage before use beyond 2026, fail closed when unverified |
| `PortfolioEngine.apply` accidentally called | Low | Critical | Static test and code review; no import/call in autonomy |
| Cross-store failure leaves inconsistent book | Medium | High | Existing fill journal/compensation/reconciliation; no new transaction |
| Kill switch not effective in time | Low | Critical | Submission gate immediately before forward; lease revoke; cancellation |
| UI reports autonomous when only monitoring | Medium | High | Server-authoritative mode/health; UI tests; no local state inference |
| Learning silently changes strategy | Low | High | Advisory only; no auto-approval/promotion |
| Max positions exceeded | Medium | High | Horizon `max_positions` pre-filter; NM-08 still authoritative |
| Long cycle blocks event loop | Medium | Medium | `max_cycle_seconds`; bounded batches; no unbounded provider calls |
| Table growth | Medium | Low | Limits/retention policy in AT-06 |
| Baseline is uncommitted | High | Medium | Record a new baseline before implementation; do not mix QA changes |
| Scope creep into live trading | Low | Critical | Hard invariant, no live field/route, static scan |

---

## 6. Dependencies

### Required for AT-01

- accepted NM-01–NM-14 baseline in a reproducible working tree;
- existing API route registration and tests;
- a decision on whether to add the autonomy config to the existing settings UI or keep it page-local (proposed: page-local config API, no `NovaMarketConfig` schema change).

### Required for AT-02

- 2026 NSE/BSE calendar verification;
- a decision on the discovery session helper migration;
- a market-data fixture with bars for all test horizons.

### Required for AT-03/04

- strategy/risk fixtures that can produce approved, rejected, restricted and review outcomes;
- execution fixtures for fill, partial fill, rejection and pending recovery;
- actor plumbing on the forward wrapper.

### Required for AT-05

- `HORIZON_PROFILES` mapping confirmation;
- `PortfolioEngine.detail()` fields for average cost/opened_at (verified available);
- a decision on protected exits while paused;
- optional portfolio-risk exit enablement (default false).

### Required for AT-06

- a subprocess/browser harness for crash and UI tests;
- explicitly verified calendar coverage before any use beyond 2026 (OD-04);
- operator acceptance of kill/restart semantics;
- documentation and API reference regeneration.

---

## 7. Recommended implementation order

1. **AT-01 — Runtime and Safety Shell.** This is the first milestone because every later capability needs a durable mode, lease and status. It should not be combined with scanning or execution.
2. **AT-02 — Session-Aware Monitoring and Discovery.** Prove the calendar/session gate and cycle durability with zero orders.
3. **AT-03 — Decision and Risk Pipeline.** Prove signal/proposal/risk provenance in monitoring.
4. **AT-04 — Paper Execution.** Add the forward path behind an internal release gate; prove idempotency and fills.
5. **AT-05 — Exits.** Add protective exits, paused policy and learning evidence.
6. **AT-06 — Recovery, Observability and Acceptance.** Prove crash/restart, kill, all 20 scenarios, three horizons and the UI; then open the operator autonomous gate.

Do not start with a “quick auto-buy” loop. That would create orders without the lifecycle evidence and make later recovery harder.

---

## 8. Readiness gates

### Ready now

- source-based architecture;
- component mapping;
- API contract;
- state machine;
- test strategy;
- gap/decision/review documents.

### Not ready

- implementation approval;
- autonomous mode exposure;
- verified calendar coverage beyond 2026 (OD-04);
- target-weight autonomous rebalancing;
- external security/accessibility certification (the 9 November report explicitly does not claim these).

### Gate to open `autonomous`

All of the following must be true:

- AT-01–AT-06 accepted;
- 20/20 lifecycle scenarios pass;
- three horizons pass entry/exit;
- crash/restart and kill tests pass;
- no duplicate order/fill in fault injection;
- paper-only static and runtime assertions pass;
- UI shows monitoring vs autonomous accurately after reload;
- API docs are in sync and focused test/build gates pass;
- operator accepts paused-protective-exit and target-weight limitations.

---

## 9. Verification performed for this design

This task was documentation-only. The following source checks were performed:

- read the actual `NM14_FINAL_ACCEPTANCE_REPORT.md`, not the missing `(4)` filename;
- confirmed no `GIT_BASELINE_EVIDENCE.md` in `docs/`;
- confirmed current commit and uncommitted working tree;
- inspected `NovaMarketService` lifecycle/runtime/forward methods;
- inspected discovery scheduler and session helper;
- inspected verified calendar/refresh planner;
- inspected strategy evaluate/propose/forward/conflict paths;
- inspected risk assessment and execution revalidation/idempotency/recovery;
- inspected portfolio apply/close/detail and horizon policies;
- inspected learning evidence and monitor methods;
- inspected API route registration and `docs/API.md` check;
- inspected existing React routes/nav/pages;
- confirmed the 15-minute market-refresh floor and 2026 calendar coverage;
- confirmed `HORIZON_POLICIES.max_positions` and risk's per-position weight check.

No application source, database schema, migration or runtime configuration was modified. No commit or push was made. The current QA fixes and existing uncommitted work were left untouched.

---

## 10. Owner decisions (10 November 2026)

1. **Day/swing/long mapping — resolved (OD-01).** `short_term → day_trading`, `medium_term → swing_trading`, `long_term → long_term`.
2. **Paused protective default — resolved (OD-02).** Protective exits stay armed; `paused_exit_policy` is fixed to `protective` and `monitor_only` is not supported.
3. **Target-weight autonomy — resolved (OD-03).** Excluded from v1; the only eventual order path is an approved strategy proposal → NM-08 → NM-10.
4. **Calendar coverage — resolved (OD-04).** Only explicitly verified coverage is used; 2026 is verified, coverage is never extrapolated, and autonomy fails closed outside it.
5. **Operator permissions — resolved (OD-05).** Start/stop controls require a dedicated server-side autonomy-operator credential; ordinary `require_auth` is insufficient and a deployment without the credential fails closed.
6. **Restart behavior — resolved (OD-06).** No automatic resume; a restart comes up `disabled`.

Still open: whether day-trading positions must be forcibly flattened before the close is **PENDING_OWNER_DECISION** and is not implemented by AT-01. Multi-portfolio concurrency and retention remain later-milestone design questions.

---

## 11. Final recommendation

**Recommendation:** Approve AT-01 for a separately reviewed implementation, not the full roadmap at once.

AT-01 is low financial risk because it cannot submit an order. It establishes the runtime, modes, lease, persistence, API and UI shell that every later milestone depends on. AT-02 should not start until AT-01's no-order and no-duplicate guarantees are accepted.

The autonomous engine should not be described as “complete” after the documentation set. It becomes real only when a durable cycle produces a linked signal → proposal → risk decision → order → fill → portfolio/ledger → exit without a human invoking each step, and when the kill/restart/duplicate tests pass. The architecture is ready to begin that work; the product is not yet autonomous.
