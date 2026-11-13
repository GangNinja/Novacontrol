# NM-12 Shadow Mode — Decisions, Outcomes and Analytics

## Decision semantics

A `ShadowDecision` records one strategy observation with:

- strategy id/version, instrument, decision time and market-data timestamp;
- the NM-02 data reference (source, observed time, quality, freshness, bar
  count);
- the immutable portfolio snapshot id;
- the risk-policy version and the raw NM-08 outcome;
- the proposal (side, action, quantity, notional, reference price, horizon);
- the risk assessment (breaches, warnings, restrictions, insufficient data);
- the projection id, if one exists;
- latency and a simulation-only label.

Statuses:

| Status | Meaning |
|---|---|
| `signal` | HOLD/NO_SIGNAL; no proposal |
| `risk_approved` | NM-08 approved, projection being recorded |
| `risk_rejected` | restricted, review, insufficient data or rejected |
| `projected` | risk approved and a hypothetical order projection exists |
| `blocked` | evaluation failed or the proposal was invalid |
| `expired` | reserved for an expired decision; outcomes also carry expiry |

A blocked or rejected decision never becomes a projection.

## Projection semantics

A `ShadowProjection` is a **hypothetical order projection**, not an order and
not a fill. It records instrument, side, action, quantity, order type, reference
price, estimated execution price, estimated slippage, fee components, estimated
notional, assumptions and status.

`status` is `projected` when the pure NM-10 model can price the order, or
`unavailable_liquidity` when there is no measurable traded volume. Both are
simulations. No fill price, fill quantity or execution id is invented.

## Outcome semantics

For every projected decision, an outcome sweep measures observations strictly
after `decision_at`:

- **Return %** = signed price change from the projection reference, in percent.
- **Hypothetical P&L** = signed `(price - reference) × quantity`.
- **MFE** = best favorable excursion, floored at 0.
- **MAE** = worst adverse excursion, capped at 0.
- **Intervals** = session `observation_intervals` (default 1, 5, 20 subsequent
  bars). An interval that has not been reached is `unresolved`; after
  `outcome_horizon_days` it becomes `expired`.
- **Unresolved/missing data** is retained explicitly; nothing is fabricated.

A decision cannot use its own or future data. Explicit observations with a
timestamp after the decision time are rejected, and explicit bars are filtered
by `observed_at`/`available_at`.

## Analytics

Session analytics include:

- decision totals and BUY/SELL/EXIT/HOLD/NO_SIGNAL distribution;
- risk approved/rejected/blocked counts and rejection reasons;
- projection counts, statuses and whether any execution id exists;
- resolved/unresolved/expired outcomes, wins/losses, mean return, mean MFE/MAE;
- strategy, instrument and horizon breakdowns;
- data-quality failure count, error count and mean decision latency.

Every response carries `hypothetical: true` and a note that these are not
investment returns. `sample_sufficient` is false below `min_sample_size`
(default 5), and `win_rate_pct` is `null` rather than a misleading statistic.
The HTTP test uses a smaller threshold only to exercise the shape.

## Comparison categories

`POST /market/shadow/sessions/{id}/compare` compares compatible evidence and
records one or more categories:

- `signal_disagreement`
- `risk_decision_disagreement`
- `data_mismatch` (available for explicit reference evidence)
- `timing_difference`
- `execution_assumption_difference`
- `outcome_divergence`

When no compatible reference is supplied the comparison is marked
`compatible: false` with an explanation rather than treating differing market
conditions or execution assumptions as equivalent. Backtest return ratios are
normalized to percent before comparison.
