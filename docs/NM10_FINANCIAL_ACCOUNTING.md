# NM-10 Financial Accounting

## Money movement

Every BUY fill posts two NM-03 transfers:

1. gross value from the portfolio wallet to `pfw_market_clearing`;
2. simulated transaction costs from the portfolio wallet to the same clearing
   wallet.

Every SELL fill posts:

1. gross proceeds from `pfw_market_clearing` to the portfolio wallet;
2. transaction costs from the portfolio wallet to the clearing wallet.

The wallet balance is never edited directly. Each posted transaction carries a
unique idempotency key derived from the fill id and leg. The fill records every
ledger transaction id.

## Reservations

Reservations are execution-owned claims, not wallet mutations.

- BUY: cash reservation = gross notional + estimated fees + configured slippage
  buffer. Active reservations for the portfolio are subtracted from available
  wallet cash.
- SELL: quantity reservation equals the requested quantity. Active quantity
  reservations are subtracted from the held position.

A fill consumes the actual net cash or quantity; a full fill releases the unused
cash buffer. Cancellation, rejection, expiry and failure release all remaining
active reservations. Reservations are persisted so duplicate submissions cannot
double-reserve.

## Position accounting

Positions are updated only after a fill:

- BUY: quantity increases; average cost includes gross value and simulated fees;
- SELL: quantity decreases; realized P&L = `(fill price - average cost) * qty -
  sell fees`;
- full exits close the position and retain realized P&L;
- NM-07's `record_position_history` receives `paper_buy`/`paper_sell` entries and
  the portfolio timestamp/realized total is touched.

An order that is only open does not change a position or cash. A partial fill
changes exactly the filled quantity, never the requested quantity.

## Atomicity and failure

The wallet and portfolio stores use separate SQLite connections. The engine orders
writes and uses idempotency, but cannot claim a distributed transaction across
those stores. If a later write fails, the order is failed and reconciliation
reports the incomplete evidence; no automatic adjustment is posted. This is
deliberately visible rather than hidden.
