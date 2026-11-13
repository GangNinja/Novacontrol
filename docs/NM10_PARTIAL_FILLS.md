# NM-10 Partial Fills and Liquidity

## Liquidity budget

Only a fraction of the reference session's average completed-bar volume is
assumed available to one paper order. The default is 10%; the value is recorded
in the execution configuration and returned by the slippage contract.

```
available = average_daily_volume * participation_rate
```

No book depth is available in NM-02. NM-10 therefore treats the market reference
as a volume budget, not as a full order book, and never invents unlimited volume.

## One fill

1. Trigger and limit conditions are checked against the current snapshot.
2. The fill quantity is `min(remaining_quantity, available_quantity)`.
3. For a SELL, it is further capped by the current position.
4. For a BUY, it is reduced until the modelled fee-inclusive cost fits the active
   cash reservation.
5. The remainder stays on the order.

A fill with `quantity < remaining` moves the order to `PARTIALLY_FILLED`. The
order remains cancellable and can be processed again when fresh liquidity
arrives. A fill that exhausts the remainder moves it to `FILLED`.

## Invariants

- There is at most one `execution_fills` row per `(order_id, sequence)`.
- The immutable fill table rejects `UPDATE` and `DELETE` with SQLite triggers.
- The sum of fill quantities is checked against requested and filled quantities
  by reconciliation.
- Cancelling or expiring a partially filled order preserves every existing fill
  and releases only the remaining reservation.

## Limitation

There is no queue position, latency, intraday volume curve or hidden liquidity.
The participation-rate model is explicitly an approximation. A real book would
require live depth that NM-02 does not claim to provide.
