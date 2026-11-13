# NM-10 Execution Model

## Market reference

`pricing.snapshot_for` reads NM-02's latest reconciled observation. Only an
observation from a usable reconciliation is eligible; the snapshot records the
instrument's trading status, the average completed-bar volume, the recent return
volatility and whether a bar-close fallback was configured.

The default policy refuses stale observations (`stale_after_seconds=300`),
unusable reconciliations, and `invalid` or `suspect` quality. `allow_bar_fallback`
is **false** by default; when enabled, the fallback is explicitly labelled
`nm02.bar.close.fallback` and is never presented as a live quote.

## Liquidity

NM-02 exposes trades and bars, not a complete order book or depth feed. NM-10
therefore uses a disclosed participation-rate approximation:

```
available_quantity = average completed-bar volume * participation_rate
```

The default participation rate is 10%. If no historical volume is available, the
available quantity is zero and a market order does not fill. The engine never
invents depth or liquidity.

## Fill price

For a fill, the engine computes a synthetic spread and square-root impact:

```
half_spread_bps = spread_bps / 2
impact_bps = impact_coefficient_bps
             * sqrt(quantity / available_quantity)
             * (1 + volatility_pct * volatility_weight)
raw_bps = min(half_spread_bps + impact_bps, maximum_slippage_bps)
price = reference * (1 +/- raw_bps / 10000)
```

A buy pays the positive adjustment; a sell receives the negative adjustment.
The limit is a hard cap after the adjustment. The model version
(`ExecutionConfig.version`) is stored on every order and fill.

This is an approximation, not a consolidated bid/ask. The absence of a real
depth book is a known limitation and is stated by `GET /market/execution/slippage`.

## Fill processing

`process` performs: expiry check, stop-trigger check, limit check, liquidity cap,
portfolio availability check, modelled price, fees, ledger settlement, portfolio
position update, then one execution-store commit for the immutable fill, order
state, transition and reservation changes, followed by event publication.
The sum of fills can never exceed the requested quantity. A second fill uses the
next sequence and the same order.

## Multiple fills

With a participation-limited volume, a 100-share order against 20 shares of
available liquidity becomes a 20-share `PARTIALLY_FILLED` order. A later
`process` with more available volume may fill the remainder; otherwise the order
can be cancelled or expires. Fills are individually recorded and immutable.
