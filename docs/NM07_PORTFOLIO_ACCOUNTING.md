# NM-07 — Portfolio Accounting

All money is `Decimal` quantised to one paisa. Positions hold a quantity, an
average cost and a last price; valuations are snapshots taken from timestamped
NM-02 observations; realized and unrealized P&L are reported separately.

## Cost basis and realized P&L

- A buy adds `quantity × price` to cost basis and updates the average cost.
- A sell removes the sold quantity at the stored average cost and records
  `(price - average_cost) × quantity` as realized P&L on the position and on the
  portfolio record.
- Selling the whole position closes it (`closed_at` set) and publishes
  `position.removed`.
- Unrealized P&L is `market_value - cost_basis` for every open position.

## Valuation

`value_portfolio(record, positions, cash, history_totals=…)` returns:

| Field | Meaning |
| --- | --- |
| `cash` | live wallet balance |
| `invested` | sum of position market values (cost basis when unpriced) |
| `total_value` | cash + invested |
| `realized_pnl` | booked on the portfolio record |
| `unrealized_pnl` | market value minus cost basis |
| `total_pnl` | realized + unrealized |
| `return_pct` | `(total - initial_capital) / initial_capital × 100` |
| `drawdown_pct` | `(total - peak) / peak × 100`, peak from stored snapshots |
| `cash_weight` | cash / total |
| `priced_positions` / `stale_positions` / `holdings` | counts |

Drawdown uses the append-only valuation history, so it cannot be improved by
rewriting a past snapshot.

## Stale prices

A position whose `priced_at` is older than `max_price_age_hours` (72h by default)
is counted as stale and flagged in every holding row. The last known price is
still used for the book value — the alternative, pretending the position is
worth cost, would be worse — but the valuation and the API say plainly that it
is stale. Missing prices never fabricate a value: an unpriced position is valued
at cost and counted as stale.

## Append-only records

- `portfolio_valuations` and `portfolio_decisions` carry SQLite triggers that
  reject `UPDATE` and `DELETE`. History is the audit trail.
- `portfolio_position_history` records every buy, sell and close with quantity,
  price, notional, realized amount and reference.
- `portfolio_capital` records each funding and release with the ledger
  transaction id.

## Reconciliation

`reconcile(portfolio_id)` compares:

- capital funded and released (capital moves),
- realized P&L in the book (positions),
- realized P&L in the ledger (clearing account),

and reports any discrepancy instead of adjusting it away. `GET /market/wallet/
reconcile` performs the same check across every wallet.

## Fees, taxes and FX

`fee_bps` is zero and no tax or FX model exists in NM-07, so returns are gross
and single-currency. The fields exist so a later phase can add costs without
changing the shape of a valuation or a proposal.
