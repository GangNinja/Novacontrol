# NM-11 Leakage Prevention

The enforced convention is: a strategy decision made at a completed bar's
timestamp may use that bar and earlier bars. Its order executes, at the
earliest, on the **next** observation of that instrument, at that observation's
open. The signal close is never reused as the execution price.

At each observation the engine checks `bar.available_at <= observed_at`. A bar
whose availability is later than the decision time is excluded with a warning
and counted in `data_quality.unavailable_bars`.

## Events (NM-04)

The event loader supplies candidate rows; the engine keeps a row only when its
`published_at` (or `available_at`/`created_at`) is at or before the decision
time, its instrument matches, and its timestamp parses. Future rows, other
instruments and malformed timestamps are excluded. A loader failure is
recorded and yields no events rather than breaking the replay.

## Forecasts (NM-06)

A forecast is visible only when `created_at <= decision time`,
`data_cutoff <= decision time`, the configured model version matches (when one
is configured) and its status is not `failed`, `suspended` or `degraded`.

## Execution timing

Orders are created after the decision and execute from the next bar. Liquidity
is the previous bar's volume, not the current bar's, because the current bar's
full-day volume is not observable at its open.

## Sessions

A bar dated on an NM-02 calendar weekend or holiday is excluded before it can
become an observation, fill an order or enter history. A bar with
`tradable=false` is a halt: it warns and produces no decision or fill. A missing
bar is a closed session, never a fabricated return.

## Tests

`LeakageTests` covers: a future-availability bar, same-bar execution,
previous-bar liquidity, future/foreign/malformed events, future/cutoff/model/
failed forecasts, a halted bar, an exchange-holiday bar, missing sessions and
duplicate observations.

## Limitations

- Strategy definitions are configuration snapshots; historical NM-09 registry
  reconstruction is not implemented.
- No test covers a future corporate action or a future model artifact; those
  are outside the supplied data contract.
