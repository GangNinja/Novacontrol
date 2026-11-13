# Autonomous Paper Trading — Design Decisions

**Status:** Accepted decision record, reconciled 10 November 2026 with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md`. AT-01 is implemented; AT-02–AT-06 are not approved or started.
**Date:** 9 November 2026 (reconciled 10 November 2026)
**Scope:** Architectural decisions behind the autonomous paper-trading design. They authorize the AT-01 shell only; nothing here authorizes live trading or the later milestones.

---

## 1. Decision index

| ID | Decision | Status |
|---|---|---|
| ADR-01 | Compose the existing engines; add no parallel financial service | Accepted |
| ADR-02 | One runtime tick inside `NovaMarketService`, not a second task | Accepted |
| ADR-03 | The only autonomous order path is strategy proposal → NM-08 → NM-10 | Accepted |
| ADR-04 | Fail closed on unverifiable session, data, risk, execution or portfolio state | Accepted |
| ADR-05 | Four operating modes are separate from runtime health | Accepted |
| ADR-06 | Paused blocks entries but keeps protective exits armed by default | Accepted |
| ADR-07 | Use a short-lived SQLite cycle lease, not an external queue | Accepted |
| ADR-08 | Reuse proposal/order idempotency; never invent a second order ID | Accepted |
| ADR-09 | Exits use a narrow `propose_autonomous_exit` seam, not direct execution | Accepted |
| ADR-10 | v1 uses strategy sizing + NM-08; no target-weight `PortfolioEngine.apply` | Accepted |
| ADR-11 | Trust only the verified exchange calendar; fail closed when coverage is unknown | Accepted |
| ADR-12 | Autonomous conflicts default to `review`; no silent priority selection | Accepted |
| ADR-13 | Learning remains advisory; no automatic promotion or risk change | Accepted |
| ADR-14 | No automatic autonomous resume; require recovery and a clean cycle | Accepted |
| ADR-15 | Additive API and UI; do not fork the application or redesign the desk | Accepted |
| ADR-16 | Disclose delayed data and the 15-minute refresh floor | Accepted |
| ADR-17 | No direct wallet/ledger/position mutation from autonomy | Accepted |
| ADR-18 | Retry only idempotent reads; no blind retry of writes/submits | Accepted |

---

## ADR-01 — Compose the existing engines

**Decision:** The autonomy layer is an orchestrator and lifecycle manager. It reuses `AssetDiscoveryEngine`, `StrategyEngine`, `RiskEngine`, `ExecutionEngine`, `PortfolioEngine`, `WalletEngine`, `LearningService` and the existing event bus.

**Alternatives considered:**

- Build a new autonomous trading service with its own risk and execution code. Rejected: duplicates the accepted financial logic, creates two sources of truth and would bypass the NM-08/NM-10 guarantees.
- Put autonomy inside `StrategyEngine`. Rejected: the strategy engine is a decision/proposal engine, not a scheduler, lease owner or position supervisor.
- Put autonomy inside the API request handlers. Rejected: no continuous lifecycle, no restart recovery, and a long-running request is not a monitor.

**Consequences:** A new package is justified, but it must stay thin. Any new financial calculation is a design defect.

---

## ADR-02 — One runtime tick

**Decision:** `NovaMarketService._runtime_tick()` calls `AutonomyRuntime.tick(now)`. There is no second long-running asyncio task, Celery worker, external cron or `InMemoryScheduler` job for autonomy.

**Alternatives considered:**

- A separate autonomy task started by the API. Rejected: two lifecycles, two shutdown paths, duplicate-tick races and a higher chance of a stray task after a reload.
- An external scheduler. Rejected: unnecessary infrastructure, credentials and operational surface.
- Reuse the generic `InMemoryScheduler`. Rejected: it is an application-level once-off scheduler, not a durable recurring trading loop.

**Consequences:** The existing 15-minute market-refresh cadence is the floor for scans. The autonomy scheduler can skip, but cannot scan faster than the verified data cadence. This is an honest limitation, not a bug.

---

## ADR-03 — One order path

**Decision:** The only autonomous order path is:

```text
StrategySignal
  -> ProposedAction
  -> RiskEngine.assess()
  -> approved proposal
  -> StrategyEngine.forward()
  -> ExecutionEngine.submit()
  -> ExecutionEngine.process()
```

**Alternatives considered:**

- Call `ExecutionEngine.submit()` directly from an autonomy decision. Rejected: it would skip the proposal/risk provenance that the execution engine requires and would create a parallel decision path.
- Call `PortfolioEngine.apply()` for rebalancing. Rejected: it moves the book and ledger directly, bypassing NM-10.
- Call `WalletEngine.transfer()` for an exit. Rejected: bypasses order, fill, fee, slippage and reconciliation semantics.

**Consequences:** Every autonomous order has a `proposal_id`, `risk_decision_id` and order ID. There is one place to audit.

---

## ADR-04 — Fail closed

**Decision:** If the session, calendar coverage, market data, risk decision, execution availability, portfolio state or cycle lease cannot be verified, no order is submitted. A skipped cycle is a valid outcome.

**Alternatives considered:**

- Fall back to the last known price. Rejected: invents a fill reference and can execute on stale data.
- Retry execution until it succeeds. Rejected: a failed order may be a real terminal state; retrying can duplicate or bypass the idempotency record.
- Degrade to manual execution. Rejected: an autonomous component must not silently become a human-triggered order path.

**Consequences:** Some market opportunities are missed. That is preferable to an unverified paper order and is consistent with the existing execution stale-data refusal.

---

## ADR-05 — Modes and runtime states are separate

**Decision:** Operating modes are `disabled`, `monitoring`, `autonomous`, `paused`. Runtime states are `stopped`, `starting`, `running`, `recovering`, `degraded`, `stopping`. There is no `live` mode.

**Alternatives considered:**

- Add a `live` mode for future use. Rejected: the product has no broker/live path, and an inert mode invites accidental wiring.
- Let `enabled` mean "trading." Rejected: the service is enabled for market data/UI even when trading is not permitted.
- Treat `paused` as task cancellation. Rejected: existing positions would lose monitoring and protective exits.

**Consequences:** The UI must show both dimensions. A runtime can be `running` while `monitoring` or `paused`; that is not a contradiction.

---

## ADR-06 — Paused protective exits

**Decision:** `paused` blocks new entries. Protective exits remain armed under the fixed `paused_exit_policy="protective"` (owner decision OD-02); `monitor_only` is not a supported policy. Every allowed exit still passes NM-08/NM-10.

**Alternatives considered:**

- Paused means no orders at all. Rejected: it would leave a stop-loss unarmed, which is a greater safety failure than a protective reduction. The literal "no new autonomous orders" is preserved by distinguishing entries from protective exits.
- Paused cancels all open orders. Rejected: an operator may pause entries while a legitimate exit is working.

**Consequences:** The pause dialog must state which policy is active. The decision is visible in the status and cycle records.

---

## ADR-07 — Short-lived SQLite cycle lease

**Decision:** A single-row lease in the same SQLite database is acquired conditionally for a cycle, renewed at phase boundaries and released in `finally`. `lease_seconds` is bounded and at least `max_cycle_seconds`. The in-process `asyncio.Lock` is an additional guard, not the only guard.

**Alternatives considered:**

- In-process singleton only. Rejected: two API processes or two uvicorn workers could each run a cycle.
- A file lock. Rejected: platform/filesystem edge cases; SQLite is already the durable store.
- Redis/etcd lease. Rejected: unnecessary infrastructure and a new dependency.
- A long-lived lease for the whole process. Rejected: a crash would block recovery for the full lease.

**Consequences:** A crash can leave a cycle interrupted; the next process waits for the bounded lease or marks the cycle interrupted when ownership is proven. Recovery is explicit.

---

## ADR-08 — Reuse idempotency

**Decision:** The autonomy layer uses deterministic cycle keys and the existing `proposal_id`/execution order uniqueness. It never generates a second order ID for a retry and never submits a terminal proposal again.

**Alternatives considered:**

- Add an `autonomy_order_id` above the execution order. Rejected: two order identities invite double submission and reconciliation ambiguity.
- Rely on UI disabling. Rejected: API retries and restart paths bypass the UI.

**Consequences:** A repeated cycle may replay the same order/fill; that is the desired behavior. A new strategy signal with a new data cutoff is required for a new attempt.

---

## ADR-09 — Exit proposal seam

**Decision:** Add `StrategyEngine.propose_autonomous_exit()` that creates an `EXIT`/`SELL` signal and proposal through a shared `_propose_with_risk()` helper. `ExitSupervisor` calls it; it does not call execution directly.

**Alternatives considered:**

- Direct `ExecutionEngine.submit()` from the supervisor. Rejected: no proposal/risk provenance and a bypass of the accepted gate.
- A second exit risk engine. Rejected: duplicate risk policy.
- A new synthetic order endpoint. Rejected: violates the single order path.

**Consequences:** The exit proposal has a reserved `system:protective-exit` provenance and is visible in the existing proposal/risk UI. The common helper prevents the normal and protective paths from drifting.

---

## ADR-10 — Capital allocation boundary

**Decision:** v1 uses the existing strategy sizing (`notional_fraction`, 50% reduce, full exit) plus NM-08 exposure/cash/position/sector limits and the horizon `max_positions` pre-filter. Autonomy does **not** call `PortfolioEngine.propose()/apply()` and does not claim target-weight rebalancing.

**Alternatives considered:**

- Use `PortfolioEngine.apply()` for autonomous target weights. Rejected: it bypasses NM-10 and moves the ledger directly.
- Build a new autonomous allocator now. Deferred: it needs a reviewed order-intent bridge and a full mapping of target-weight deltas to proposals; it is not necessary for the minimum signal-driven lifecycle.
- Ignore the horizon max positions. Rejected: it would allow more names than the accepted portfolio shape. The pre-filter is used, and NM-08 remains authoritative.

**Consequences:** This is a visible product limitation. A future allocator must be a separate design with an execution-aware bridge, not a shortcut through the manual apply path.

---

## ADR-11 — Verified calendar only

**Decision:** Autonomy uses `marketdata/calendar.py` and `plan_market_refresh()`. Weekdays alone are not a trading calendar. If the year is outside the verified holiday coverage, autonomy may monitor but cannot submit new orders.

**Alternatives considered:**

- Use `discovery/sessions.py` because it already exists. Rejected: it documents that it has no holiday calendar.
- Infer holidays from missing bars. Rejected: a provider outage would be misread as a holiday.
- Fall back to a weekday schedule. Rejected: would trade on Republic Day/Diwali and mis-state session validity.

**Consequences:** Calendar coverage must be maintained annually. The UI/health surface must say when coverage is unverified.

---

## ADR-12 — Conflict policy review

**Decision:** Autonomous runs use `conflict_policy="review"` (or `block`) and do not silently select a winning signal by priority. A human can resolve a conflict in the existing strategy UI.

**Alternatives considered:**

- `priority` automatic selection. Rejected for v1: it is a valid existing mode but creates an unattended discretionary choice; the autonomy default should be conservative.
- Ignore conflicts and submit both. Rejected: duplicate/opposite exposure and likely duplicate orders.

**Consequences:** Some opportunities are intentionally not traded. The conflict record is visible and traceable.

---

## ADR-13 — Learning is advisory

**Decision:** Autonomy records paper evidence and may read learning alerts, but never approves, promotes, rolls back or weakens a risk policy. Learning changes remain governed.

**Alternatives considered:**

- Auto-promote a high-scoring candidate. Rejected: violates the accepted governance and could change live strategy behavior without review.
- Auto-adjust risk thresholds after losses. Rejected: weakens financial safety and creates a feedback loop.

**Consequences:** Improvement is slower but auditable and reversible.

---

## ADR-14 — No automatic autonomous resume

**Decision:** A restart always comes up `disabled`; there is no auto-resume and no `resume_on_restart` setting (owner decision OD-06). A prior `autonomous` mode is not proof that resuming is safe, so an authorized operator must start the shell again.

**Alternatives considered:**

- Resume the persisted mode immediately. Rejected: a crash can leave pending fills, an open order or stale data.
- Resume automatically or start monitoring on boot. Rejected: a crash can leave pending fills, an open order or stale data, so restart continuity is never assumed.

**Consequences:** An operator may need to resume after a restart; that is the safe default.

---

## ADR-15 — Additive API/UI

**Decision:** Add `/market/autonomy/*` and a new React page; extend Dashboard, Paper, Health and Activity with links/cards. Do not create a second application, replace the workspace shell or redesign existing pages.

**Alternatives considered:**

- Fork the NovaMarket frontend. Rejected: violates the accepted workspace architecture.
- Put all controls on the Paper page. Rejected: mixes manual execution and autonomous lifecycle, and would make the kill/monitor state hard to reason about.
- Build a separate dashboard. Rejected: duplicates navigation, auth and visual language.

**Consequences:** Five-place API registration and a focused UI addition are required; the existing 25 routes remain.

---

## ADR-16 — Disclose delayed data

**Decision:** The system states a 15-minute refresh cadence, not tick data. Autonomy does not scan faster than the floor and does not claim intraday exchange precision. Paper fills use stored references and the existing NM-10 model.

**Alternatives considered:**

- Poll faster to simulate ticks. Rejected: unnecessary provider load and a misleading claim.
- Hide the limitation. Rejected: the accepted baseline already discloses delayed/EOD data.

**Consequences:** Day-trading behavior is paper simulation, not exchange-grade execution. This must be visible in the UI and acceptance report.

---

## ADR-17 — No direct financial mutation

**Decision:** Autonomy may read portfolios/wallets and call the public strategy/risk/execution services. It may not call `PortfolioEngine.apply()`, `WalletEngine.transfer()`, `PortfolioStore.upsert_position()`, ledger correction or any direct SQL write to financial tables.

**Alternatives considered:**

- A small direct write for speed. Rejected: breaks the single-owner rule and reconciliation.
- A separate autonomy ledger. Rejected: duplicates accounting and breaks NC = ₹1 consistency.

**Consequences:** All financial effects go through NM-10. Static tests and code review enforce this.

---

## ADR-18 — Bounded retries

**Decision:** Retry only idempotent reads (for example, a provider quote can be re-read next cycle). Do not retry a write, a risk assessment with changed state, or a submit whose outcome is unknown. On an unknown execution outcome, read the order by proposal and reconcile.

**Alternatives considered:**

- Retry every failed submit. Rejected: duplicates and masks terminal failures.
- Never retry reads. Rejected: transient provider failures would cause unnecessary skips.

**Consequences:** A failed cycle may be retried on the next cadence; an execution failure is terminal for that proposal.

---

## 4. Decisions resolved by the owner (10 November 2026)

1. **Horizon mapping — resolved (OD-01).** `short_term → day_trading`, `medium_term → swing_trading`, `long_term → long_term`; persisted portfolio keys remain NM-07-compatible.
2. **Paused exits — resolved (OD-02).** `paused_exit_policy` is fixed to `protective`; `monitor_only` is not supported.
3. **Target-weight autonomy — resolved (OD-03).** Excluded from v1; the only eventual order path is an approved strategy proposal → NM-08 → NM-10.
4. **Calendar coverage — resolved (OD-04).** Only explicitly verified coverage may be used. 2026 is the verified year; coverage is never extrapolated and autonomy fails closed outside it.
5. **Operator permissions — resolved (OD-05).** Start/stop-style controls require a dedicated server-side autonomy-operator credential, separate from ordinary authentication; a deployment without it fails closed.
6. **Restart behavior — resolved (OD-06).** No automatic resume; a restart comes up `disabled`.

### Still open

- **Day-trading forced flattening** (whether short-term positions must be flattened before the close) is **PENDING_OWNER_DECISION**. AT-01 neither implements nor invents it.
- Multi-portfolio concurrency and retention remain design questions for later milestones, not AT-01 blockers.
