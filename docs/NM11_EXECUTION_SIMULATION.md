# NM-11 Execution Simulation

NM-11 reuses `ExecutionConfig`, `execution_price`, `fee_breakdown` and
`exits_limit` from NM-10. Execution occurs from the next bar onward, never at
the signal close. The reference price is the bar's open; stop and limit
triggers use the bar's high/low.

## Order types

- **Market** — fills at the bar open plus NM-10 slippage/fees.
- **Limit** — a buy needs `low <= limit` and fills at `min(open, limit)`; a
  sell needs `high >= limit` and fills at `max(open, limit)`.
- **Stop** — a buy triggers when `high >= trigger` and fills from
  `max(open, trigger)`; a sell triggers when `low <= trigger`.
- **Stop-limit** — the stop must trigger first, then the limit condition is
  applied to the same bar.

## Risk decisions

Every entry is evaluated by **NM-08's `evaluate_policy`** against the policy
snapshot recorded in the configuration. The replay builds a `RiskSnapshot` with
current and projected weights, cash weight, invested fraction, drawdown,
volatility, liquidity and instrument sector. The raw NM-08 outcome
(`approved`/`restricted`/`requires_review`/`insufficient_data`/`rejected`) is
stored as `policy_outcome`; the simulated outcome blocks anything that is not
approved. Sells are recorded as approved de-risking exits: NM-08 entry gates
never block a reduction.

## Liquidity, fees and expiry

Available quantity is the **previous** completed bar's volume multiplied by
`participation_rate`. Using the current bar's full volume would be look-ahead
at the open. A fill smaller than the remaining quantity is a partial fill and
the order stays pending.

Expiry is counted in **bars** (`order_ttl_bars`), not calendar days, so a Friday
order with a one-bar TTL survives the weekend. Orders still pending at the end
of the data are reported as expired/unfilled.

## Sessions, halts and cancellation

For a known MIC, the NM-02 calendar decides whether a bar's local date is a
trading day. A holiday/weekend bar is excluded (`data_quality.closed_bars`) and
cannot fill an order. An unknown MIC has no calendar and is reported as such.
A `tradable=false` bar is a halt: warning, no decision, no fill.

`POST /market/backtests/{id}/cancel` refuses a completed run and marks a
created run `cancelled`; an in-flight replay observes the cancellation flag at
every observation and aborts. This is covered by a concurrent-thread test.

## Limitations

- No bid/ask history or order-book depth; slippage is the NM-10 model.
- Daily bars only; no intraday sessions.
- Closures come from the supplied bar dates plus NM-02's dated calendars; a
  year outside verified coverage is reported rather than assumed.
