# NM-09 Performance Evaluation

Performance is measured from the first market bar observed at or after each signal's configured step horizon. It never uses today's price as a proxy for a historical outcome, and it is explicitly signal-quality evidence, not executed-trade performance (`is_executed_performance: false`).

The engine counts observations, actionable signals, wins, losses, flat outcomes, win rate, average return, profit factor, drawdown, Sharpe and total compounded return. It reports `insufficient_evidence` below the configured minimum sample, and `healthy`/`degraded` only when the sample is sufficient. No synthetic fill, fee, slippage or P&L is invented.

Each evaluation publishes `novamarket.strategy.performance.updated` with the strategy id, observation count and degradation. The report is exposed at `GET /market/strategies/{id}/performance` and never rewrites an existing proposal's risk decision.
