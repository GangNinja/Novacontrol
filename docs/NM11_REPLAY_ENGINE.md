# NM-11 Replay Engine

Replay uses a sorted `(timestamp, instrument_id)` timeline. Ties are broken by
instrument id, giving a deterministic order. For each observation:

1. reject an unavailable bar;
2. reject a bar whose NM-02 calendar says the exchange is closed;
3. execute a pending order for that instrument, if any, at the current bar;
4. append the closed bar to that instrument's history;
5. build a strategy context from history only (events and forecasts filtered to
   the decision time);
6. evaluate the selected NM-09 catalog evaluator;
7. ask NM-08 `evaluate_policy` for the entry, then queue the order for the next
   bar;
8. value the whole portfolio once per timestamp.

Execution prices at the bar's open use the **previous** bar's volume and
volatility for the liquidity/slippage model. Portfolio equity is written once
per timestamp; later instruments overwrite the same moment, leaving the
completed portfolio state. Positions are NM-07 `Position` objects, so the mark,
cost basis and realized P&L are owned by the portfolio model rather than a
second accounting table.

A pending order is instrument-scoped. A risk rejection, a halted bar, a closed
session or an unavailable bar affects only that instrument's action. Orders
still pending at the end are expired and counted as unfilled.

## Lifecycle

- **Checkpoint** — `novamarket.backtest.replay.checkpoint.created` records the
  final observation and the number of equity observations.
- **Cancellation** — a created row is marked `cancelled`; an in-flight replay
  checks the flag at every observation and aborts. A completed run cannot be
  cancelled.
- **Expiry** — `order_ttl_bars` is counted in observations, not calendar days.
- **Halts/closures** — `tradable=false` warns and skips the decision; a holiday
  or weekend date from NM-02's calendar is excluded entirely.

## Walk-forward

`walk_forward(config, bars, train_bars, test_bars)` advances a sequential
window: each fold replays the training history plus its test slice in one
point-in-time run, then reports metrics computed only on the test slice with
`out_of_sample: true`. Folds with insufficient history are reported as skipped,
not silently dropped. Results are persisted as an immutable comparison record.

## Limitations

- No resume-from-checkpoint support; the checkpoint is an event, not a restart
  point.
- No intraday frequencies.
- No Monte Carlo/bootstrap resampling.
