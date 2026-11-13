# NM-12 Shadow Mode — Architecture and Sessions

## Purpose

Shadow Mode observes live market data and records what NovaMarket *would have
done*. It produces strategy signals, risk assessments, hypothetical order
projections and hypothetical outcomes without submitting an order, filling a
paper order, moving NovaCoin, reserving capital, or changing a portfolio.

Shadow Mode is a separate data boundary: `shadow.sqlite3` (or the configured
shadow path) with its own session, decision, projection, outcome, error and
comparison tables. It has no wallet, ledger, portfolio, execution or broker
gateway.

## Reuse

| Component | Reused surface |
|---|---|
| NM-02 | `MarketDataEngine.latest()` / `.bars()` and `MarketDataStore` |
| NM-07 | portfolio `detail()` for the baseline snapshot (read-only) |
| NM-08 | `RiskPolicy`, `RiskSnapshot`, `evaluate_policy` |
| NM-09 | `EVALUATORS`, `StrategyContext`, `validate_config` |
| NM-10 | pure `execution_price`, `fee_breakdown`, `MarketSnapshot` assumptions |
| NM-11 | completed backtest results for comparison |

No second strategy, risk or pricing implementation exists in `shadow/`.

## Pipeline

1. A cycle receives a timestamped NM-02 observation (or an explicit observation
   in a test/operator request).
2. Freshness, quality, positivity and timestamp checks run. Stale, missing,
   invalid or future-dated data produces a structured data failure and no new
   decision for that instrument.
3. The immutable baseline snapshot captured at session start supplies holdings,
   cash and total value.
4. Each selected NM-09 evaluator runs against a `StrategyContext` built only
   from bars whose `observed_at` and `available_at` are at or before the decision
   time.
5. Actionable signals become proposals. BUY size is `notional_fraction` of the
   baseline (default 5%, capped at 25%); SELL/EXIT require a baseline holding.
6. NM-08 `evaluate_policy` evaluates the proposal against the selected policy
   version. Only `approved` proceeds; restricted, review, insufficient-data and
   rejected outcomes are stored as `risk_rejected` (or `blocked` when the
   evaluator itself failed).
7. Approved proposals become **hypothetical order projections** using NM-10's
   pure pricing and fee functions. A projection has no execution order id and is
   labelled `hypothetical order projection`.
8. Later cycles/outcome sweeps measure only observations after the decision
   timestamp: return, P&L, MFE, MAE, configured intervals, expiry and unresolved
   status.

## Session lifecycle

```
created -> running -> paused -> running
   |         |          |
   |         +----------+--> stopped --> completed
   |         |          |
   +---------+----------+--> cancelled
              \------------> failed -> cancelled
```

`start` accepts `created` or `paused`; `resume` accepts only `paused`; `pause`
only `running`; `stop` accepts `running` or `paused`; `complete` accepts
`running`, `paused` or `stopped`; `cancel` accepts every non-completed state.
An unlisted edge raises `ShadowStateError` (HTTP 409).

When `auto_monitor` is true (the default), `start`/`resume` schedules a bounded
interval loop that runs cycles and outcome sweeps until the session is paused,
stopped, completed or cancelled. A manual `POST …/cycle` is available for
deterministic operation and tests.

At `start` the service captures a `PortfolioSnapshot` once. Later changes to the
live portfolio never rewrite it; a new baseline requires a new session. If the
baseline cannot be captured the session enters `failed`, records the reason and
does not monitor. The resolved strategy definitions and risk policy are frozen
in the session at the same point: editing a strategy or policy afterwards does
not change an already-running session.

## Configuration

`POST /market/shadow/sessions` accepts:

- `portfolio_id` or an inline `baseline_snapshot`
- `instruments` (at least one)
- `strategies` (IDs resolved through NM-09, or inline `{strategy_id,
  catalog_id, version, config, horizons}` definitions)
- `horizons`
- `market_data_source` (the NM-02 provider used for both the latest quote and
  bar history; the default `nm02` accepts the reconciled cross-provider view.
  Explicit cycle observations bypass the provider)
- `start_at` / `end_at`
- `monitoring_interval_seconds` (default 60)
- `risk_policy_version`
- `observation_intervals` (default `[1, 5, 20]`)
- `description` and `metadata`
- `auto_monitor` (default true)

`notional_fraction` and `outcome_horizon_days` live in `metadata` and are
validated/defaulted (5% and 30 days).

## Persistence and recovery

- Sessions are mutable lifecycle records.
- Decisions, projections and comparisons are append-only (SQLite triggers
  refuse UPDATE/DELETE); their idempotency key is
  `(session, strategy, version, instrument, observation timestamp, signal)`.
- Outcomes are upserted per `(decision, interval)` as later observations arrive;
  unresolved and expired outcomes are retained.
- Errors are append-only structured records.
- On construction after a restart, a session that was `running` is moved to
  `paused` with an explicit "application restarted" reason. It does not silently
  resume live observation; an operator resumes it deliberately.

## Failure handling

- Provider/data failures are recorded and stop new decisions for the affected
  instrument.
- A failed strategy evaluation records `strategy_failed` and creates a blocked
  decision, never an order.
- A failed risk evaluation records `risk_failed` and marks the decision blocked;
  it is never approval. A policy that cannot be parsed is such a failure; it is
  never replaced with defaults.
- Duplicate cycles/observations are idempotent; duplicate rows are counted, not
  written twice.
- A failure in one session does not touch another session, the wallet, the
  ledger, paper portfolios or execution state.
