# NM-11 Architecture — Reproducible Backtesting

## Status

Implemented and verified. `backtesting/core.py` owns replay and persistence;
`backtesting/service.py` owns configuration, NM-02 loading and the run
lifecycle. Twenty authenticated `/market/backtests…` routes expose the
subsystem, including persisted comparisons and walk-forward evaluation.
Permissions are `market.backtest.read|run|compare` with three capabilities.

## Reuse (no duplicate subsystems)

| Subsystem | Reused surface |
|---|---|
| NM-02 market data | `MarketDataEngine.bars()` / `MarketDataStore.bars()`, `ExchangeCalendar` sessions and holidays |
| NM-04 events | point-in-time event loader |
| NM-06 forecasts | point-in-time forecast loader with model/cutoff filters |
| NM-07 portfolios | `Position` (quantity, average cost, realized P&L, mark, `cost_basis`, `market_value`) |
| NM-08 risk | `RiskPolicy` snapshot and `evaluate_policy` for every entry decision |
| NM-09 strategies | catalog evaluators, `StrategyContext`, `validate_config` |
| NM-10 execution | `ExecutionConfig`, `execution_price`, `fee_breakdown`, `exits_limit`, `MarketSnapshot` |

The replay keeps an isolated simulated book; it never touches a production
wallet, ledger, portfolio, broker or order gateway.

## Components

- `BacktestConfig` — frozen, hashed configuration including the full NM-08
  policy snapshot, NM-10 execution assumptions and order/TTL settings.
- `HistoricalBar` — validated OHLCV with observation/availability timestamps,
  provenance, `tradable` and `adjusted`.
- `normalise_bars` — period filtering, chronology, duplicate removal.
- `BacktestEngine` — point-in-time replay, next-bar execution, `Position`
  book, NM-08 policy decisions, metrics, benchmark/regimes, walk-forward and
  full replay verification.
- `BacktestStore` — immutable completed runs (bars, orders, fills, risk
  decisions, signals, metrics, warnings) and immutable comparison/sensitivity/
  walk-forward records.
- `BacktestingService` — lifecycle, NM-02 pagination, event/forecast/policy
  loaders, comparison persistence and event publication.

## Guarantees

- Decisions at T use only observations available by T.
- Orders execute from the next bar at the earliest; liquidity uses the
  previous bar's volume.
- Exchange holidays and weekends are excluded for a known MIC; missing bars
  are absent sessions, and `tradable=false` is a halt.
- Completed runs and comparisons are immutable (SQLite triggers).
- `verify()` replays persisted bars and recomputes the reproducibility hash.
- Every result is marked `"simulated": true`; no live order is placed.
