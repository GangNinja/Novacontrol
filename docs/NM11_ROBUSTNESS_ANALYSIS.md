# NM-11 Robustness Analysis

## Implemented

- **Regime segmentation** — each portfolio return is mapped to the latest
  benchmark 20-observation trend/volatility label (bull, bear, sideways; high
  or low volatility), with observations, return, win rate, volatility and
  drawdown per regime. Without benchmark bars returns are `unclassified`.
- **Benchmark comparison** — buy-and-hold return, drawdown, volatility, Sharpe
  and excess return beside the strategy.
- **Parameter sensitivity** — variant configurations over one historical window,
  each marked `out_of_sample: false` with a warning.
- **Walk-forward** — sequential train/test folds. Each fold replays training
  history and measures the test slice only; fold records carry
  `out_of_sample: true`, plus aggregate mean return, positive folds and
  consistency. Exposed as `POST /market/backtests/{id}/walk-forward`.
- **Persisted comparisons** — compare, sensitivity and walk-forward records are
  immutable and read back through `GET /market/backtests/comparisons`.
- **No ranking by final value** — comparisons report metrics side by side.

## Limitations

- No Monte Carlo or bootstrap resampling.
- No multi-window or regime-conditional parameter selection.
- Walk-forward folds share one instrument history; the warning in every
  response says so. Results are historical mechanism evidence, not a promise
  of future performance.
