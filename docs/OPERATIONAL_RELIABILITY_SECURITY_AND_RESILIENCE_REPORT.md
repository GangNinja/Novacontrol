# NovaMarket — Operational Reliability, Security and Resilience Report

> **Historical baseline.** This is the pre-remediation audit. The findings below were re-assessed and remediated in [SECURITY_REMEDIATION_AND_OPERATIONAL_CLOSURE_REPORT.md](SECURITY_REMEDIATION_AND_OPERATIONAL_CLOSURE_REPORT.md); that report is current where the two differ. The body below is retained unchanged as the point-in-time record.

**Audit date:** 2026-11-21  
**Suite rerun recorded:** 2026-11-23  
**Scope:** completed PI-01–PI-24 system plus the post-PI multi-timeframe, forecasting, continuous-paper and RL evaluation work.  
**Mode:** controlled paper only. No live trading was enabled, no policy was promoted, no PI-25 was created and no commit or push was performed.

## 1. Executive assessment

NovaMarket remains a local-first, single-process, paper-only research and execution system. The safety boundary is intact:

- every entry is still **strategy proposal → NM-08 risk approval → NM-10 paper execution**;
- the risk engine fails closed when it cannot establish an approval;
- execution retries are idempotent and a durable in-flight journal prevents duplicate fills;
- the wallet ledger is append-only and balanced;
- no live broker, live order route or automatic RL policy promotion exists.

The audit found and fixed **two confirmed reliability defects**: (1) `PortfolioStore.transaction()` started a second `BEGIN IMMEDIATE` for a nested transaction, so the documented nested-transaction contract raised `sqlite3.OperationalError: cannot start a transaction within a transaction`; (2) a strategy signal whose `reference_price` was `None` reached arithmetic that multiplied a `Decimal` by `None`, raising an opaque `TypeError` instead of a fail-closed validation error. Nested portfolio blocks now use SQLite savepoints, and signal proposal now rejects a missing/non-positive price before sizing. Regression tests were added for both.

The system is **operationally fit for continued controlled paper operation**, with the limitations in §14. It is **not live-trading ready**. The earlier full single-process regression run did not complete cleanly: it exited 1 at roughly 35% with no pytest failure summary, around the LSTM block. The LSTM file passes on its own (24 passed), so this was treated as a test-process/resource stability problem and the suite was rerun in ordered file groups. On 23 November 2026 the single-process suite completed without that crash but still exited 1 on one date-sensitive scheduler fixture; Group A passed and Group B had the same single failure. The group and single-process results are recorded in §15; no test was skipped or weakened.

| Area | Rating | Basis |
|---|---|---|
| Architecture reliability | **PARTIAL** | Layered boundaries are good, but one process, one SQLite file and in-process locks are single points of failure; `service.py` is large and tightly coupled. |
| Market-data reliability | **PASS** | Bounded retry/backoff, circuit breaker, quota, provider health, provenance, stale/future fail-closed, append-only store and tests. No live outage was injected. |
| Scheduler reliability | **PASS** | Durable checkpoints, atomic claims, lease recovery, duplicate-tick guard, target-time skip, session/calendar fail-closed and restart tests. |
| Execution safety | **PASS** | Proposal idempotency, risk revalidation, reservations, durable fill journal, compensation and restart recovery; duplicate submission test passes. |
| Financial accounting integrity | **PASS** | 17 wallets, 40 ledger entries and 20 transactions reconcile exactly on an isolated copy; ledger sum 0.00; no user wallet negative; lock drill failed closed with no mutation. |
| Database reliability | **PARTIAL** | All 14 non-empty SQLite files (13 NovaMarket stores plus `memory.sqlite3`) pass integrity checks; backup/restore and corruption detection pass. One 0-byte adaptive NovaMarket store is only a placeholder, there is no automated backup job, and not every store declares foreign keys. |
| Security | **PARTIAL** | Bearer auth, separate operator token, loopback guard, OriginGuard, security headers and parameterized SQL are present; authentication is off by default and the local launcher uses a predictable admin key. |
| Risk-engine resilience | **PASS** | Stale/missing data, hard breaches, generic assessment failure and restrictions all fail closed; tests pass. |
| Reinforcement-learning isolation | **PASS** | No execution, wallet or risk-limit handles; `automatic_policy_promotion=false`; no activation/promotion route. |
| Resource and long-running stability | **PARTIAL** | Baseline RSS and DB sizes are documented, but no multi-hour endurance run was performed. The historical single-process crash is unresolved; the 23 November attempt completed but was not green. |
| Observability and audit logging | **PARTIAL** | Durable decisions, fills, ledger, health, retrieval and event records exist; several 500 handlers return raw exception text and there is no unified metrics/rotation surface. |
| Disaster recovery | **PARTIAL** | Restart, lease, checkpoint, ledger-lock and backup/restore drills pass; no unexpected-shutdown or live provider/network outage was exercised end to end. |
| Overall controlled paper readiness | **PARTIAL** | Core safety controls pass; operational hardening remains for auth defaults, error detail, backup automation and endurance evidence. |
| Live readiness | **BLOCKED** | No live data, live broker, live paper session or production deployment was used. |

## 2. Architecture and reliability assessment

The reviewed stack is:

`marketdata/providers → marketdata/engine → marketdata/store` → `multitimeframe` / `forecasting` / `prediction` → `strategies` → `risk` → `execution` → `portfolio` / `wallet` → API and UI.

The intended boundaries are visible and mostly enforced:

- providers own fetching, retry, circuit and quota state;
- the engine owns provenance, freshness, quality and fail-closed decisions;
- the strategy layer produces proposals, never orders;
- NM-08 owns risk decisions and revalidation;
- NM-10 owns the only paper order/fill path;
- the wallet ledger is independent of portfolio valuation;
- the scheduler owns durable checkpoints, not trades;
- the RL environment is isolated from the wallet and execution stores.

### Single points of failure and coupling

1. **One SQLite file and one process.** The main financial store is a single file; there is no replication, failover or multi-process writer. This is appropriate for a local desk but is a single point of failure for a shared deployment.
2. **One event loop.** The runtime, forecast scheduler, autonomy heartbeat and API share an asyncio loop. A CPU-bound task can delay API latency; the scheduler's target-time checks and skip behavior prevent a late tick from becoming an unauthorized trade, but they do not remove the latency coupling.
3. **`service.py` breadth.** The application service is a large integration surface. It is the place where a future change is most likely to bypass a boundary accidentally; route and engine tests are the current protection.
4. **In-memory locks.** Wallet, execution, portfolio and scheduler correctness relies on process-local locks plus SQLite transactions. Cross-process writes rely on SQLite locking only; the lock drill demonstrates fail-closed behavior, not multi-writer throughput.
5. **No live broker or live data path.** This is a deliberate safety property, but it means live operational readiness is untested and blocked.

No architectural redesign was performed. The confirmed transaction defect was fixed locally.

## 3. Market-data reliability

Reviewed: `marketdata/providers/base.py`, `marketdata/engine.py`, `marketdata/store.py`, provider adapters, freshness/quality modules, the exchange calendar and the provider registry.

### Controls verified in code

- **Bounded retries with backoff.** Provider calls retry within a finite policy; timeout and connection failures are classified and retried only when retryable.
- **Circuit breaker.** A failing provider opens its circuit and stops spending requests; symbol-specific and unsupported-interval failures are subtracted from provider-wide failure counts so one bad symbol cannot open the whole provider.
- **Daily quota.** Provider quota is tracked and enforced before a call; a quota refusal is a classified provider failure, not an invented quote.
- **Provider health and retrieval log.** Health and every retrieval are persisted (`market_provider_health`, `market_retrieval_log`).
- **Overlap refusal.** The engine refuses a second ingestion run for the same scope instead of allowing two writers to race.
- **Provenance.** Observations and bars carry their provider and timestamps; the portfolio exposes `price_provider`, `price_from` and `fixture_priced` rather than pretending a fixture is live.
- **Fail-closed freshness.** Future bars are filtered; stale observations are marked stale; an unusable quality result refuses execution; the engine does not silently replace a missing real value with a synthetic value.
- **Append-only observations and bars.** Corrections are traceable conflict rows; identical ingestion is idempotent; duplicate timestamps are collapsed by the consuming analysis where documented.
- **Partial candles and session boundaries.** The multi-timeframe analyzer excludes the current incomplete candle and refuses a missing/stale layer; the exchange calendar fails closed outside verified coverage.

### Evidence

- `tests/test_novamarket_marketdata.py` includes retry, timeout, circuit-open, duplicate-ingestion, stale-not-live, structural-bar and provider-health tests.
- `tests/test_novamarket_multitimeframe.py` includes missing/stale/partial/duplicate/out-of-order/future-bar tests; `test_future_bars_are_never_visible_to_a_historical_decision` is the look-ahead guard.
- Prior continuous-paper evidence records a provider-failure contract and no fabricated quote acceptance.

### Limitations

- No live provider outage or network partition was injected during this audit. Provider behavior is verified by tests and adapters, not by a live outage.
- The inspected operational database is largely fixture-backed; live market coverage was not established.
- The 2027 exchange calendar is not published; out-of-coverage behavior is fail-closed but 2027 scheduling is not production-verified.

**Rating: PASS** (implemented controls and tests), with the live-data limitation above.

## 4. Scheduler and background-task reliability

Reviewed: `scheduling/service.py`, `scheduling/schedule.py`, `prediction/store.py`, `autonomy/runtime.py`, `autonomy/monitoring.py`, and the service runtime loop.

### Controls verified

- **Durable checkpoint state.** Cycles, requests, checkpoints and lifecycle records are persisted; a restart reuses the same cycle/checkpoint identity.
- **Atomic claim and lease.** A due checkpoint is claimed atomically with an owner/lease; expired leases are recovered; duplicate ticks cannot execute one checkpoint twice.
- **Target-time handling.** A checkpoint whose target has passed is skipped or marked missed rather than executing against a stale target; the session snapshot drives the decision.
- **Market-session awareness.** Calendar/session evaluation fails closed outside coverage; no forecast is forced open from a guessed session.
- **Graceful shutdown.** Scheduler `stop()` cancels and awaits its task; the service runtime cancels the forecast scheduler, heartbeat and runtime task; heartbeat tasks handle `CancelledError`.
- **No trade path.** The scheduler writes forecasts/evidence and never executes a paper order or wallet movement.
- **Horizons.** `ForecastHorizon` includes 1m, 5m, 15m, 20m, 1h and longer horizons; schedule policies define per-horizon lead/confirmation stages. The post-PI multi-timeframe analyzer is analysis-only and does not create an order.
- **1m/5m/15m/20m/1h monitoring cycles.** Forecast cycles and checkpoint stages exist for these horizons; the autonomy monitoring refresh floor is 15 minutes by design, so a 1m/5m monitoring loop is not the autonomy refresh path. The 1m/5m layers belong to the multi-timeframe analyzer and short-horizon forecasts.

### Evidence

- `tests/test_novamarket_scheduler.py` pins stable cycle identity, atomic claim, lease recovery, restart recovery, duplicate-tick non-duplication and stale-source refusal.
- `tests/test_novamarket_autonomy.py` pins disabled-by-default boot, no resume after restart, one cycle at a time, lease renewal/expiry, explicit recovery and no trading route.
- The runtime task delays its first tick and catches tick exceptions so one failed tick does not stop future ticks.

### Limitations

- The runtime is a single event loop; a long CPU-bound tick can delay other work.
- No multi-hour scheduler soak was performed.
- The 1m/5m monitoring cadence is not the autonomy refresh cadence; do not describe the 900-second autonomy floor as a 1-minute cycle.

**Rating: PASS** for the implemented scheduler/checkpoint behavior; **PARTIAL** for 1m/5m operational monitoring because the autonomy floor is 15 minutes.

## 5. Trading execution safety

Reviewed: `execution/engine.py`, `execution/store.py`, `execution/pricing.py`, `execution/reconciliation.py`.

### Controls verified

- **Proposal idempotency.** An order is unique per proposal (`proposal_id UNIQUE`); a duplicate submission returns the existing order/fills and does not create a second order.
- **Risk authorization.** The order carries the NM-08 decision id; the engine revalidates the proposal and the actual order notional before filling. A rejected or restricted proposal never reaches an order.
- **Reservations.** Cash reservations are recorded before execution; fill/cancel/expire paths release or consume them.
- **Durable in-flight journal.** A fill is written as `pending` before wallet/portfolio side effects; the journal is settled `committed` after the execution record is written. Pending rows are recovered at startup.
- **Compensation.** Wallet effects are reversed with deterministic idempotency keys and the position effect is reversed against the fill id; a failure leaves the journal pending for retry, not silently accepted.
- **Duplicate fill prevention.** Unique `(order_id, sequence)` and the execution fill record guard against duplicate fills.
- **Order-state transitions.** The transition table is explicit; illegal transitions raise rather than overwrite.
- **Partial fills.** Available volume caps a fill and the remainder stays open; multiple partial fills sum to the requested quantity.
- **Rejected/expired/cancelled orders.** These paths release reservations and never erase recorded fills.
- **Reconciliation.** A clean fill passes; an overfilled order is detected; reconciliation records are durable.
- **Execution timeout / stale data.** A stale market reference refuses execution; a limit is not filled above its limit.

### Evidence

`tests/test_novamarket_execution.py` includes duplicate submission replay, rejected proposal, stale data, limit/stop behavior, partial fill, sell preservation, reconciliation, cancellation, expiry, risk restriction, wallet/position compensation, pending-fill restart recovery and actual-notional revalidation. `tests/test_novamarket_wallet.py` adds ledger and concurrent-withdrawal coverage.

### Limitations

- This is a simulated broker; there is no live partial exchange fill, queue position, market impact or broker rejection.
- The fill journal retains committed rows indefinitely for audit. That is safe, but the table grows with every fill and has no retention policy.
- The inspected production-like copy contains one historical fill whose `executed_at` is the simulated market time while order transition rows use wall time. This is consistent with replay/simulation, but operators must treat simulated timestamps separately from wall-clock events.

**Rating: PASS** for paper execution safety.

## 6. Financial accounting integrity

Reviewed: `wallet/store.py`, `wallet/engine.py`, `portfolio/store.py`, `portfolio/engine.py`, execution position effects and valuation snapshots.

### Invariants checked

- amounts are stored as decimal text and exact integer paise;
- every wallet balance equals the sum of its signed ledger entries;
- every transaction's entries sum to zero;
- the whole ledger sums to zero including the issuance account;
- user wallets cannot go negative;
- ledger entries and transactions are append-only through database triggers;
- idempotent requests return the original transaction without a second movement;
- portfolio positions use average cost and realized P&L; valuations are append-only;
- a reversal is a new transaction and preserves the original.

### Evidence from the isolated copy

The primary store was copied, including its WAL/SHM sidecars, to `tmp/ops_audit/db_integrity/novamarket/`. The copy was inspected read-only by the audit:

- `WalletEngine.reconcile()`: **ok**, 17 wallets, 40 entries, 20 transactions, ledger sum `0.00`, 0 issues.
- Stored balances vs `SUM(signed_amount_paise)`: 0 mismatches.
- Transaction entry counts vs `entry_count` and transaction sums: 0 mismatches.
- Negative non-system wallets: 0.
- Invalid position quantity/average cost: 0.
- The one filled portfolio had cash `17998.32` plus a `2000.90` marked position, equal to the `20000.00` initial capital less the `0.78` fee, with normal display rounding in average cost.

A real SQLite write-lock drill on a temporary ledger held `BEGIN IMMEDIATE` from a second connection while a deposit was attempted:

- outcome: `WalletStoreError: could not start a ledger transaction: database is locked`;
- elapsed: 5.585 s (the configured busy timeout);
- balance before/after: `0.00` / `0.00`;
- transactions after: 0.

That is fail-closed financial behavior: the failed deposit did not partially post.

### Fixed defect: nested portfolio transactions

`PortfolioStore.transaction()` previously executed `BEGIN IMMEDIATE` on every call even when `_depth > 0`. SQLite rejects a nested `BEGIN`, so a nested transaction failed before its body. It now uses a named `SAVEPOINT` for inner blocks, releases it on success, rolls back only that savepoint on an inner failure, and commits/rolls back the outer transaction once. Regression tests cover nested commit and inner-failure isolation.

### Limitations

- The main store is one SQLite file; integrity is verified on a copy, not under a multi-writer deployment.
- Historical transaction timestamp semantics can differ from wall time during replay.
- No automated reconciliation schedule is configured; reconciliation is callable and tested, not a cron job.

**Rating: PASS** for the tested ledger and accounting invariants.

## 7. Database reliability

### Stores inspected

The data directory contains 15 SQLite files: 14 NovaMarket stores plus NovaControl's `memory.sqlite3`. The NovaMarket stores are:

`novamarket.sqlite3`, `novamarket_adaptive.sqlite3` (0 bytes), `novamarket_dashboard.sqlite3`, `novamarket_evaluation.sqlite3`, `novamarket_event_intelligence.sqlite3`, `novamarket_forecast_intelligence.sqlite3`, `novamarket_learning.sqlite3`, `novamarket_multiagent.sqlite3`, `novamarket_optimization.sqlite3`, `novamarket_portfolio_management.sqlite3`, `novamarket_research.sqlite3`, `novamarket_risk_intelligence.sqlite3`, `novamarket_rl.sqlite3`, `novamarket_shadow.sqlite3`.

### Integrity and recovery drill

Each non-empty file was copied, including `-wal`/`-shm` sidecars where present, and checked read-only:

- 14 non-empty SQLite files: `PRAGMA integrity_check` = `ok` (13 non-empty NovaMarket stores plus `memory.sqlite3`);
- foreign-key check: 0 violations on every checked store;
- primary store: 127 tables, 62,408 rows;
- `novamarket_adaptive.sqlite3` is a 0-byte placeholder and was reported as EMPTY, not as a valid database.

Backup/restore drill on an isolated copy of `novamarket.sqlite3` using SQLite's backup API:

| Artifact | Integrity | FK violations | Tables | Rows |
|---|---|---|---:|---:|
| Isolated source copy | ok | 0 | 127 | 62,408 |
| Backup | ok | 0 | 127 | 62,408 |
| Restored copy | ok | 0 | 127 | 62,408 |

A corrupted copy (bytes overwritten in a page) failed `PRAGMA integrity_check` with a b-tree/page error, demonstrating that corruption is detectable and must not be silently restored.

### Transaction and access patterns

- Wallet writes use `BEGIN IMMEDIATE`, WAL, `synchronous=FULL` and a 5-second busy timeout.
- Execution writes are transactional and use `foreign_keys=ON` and WAL.
- Market-data store uses WAL, `foreign_keys=ON`, a 5-second busy timeout and a schema version.
- `PortfolioStore` uses `BEGIN IMMEDIATE` and now savepoints for nesting; its transaction contract is tested.
- Schema versions are checked where a store declares one; a newer version is refused rather than read best-effort.
- The main database is shared by wallet, execution, portfolio, risk, prediction and other tables; no cross-store SQL transaction is claimed. Cross-store effects are ordered and reconciled.

### Limitations

- There is no automated backup scheduler or retention policy in the repository; the successful drill was manual and isolated.
- Not every store declares SQLite foreign keys; `foreign_key_check` therefore proves no declared-FK violations, not full referential integrity for every logical relationship.
- SQLite remains a single-writer bottleneck. The lock drill proves fail-closed behavior, not high concurrency.
- The 0-byte adaptive store is a placeholder; it is not evidence of a working adaptive database.

**Rating: PARTIAL** — data integrity and restore are proven, operational backup automation and full FK coverage are not.

## 8. Security findings

### Present controls

- **Bearer authentication** with constant-time comparison (`hmac.compare_digest`).
- **Separate autonomy-operator credential.** Ordinary API auth cannot start/stop the autonomy runtime; an unset operator token fails closed with 403.
- **Loopback-only admin credential.** `NOVACONTROL_ADMIN_TOKEN` is accepted only from loopback; it is not a built-in default in the backend.
- **Host and origin guards.** Trusted Host middleware rejects DNS-rebinding host names; OriginGuard refuses foreign-origin unsafe requests; loopback origins are the documented exception.
- **CORS** is opt-in and exact-origin; credentials are disabled.
- **Security headers** include `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy` and a restrictive CSP.
- **Secret-free configuration.** NovaMarket configuration rejects secret-shaped keys and stores only `env:NAME` / `store:NAME` references; the Alpha Vantage adapter does not echo provider error bodies and its API key is not included in a normalized error.
- **Parameterized SQL.** No user-controlled string was found interpolated into SQL except fixed table names and generated savepoint identifiers.
- **No unsafe deserialization.** No `pickle`, unsafe YAML loader, `eval` or `exec` was found in the NovaMarket surface.
- **Signal/price validation.** A signal with no positive reference price is now rejected with `StrategyValidationError` before any EXIT arithmetic; this is both a type-safety and fail-closed fix.
- **Local exposure.** The launcher binds uvicorn and Vite to `127.0.0.1`; the API is not bound to `0.0.0.0` by the project launcher.
- **Sensitive logs.** Wallet failure logging records the operation and error class, not the token or payload. The request logger records method/path/status, not query strings or headers.

### Evidence from an authenticated smoke test

With `NOVACONTROL_API_TOKEN` and `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN` set:

| Check | Result |
|---|---|
| `/health` without token | 200 (liveness only) |
| `/status` with wrong bearer | 401 |
| `/status` with bearer | 200 |
| `/market/status`, multi-timeframe, risk, execution, autonomy status with bearer | 200 |
| `POST /market/wallets` with foreign Origin | 403 |
| same request with same-origin Origin | 422 (validation reached; not a CSRF rejection) |
| security headers on `/health` | all present |

### Findings and residual risks

1. **Authentication is off by default.** `ApiTokenAuthenticator` returns `authenticated=True`, principal `local`, when no token is configured. The API is protected by loopback binding and OriginGuard, but any local process can call it. This is a deliberate local-first default, not a hardened shared-PC default.
2. **The local launcher uses a predictable admin key.** `start.ps1` sets `NOVACONTROL_ADMIN_TOKEN=narasimha-novacontrol` when unset. It is loopback-only, but it is not a secret against another local process. Operators on a shared machine should set a private token.
3. **The admin key can satisfy both ordinary and operator checks for loopback requests.** This is documented, but it collapses two permissions for local use.
4. **SSE accepts `?token=`.** EventSource cannot set an Authorization header; the token can appear in browser history, proxy logs or a copied URL. The path logger does not log the query string, but intermediaries may. Prefer a short-lived ticket for any remote deployment.
5. **Several 500 handlers return `str(exc)`.** This can disclose internal paths, SQLite text or implementation details. The inspected provider adapters scrub provider bodies, but the API surface is not uniformly generic. This is a hardening item, not an observed credential leak.
6. **No dependency-vulnerability scan.** `pip-audit` and `safety` are not installed; no vulnerability database query was performed. Optional heavy dependencies (`torch`, `xgboost`, `lightgbm`) are not in the base install.
7. **`.env` is untracked and gitignored**, but only ordinary file permissions were observed. The audit did not read or print its values. Use the OS secret store or a private environment for production credentials.
8. **The EventSource cannot send headers**, so the query-token exception is a real protocol constraint, not an accidental omission.

No destructive security change was made. The recommendation is to set a private API/operator token and keep the API loopback-only.

**Rating: PARTIAL.**

## 9. Risk-engine resilience

Reviewed: `risk/engine.py`, `risk/policies.py`, `risk/store.py`.

### Fail-closed behavior verified

- Missing timestamped price → `INSUFFICIENT_DATA`, never approval.
- Stale price beyond policy limit → hard `stale_market_data` breach → `REJECTED`.
- Missing portfolio/valuation state → insufficient data.
- Missing volatility/liquidity observations → insufficient data; an adding action cannot proceed without liquidity.
- Restricted instrument → hard rejection.
- Excessive concentration, cash reserve, drawdown and capital request → policy-specific rejection/restriction/review.
- Generic assessment failure → `_failure_decision` with `INSUFFICIENT_DATA`; execution cannot consume an approval.
- Execution revalidates the actual order notional and refuses stale data.
- A risk service/database interruption becomes a failed assessment, not an approval; the execution path catches the failure and does not create an order.
- Duplicate proposals reuse the risk decision/execution idempotency instead of creating a second order.

### Tests

`tests/test_novamarket_risk.py` covers known drawdown, zero/insufficient volatility, concentration, restricted instruments, stale data, hard-breach precedence and API status/404 behavior. `tests/test_novamarket_execution.py` covers stale-data refusal, restricted risk decisions and actual-notional revalidation.

### Limitations

- No live risk service was killed during this audit; the failure path is unit/integration tested and the wallet lock drill demonstrates the database boundary.
- Risk is process-local; a stalled process delays approval, but a stalled process cannot silently approve.

**Rating: PASS.**

## 10. Reinforcement-learning isolation

Reviewed: `rl/`, `adaptive/`, `rewards/`, `evaluation/rl_strategy/`, and the service's RL/learning surfaces.

### Isolation verified

- The RL environment has no execution engine, wallet engine or order handle. It produces observations, actions and simulated transitions; it does not place orders.
- `automatic_policy_promotion=false` is exposed in RL and adaptive payloads.
- There is no policy activation/promotion/execute route in the RL surface; the audit did not find a path from an experimental policy to NM-10.
- Model/registry records are versioned; no automatic promotion was performed by this audit.
- The cash-drag experiment remains experimental; no policy was promoted and the default reward was not changed.
- Training/evaluation separation is chronological; the test window is not used to select the policy.
- The risk engine remains the sole risk authority; RL may propose but cannot set risk limits.

### Evidence

- Prior RL report: all ten freshly trained policies made zero trades; the result is a negative finding, not a trading success.
- `tests/test_novamarket_pi14.py` and `test_novamarket_pi15.py` cover reward/experiment isolation and training/evaluation boundaries.
- `test_no_activation_route_exists` and `automatic_policy_promotion=false` assertions are the current guard.

### Limitations

- The RL policy is degenerate (zero trades); isolation is strong but the policy is not useful.
- Adaptive retraining/rollback paths are tested by their own suites; no live promotion was exercised.

**Rating: PASS** for isolation; the experimental policy itself remains failed/insufficient.

## 11. Resource and long-running stability

### Measured evidence

- Prior baseline: uvicorn RSS approximately 231–288 MB per process; data directory about 100 MB; primary DB 58 MB, RL DB 35 MB.
- Current isolated copy: primary store 60.1 MB / 127 tables / 62,408 rows; RL store 35.8 MB / 25 tables / 5,217 rows.
- Prior decision latency: baseline EMA 0.181 ms mean / 0.235 ms p95; enhanced multi-timeframe 8.398 ms / 11.411 ms (under 12 ms p95 in that environment).
- Prior full-suite wall time: about 12 minutes. The 23 November single-process run completed in 514.43 s but exited 1 on one date-sensitive scheduler fixture (see §15).

### What was not measured

- No multi-hour soak, no sustained API load, no CPU sampling (psutil is absent), no GPU use, no model-loading benchmark in this audit, no heap-growth trace and no database-growth projection.
- The committed execution journal grows with every fill; no retention job exists.
- The main DB is 60 MB and the RL DB 35 MB; these are modest for the current data, but no long-term growth bound is enforced.

### Stability incident

The historical single-process `pytest tests/test_novamarket_*.py` run ended with exit code 1 at roughly 35%, with only progress dots and no failure summary. The last collected tests in that range are LSTM tests. Running `tests/test_novamarket_lstm.py` alone passed (24 passed). This is consistent with a test-process resource/native crash, not a test assertion failure; it is still an unresolved stability limitation and the reason the suite was rerun in groups. A 23 November rerun completed without reproducing that crash, but it still exited 1 on a separate date-sensitive scheduler fixture. Do not report the single-process suite as green.

**Rating: PARTIAL.** Short-run behavior is acceptable; long-running stability is not proven.

## 12. Observability and audit logging

### Present

- Durable tables for market observations/bars, provider health, retrieval log, quality events, risk decisions/alerts, execution orders/transitions/fills/reservations, ledger transactions/entries, portfolio decisions/valuations/positions, forecasts/checkpoints and autonomy leases.
- `GET /status`, `/health`, `/system/health`, `/system/telemetry`, `/market/status`, `/market/risk/status`, `/market/execution/status`, `/market/autonomy/status`, `/market/multi-timeframe/status` and the event bus/SSE channel.
- Event-bus events for risk, execution, wallet, forecast and scheduler paths; request logging with method/path/status/latency.
- Wallet reconciliation logs counts and ledger sum; provider errors are normalized.

### Gaps

- Several API 500 paths return raw exception strings; operators see internal details in the response.
- No unified metrics endpoint for CPU/RSS/queue depth/writer lock wait; process CPU was not sampled.
- No log rotation/retention policy was inspected.
- No single correlation id was verified across market → forecast → risk → execution; the individual records carry ids, but a full trace was not demonstrated.
- The current execution journal has no retention/pruning policy.

**Rating: PARTIAL.**

## 13. Disaster-recovery results

| Scenario | Method | Result |
|---|---|---|
| Application restart | Existing persistence/restart and scheduler restart tests; copied store reopen | PASS for stores/checkpoints; no full API process kill during a live cycle. |
| Database unavailable | Real SQLite write lock on a temporary ledger; separate connection held `BEGIN IMMEDIATE` | PASS — deposit failed after 5.585 s, no balance change, no transaction. |
| Market provider unavailable | Provider retry/timeout/circuit tests; prior continuous-paper provider contract | PASS at test/contract level; no live provider outage. |
| Scheduler interruption | Atomic claim, lease recovery, duplicate-tick and restart tests | PASS. |
| Worker crash | In-flight fill journal and `test_pending_fill_is_reversed_after_restart_recovery` | PASS at unit/integration level. |
| Incomplete transaction | Wallet/position compensation tests (`test_wallet_commit_before_position_failure_is_compensated`, position-before-execution-write) | PASS. |
| Corrupted backup | Overwrote bytes in an isolated backup copy; `PRAGMA integrity_check` | PASS — corruption detected, not restored. |
| Invalid configuration | Unknown-field/secret-shaped-key/schema-version tests | PASS. |
| Network interruption | Provider timeout/retry tests; no live network partition | PARTIAL. |
| Unexpected shutdown | Restart/lease recovery and journal recovery; no OS-level kill during a live paper lifecycle in this audit | PARTIAL. |

No financial records were deleted, no production/user database was mutated and all recovery drills used copies or temporary stores.

## 14. Fixed defects

### F-1 — Nested portfolio transactions raised instead of joining the outer transaction

**Where:** `src/novacontrol/novamarket/portfolio/store.py`  
**Symptom:** `with store.transaction(): with store.transaction(): ...` raised `sqlite3.OperationalError: cannot start a transaction within a transaction`. The docstring promised that inner calls do not commit, but the implementation issued a second `BEGIN IMMEDIATE`.  
**Fix:** Inner blocks use `SAVEPOINT portfolio_tx_<depth>`; success releases the savepoint, an inner failure rolls back only that savepoint, and the outer block commits/rolls back once.  
**Regression tests:** `PortfolioTransactionBoundaryTests` in `tests/test_novamarket_portfolio.py` — nested commit and inner-failure isolation.  
**Verification:** `tests/test_novamarket_portfolio.py` → 51 passed; mypy on the changed store → clean; API reference unchanged at 935 routes.

### F-2 — A signal with no reference price raised `TypeError` during proposal sizing

**Where:** `src/novacontrol/novamarket/strategies/engine.py`  
**Symptom:** `StrategySignal.reference_price` is optional, but `propose()` and `_notional()` multiplied it by a position quantity. A signal with `reference_price=None` therefore raised `TypeError: unsupported operand type(s) for *: 'Decimal' and 'NoneType'` (also reported by mypy at the two multiplication sites) instead of refusing the proposal.  
**Fix:** `propose()` now resolves the price once, rejects `None` or non-positive values with `StrategyValidationError`, and passes the validated `Decimal` into `_notional()`.  
**Regression test:** `test_a_signal_without_a_positive_reference_price_fails_closed` in `tests/test_novamarket_strategies.py`.  
**Verification:** `tests/test_novamarket_strategies.py` → 43 passed; full `mypy src` → clean.

No other code defect was confirmed strongly enough to change behavior. The remaining items are hardening/limitations, not silently patched.

## 15. Test results and verification

### Focused and structural checks

| Check | Result |
|---|---|
| `pytest tests/test_novamarket_portfolio.py -q` | **51 passed** (including 2 new transaction-boundary tests) |
| `pytest tests/test_novamarket_lstm.py -q` | **24 passed** |
| `pytest tests/test_novamarket_strategies.py -q` | **43 passed** (including the missing-reference-price regression) |
| `mypy src` | **clean** (the two optional-price multiplication errors are gone) |
| `mypy src --platform win32` | **clean** |
| `mypy src/novacontrol/novamarket/portfolio/store.py` | **clean** |
| `python scripts/generate_api_reference.py --check` | **in sync (935 routes)** |
| Database integrity on 14 non-empty SQLite files | integrity `ok`, 0 declared-FK violations |
| Primary backup → restore | 127 tables / 62,408 rows preserved, integrity `ok` |
| Ledger reconciliation on isolated copy | 17 wallets, 40 entries, 20 transactions, sum `0.00`, 0 issues |
| Wallet write-lock drill | failed closed at 5.585 s; no mutation |
| Auth/CORS smoke | 401 wrong token, 403 foreign Origin, 200 with valid token, security headers present |
| Corrupted-backup detection | integrity failure detected |

### Full regression status

#### 23 November 2026 rerun (current)

The current tree collected **1,972 tests** and was rerun in the same ordered split. **Group A** (21 files through `test_novamarket_lstm.py`) passed **697 tests and 390 subtests** in 349.67 s. **Group B** (35 files) produced **1,272 passed, 1 failed, 2 skipped and 57 subtests passed** in 231.97 s. The **single-process** pass completed all 1,972 tests in 514.43 s without the historical LSTM crash: **1,969 passed, 1 failed, 2 skipped and 447 subtests passed**. The one failure is `tests/test_novamarket_scheduler.py::test_api_exposes_cycle_scheduler_and_missed_surfaces`: it selected 24 November 2026, a verified NSE holiday, and expected 200 where the API correctly returned 422. It fails identically in isolation. This is a date-sensitive test fixture, not a product execution or financial-control failure. The completed single-process run does not prove the runner stable; the suite is not green. Evidence: `tmp/ops_audit/current_novamarket_suite_record.json`, with raw logs `tmp/ops_audit/pytest_current_group_a.log`, `pytest_current_group_b.log`, `pytest_current_full_single_process.log` and `pytest_current_scheduler_isolated.log`.

#### 22 November 2026 run (historical)

- `pytest tests/test_novamarket_*.py -q` was attempted once. It collected **1,971 tests** (before the F-2 regression test was added) and exited **1** at about 35% with **no pytest failure summary**; the log is `tmp/ops_audit/pytest_novamarket.log`. The last collected block was the LSTM file; that file passes alone.
- Ordered file-group reruns are recorded in `tmp/ops_audit/pytest_group_a.log` and `tmp/ops_audit/pytest_group_b.log` (group B was rerun after the F-2 fix). Group A: 697 passed. Group B: 1,273 passed, 2 skipped. Combined: 1,970 passed of 1,972 currently collected, 0 failed.
- No test was skipped, weakened or removed to make this report pass.

**Group A result:** **697 passed, 0 failed, 0 skipped, 390 subtests passed** in 317.13 s (`tmp/ops_audit/pytest_group_a.log`).  
**Group B result:** **1,273 passed, 0 failed, 2 skipped, 57 subtests passed** in 462.49 s (`tmp/ops_audit/pytest_group_b.log`, rerun after the F-2 fix).  
**22 November combined interpretation:** the two ordered groups covered all **1,972 collected tests**: **1,970 passed, 0 failed, 2 skipped and 447 subtests passed**. The two skips are the existing optional-Node renderer tests. The single-process crash remains a historical test-process stability finding, not an assertion failure; the 23 November attempt completed but did not reproduce it.

### Checks not run

- `ruff` is not installed in the project virtual environment; no lint result is claimed.
- `pip-audit` / `safety` are not installed; no dependency-vulnerability result is claimed.
- No multi-hour soak or live-provider/live-broker test was run.
- No line/branch coverage measurement was taken.

## 16. Remaining vulnerabilities and limitations

1. **Authentication is opt-in.** With no API token, every local caller is principal `local`. Keep the service loopback-only.
2. **Predictable local admin key.** `start.ps1` defaults `NOVACONTROL_ADMIN_TOKEN` to a fixed string. Set a private value on a shared machine.
3. **Raw 500 error text.** Several handlers return `str(exc)`; use a generic client message plus a server-side correlation id.
4. **SSE query token.** EventSource forces `?token=`; use a short-lived ticket if the API is ever exposed beyond loopback.
5. **Single process / single SQLite file.** No HA, replication or multi-writer support.
6. **No automated backup/reconciliation schedule.** The backup and reconciliation drills were manual.
7. **No dependency vulnerability scan.** Install and run `pip-audit` in CI before deployment.
8. **No live validation.** Market data is fixture/live-limited; no live broker or live paper lifecycle.
9. **RL policy is degenerate.** Isolation is good, but the trained policies made zero trades; do not present them as trading skill.
10. **Long-running stability is unproven.** No multi-hour RSS/CPU/DB-growth/queue-depth run; the single-process suite crash remains unexplained.
11. **Execution journal retention.** Committed in-flight rows are retained indefinitely; define retention without deleting financial evidence.
12. **Timestamp semantics.** Simulated market timestamps and wall-clock transition timestamps can differ in replay; label them explicitly in operator views.
13. **Calendar coverage.** 2027 exchange holidays are not verified; the system fails closed outside coverage.

## 17. Operational readiness

**Controlled paper trading:** **PARTIAL / ready with limitations.** The risk, execution, ledger, scheduler and database safety controls are present and tested; the transaction-boundary defect is fixed; ledger and database recovery drills pass. Before relying on it for a long controlled run, set private tokens, keep the API loopback-only, define a backup/retention procedure, and run a multi-hour soak with real provider or recorded live data.

**Live trading:** **BLOCKED.** No live broker, live order route, live market session or regulatory/operational approval exists. Nothing in this audit changed that.

**No PI-25 was created. The completed PI-01–PI-24 roadmap was not modified. No commit or push was performed.**
