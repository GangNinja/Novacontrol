# NM-10 Slippage Model

## Formula and inputs

The model is deliberately deterministic and uses only available NM-02 inputs:

- reference last price,
- average completed-bar volume,
- recent close-to-close volatility,
- order quantity and side,
- configured half-spread and impact parameters.

There is no live bid/ask or depth data, so the spread is a disclosed synthetic
assumption. The implementation is in `execution/pricing.py`.

## Defaults

| Parameter | Default |
|---|---:|
| Half-spread (`spread_bps / 2`) | 4 bps |
| Impact coefficient | 12 bps at 100% participation |
| Volatility weight | 0.35 |
| Maximum slippage | 150 bps |
| Participation rate | 10% |

## Recorded values

Each fill records the reference price, execution price, absolute slippage amount,
slippage percentage, model version, full market snapshot and the fee breakdown.
`GET /market/execution/slippage` returns the active assumptions and limitations.

## Reproducibility

Given the same snapshot, quantity, side and configuration, the calculation is
pure and returns the same `Decimal` result. It does not consult a moving clock,
random source or network. The configured model version is persisted so a later
phase can reproduce the assumption set rather than reinterpreting a historical
fill with today's defaults.
