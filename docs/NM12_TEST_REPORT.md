# NM-12 Shadow Mode — Test and Verification Report

## 1. Implementation summary

NM-12 is implemented as a separate, non-executing shadow subsystem. It opens
its own SQLite boundary, captures an immutable portfolio baseline at session
start, observes NM-02 data, evaluates selected NM-09 strategies, sends every
actionable proposal through NM-08 `evaluate_policy`, records hypothetical NM-10
priced projections, and measures later outcomes and analytics. It has no
execution, wallet, ledger, portfolio-mutation or broker path and does not
activate NM-13.

## 2. Files and modules

Created:

- `src/novacontrol/novamarket/shadow/__init__.py`
- `src/novacontrol/novamarket/shadow/models.py`
- `src/novacontrol/novamarket/shadow/store.py`
- `src/novacontrol/novamarket/shadow/guards.py`
- `src/novacontrol/novamarket/shadow/service.py`
- `tests/test_novamarket_shadow.py`
- `docs/NM12_SHADOW_MODE.md`
- `docs/NM12_SAFETY.md`
- `docs/NM12_ANALYTICS.md`
- `docs/NM12_API.md`
- `docs/NM12_TEST_REPORT.md`

Modified:

- `src/novacontrol/novamarket/service.py` — lifecycle wiring, status/contracts,
  shutdown, 22 methods
- `src/novacontrol/core/events.py` — 18 typed NM-12 events and payload contracts
- `src/novacontrol/novamarket/events.py` — implemented-event registry; removed
  the NM-12 future placeholder
- `src/novacontrol/novamarket/permissions.py` — four shadow permissions
- `src/novacontrol/novamarket/contracts.py` — phase `NM-12`
- `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py` — 22
  routes
- `tests/test_novamarket.py`, `tests/test_novamarket_api.py`,
  `tests/test_novamarket_wallet.py` — phase/event/route/capability contracts and
  the HTTP shadow lifecycle
- `docs/API.md` — regenerated

## 3. Reused components

NM-02 `MarketDataEngine`/`MarketDataStore`, NM-04 event contracts, NM-06
forecast types (available to strategy context), NM-07 portfolio `detail()`,
NM-08 `RiskPolicy`/`RiskSnapshot`/`evaluate_policy`, NM-09 `EVALUATORS` and
`StrategyContext`, NM-10 pure pricing/fee functions, NM-11 completed backtest
results. No strategy, risk or pricing implementation was duplicated.

## 4. Session lifecycle

Implemented: create, start, pause, resume, stop, complete, cancel, get, list and
history; unique ids; explicit states; invalid edges raise 409. A session
captures its baseline once at start; later portfolio changes do not rewrite it.
Restart recovery moves a running session to paused with a recovery reason;
created/paused/stopped records are preserved. Session start also freezes the
resolved strategy definitions and the risk policy, so a later strategy or
policy edit cannot silently change an already-running session.

Shadow data is stored in `novamarket_shadow.sqlite3`, a dedicated file beside
the market-data database (or in memory when no data directory exists). It is
never the market, wallet or ledger file.

## 5. Shadow decision pipeline

Observe → freshness/quality gate → baseline snapshot → NM-09 evaluation →
NM-08 policy → NM-10 pure projection → later observations → analytics. Every
decision stores the strategy version, NM-02 reference, portfolio snapshot id,
risk-policy version, raw risk outcome and latency. Actionable BUY/SELL/EXIT
signals create proposals; HOLD/NO_SIGNAL are recorded without a projection.
Duplicate observations are idempotent by a deterministic key. A configured
`market_data_source` selects the NM-02 provider for both the latest quote and
the bar history; explicit observations bypass the provider. A policy that
cannot be parsed blocks the decision instead of silently falling back to
defaults.

## 6. Execution isolation

`ShadowExecutionGuard` denies submit/execute/place/process/reserve/debit/credit.
Every projection is checked for `simulated=true`, no execution order id and the
`hypothetical order projection` label. The service holds no execution engine and
no wallet/ledger gateway. The API exposes no live/broker route. Tests assert the
guard, projection shape, no portfolio mutation, and unchanged wallet/portfolio
status across the HTTP lifecycle.

## 7. Outcome tracking and analytics

Outcomes use only observations strictly after the decision timestamp, with
explicit bars filtered by `observed_at` and `available_at`; future-dated
observations are rejected. Return, hypothetical P&L, signed MFE/MAE, configured
intervals, unresolved and expired statuses are stored. Analytics cover decision
and signal distribution, risk approval/rejection, projections, outcomes,
strategy/instrument/horizon breakdowns, rejection reasons, data failures and
latency. `hypothetical` is explicit and small samples report
`sample_sufficient=false` with no win rate. The sample gate counts distinct
decisions, not the several interval rows one decision produces.

## 8. API and event integration

22 authenticated routes cover status, contracts, session CRUD/lifecycle, cycle,
outcome sweep, decisions, projections, outcomes, analytics, errors, health,
comparison and persisted comparisons. 18 typed events are registered with
payload contracts. Four permissions/capabilities were added; the permission
model still denies live trading. Market route count: **177**; capabilities:
**43**; implemented events: **151**.

## 9. Automated test results

- `tests/test_novamarket_shadow.py`: **34 passed, 7 subtests**.
- `tests/test_novamarket_api.py` including the HTTP shadow lifecycle: **42
  passed, 185 subtests**.
- Combined NM-12 + API + core + wallet: **239 passed, 267 subtests**.
- `mypy src`: clean, **541 source files**.
- `mypy src --platform win32`: clean.
- `generate_api_reference.py --check`: `docs/API.md` in sync.
- `git diff --check`: clean.

The real-integration test registers an NM-02 instrument, appends `OhlcvBar`
rows, runs the real NM-09 evaluator and real NM-08 `evaluate_policy`, and
asserts the decision's data source is `nm02` with a persisted risk-policy
version.

## 10. Live-data verification

No live provider feed/credentials were configured in this harness, so a live
provider session was not claimed. A direct check of the existing Yahoo chart
endpoint returned **HTTP 429 (rate limited)**, so it could not serve as a live
feed here. Verification used deterministic observations for behavior and the
real NM-02 `MarketDataStore` for the integration path. A live run requires a
configured NM-02 provider; the same `ShadowService` observes through
`MarketDataEngine.latest()`/`.bars()` with no code change.

## 11. Known limitations

- Daily bars/latest quotes; no intraday or order-book depth.
- Projections use the disclosed NM-10 synthetic spread/impact model, not a
  real book.
- No live provider was exercised in this environment.
- Outcome intervals are bar-count based; expiry is calendar-day based.
- Comparison with NM-10 behavior relies on supplied reference evidence; there is
  no direct paper-order reader in the shadow boundary (deliberately, to avoid
  coupling to execution state).
- No Monte Carlo/bootstrap analytics.

## 12. Remaining defects

No known defects in the tested scope. The verification pass added tests for
the dedicated shadow database path, frozen strategy and policy selection,
provider forwarding for quotes and bars, invalid-policy blocking, distinct
sample counting, `data_mismatch` comparison evidence, and append-only decision,
projection and comparison tables.

Unverified requirement: a live provider session. The public Yahoo chart
endpoint returned HTTP 429 in this environment, so live data is the one item
that was not exercised; deterministic observations and the real NM-02 store
cover the integration path. A live run needs a configured NM-02 provider and
uses the same `MarketDataEngine` calls with no code change.

## 13. Final status

**PASS** for the implemented and tested scope, with the live-provider limitation
stated in §10. Shadow sessions are non-executing end to end; strategy, risk and
pricing components are reused; projections and outcomes are hypothetical;
wallet, ledger, paper portfolio and execution state are untouched; tests,
mypy, route/event contracts and documentation are updated.
