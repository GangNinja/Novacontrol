# NM-10 Transaction Costs

## Illustrative schedule

The default schedule is an **illustrative Indian cash-equity simulation**, not a
broker quote and not a claim about current statutory rates. Its effective date is
recorded as `2026-01-01`; the full disclaimer is returned by
`GET /market/execution/fees`.

| Component | Default |
|---|---:|
| Brokerage | 3 bps of notional |
| Exchange transaction | 0.297 bps |
| SEBI turnover | 0.01 bps |
| Stamp duty (BUY only) | 0.015 bps |
| GST | 18% of brokerage + exchange + SEBI |
| Minimum total fee | ₹0.00 |

Each component is calculated separately, quantized to paise and stored in the
fill's `fee_breakdown`. The total is the sum of the components; a minimum-fee
adjustment is recorded as its own component if configured.

## Determinism

All arithmetic uses `Decimal`, `ROUND_HALF_EVEN` and the same config version that
was stored on the order. A fee change creates a new configuration version; a
historical fill retains the version used to produce it. Reconciliation checks the
component sum against the recorded total and, when the config version matches the
current one, recomputes the expected total.

The default figures must be replaced with verified broker/exchange values before
anyone relies on exact costs. The API says so explicitly.
