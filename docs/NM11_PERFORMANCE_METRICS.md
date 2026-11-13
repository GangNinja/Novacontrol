# NM-11 Performance Metrics

`calculate_metrics` returns an unavailable result with a reason when fewer than
two equity observations exist. Otherwise it calculates: initial and final
value, total and annualized return, cumulative and periodic returns,
maximum/current drawdown, annualized volatility, Sharpe, Sortino, Calmar, 5%
historical VaR/CVaR, trade count, closed trades, wins, losses, win rate,
average win/loss, profit factor, average holding days, turnover, trade
frequency, total fees and total slippage.

The equity series has one observation per timestamp. Multi-instrument
portfolios are valued with each instrument's last known close; using the
current instrument's price for the others would misstate equity. A synthetic
initial observation at the start timestamp makes the first return meaningful.

The replay adds execution metrics: rejections, rejection rate, order count,
filled/partial/unfilled orders, fill rate and partial-fill rate. `data_limited`
marks a replay with fewer than two equity observations.

`benchmark_metrics` computes a buy-and-hold comparison from the benchmark bars
(no fees or slippage) and `excess_return` versus the strategy. `regime_analysis`
segments portfolio returns by benchmark 20-observation trend/volatility bands
(bull/bear/sideways, high/low volatility), and reports `unclassified` when no
benchmark is supplied. Regime labels are descriptive and not predictive.

Sharpe and Sortino are `null` when their denominator is zero or observations
are insufficient. Annualization uses 252 periods and is only meaningful for
daily data. Results are historical simulation, never live performance.

## Limitations

- No independent numerical cross-check against a third-party backtester.
- Benchmark comparison is buy-and-hold only; no risk-free rate series.
- Regime labels are descriptive, not a regime-switching model.
- Walk-forward/out-of-sample optimisation is not implemented.
