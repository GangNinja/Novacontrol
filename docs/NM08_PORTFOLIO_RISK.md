# NM-08 — Portfolio, Position and Aggregate Risk

## Position risk

`position_risk(portfolio_id, instrument_id)` reports the holding's market value,
weight, sector, volatility, drawdown, liquidity, forecast reliability and event
exposure. A position is flagged when it exceeds the policy's position or sector
limit, when its liquidity falls below the minimum traded value, when volatility or
drawdown breaches the horizon policy, or when a verified high-importance event
exists. Flags produce alerts and recommended restrictions; they never trigger an
automatic sale.

## Portfolio risk

`portfolio_risk(portfolio_id)` evaluates one horizon using its own policy and
returns metrics, breaches, warnings, restrictions, the applied policy version and a
plain-language explanation. Volatility uses position weights and each instrument's
annualized volatility; concentration uses the holding weights; liquidity uses the
smallest position-level traded value; drawdown comes from NM-07's valuation.

## Aggregate exposure

`exposure()` combines every open portfolio using actual capital, not equal weights:

- Each portfolio contributes its valued total (`total_value`).
- Instrument and sector weights are re-weighted by portfolio value / total value.
- The aggregate policy is evaluated against the combined book.

This detects a sector that is acceptable in each horizon but excessive when the
three portfolios are viewed together.

## State version

Every report and decision records the NM-07 state version (updated timestamp,
valuation totals, holding counts and proposal counts). A decision made against an
older version is rejected by `apply_decision`, so a changed portfolio cannot reuse
an old approval.

## Forecast and event inputs

Forecast reliability uses NM-06 uncertainty, model state and forecast age. A high
predicted return never offsets low reliability. NM-04 events are split into verified
and unverified; verified high-importance adverse events add review pressure, while
unverified reports are warnings only.

## Capital

Cash, invested value and utilization are read from the NM-07 portfolio, whose cash
is backed by the NM-03 ledger. NM-08 never writes the ledger or invents balances.
