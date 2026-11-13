# NM-07 — Performance Metrics

Performance is reported per portfolio from stored snapshots. Ratios are gated:
a metric that cannot be measured honestly is returned as absent, with the reason,
rather than as a number computed from three observations.

## Metrics

| Metric | Definition | Gate |
| --- | --- | --- |
| `return_pct` | `(total_value - initial_capital) / initial_capital × 100` | always |
| `realized_pnl` / `unrealized_pnl` | book values | always |
| `drawdown_pct` | current total vs peak stored total | always |
| `observations` | number of valuation snapshots | always |
| `sharpe` | annualised mean excess return / volatility | ≥ `min_performance_observations` (20) |
| `sortino` | mean excess return / downside deviation | ≥ 20 and at least one negative observation |
| `volatility` | annualised standard deviation of period returns | ≥ 20 |
| `turnover` | traded notional / total value across applied proposals | always (last apply) |
| `cash_drag` | cash weight × estimated idle return | reported as an estimate, labelled |

`spacing_hours()` measures the actual interval between snapshots so an
annualisation factor is derived from the data, not assumed to be daily.

## Why the gate

With fewer than 20 observations, a Sharpe ratio is noise dressed as precision.
The engine therefore returns `sharpe: null` and an explanatory note. Tests pin
this: a fresh portfolio with one snapshot reports zero or null ratios, never a
fabricated figure.

## External flows

Funding and release are capital moves, not returns. `performance()` reports
`external_flows` and a `flow_note` so a deposit cannot be mistaken for
investment performance. A time-weighted return requires a complete flow series;
until enough snapshots exist, the raw return is reported and the caveat is
stated.

## Drawdown

Drawdown is computed against the maximum of the initial capital and every stored
total value, using the append-only history. Because snapshots cannot be updated
or deleted, a past peak cannot be erased. The value is a percentage, reported
negative when the book is below its peak.

## Turnover and cash drag

Turnover is measured from the applied proposal (`sum(notional) / total_value`),
not estimated from the desired weights. Cash drag is reported as an explicit
estimate with its assumption named; it is never presented as a realized cost.

## Not reported

- No benchmark-relative alpha or beta: no benchmark series is wired in NM-07.
- No risk-adjusted score for a single position: NM-08 owns factor risk.
- No win rate from live trades: there are no live trades, only simulated book
  entries.
- No fees or taxes: `fee_bps` is zero in this phase.
