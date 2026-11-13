# NM-10 Reconciliation

`ExecutionReconciler` is read-only with respect to orders, fills, wallets and
positions. It creates a reconciliation report and never repairs a discrepancy.

## Checks

For each order it verifies:

- filled quantity equals the sum of its fill quantities;
- no fill set exceeds the requested quantity;
- remaining quantity equals requested minus filled;
- a fully filled order is marked `FILLED`;
- terminal orders have no active reservation;
- a fill belongs to its order and is not duplicated;
- gross value, net cash, fee component sum and slippage arithmetic;
- fee totals against the current schedule when the fill used the current config
  version;
- every referenced ledger transaction exists;
- position-effect arithmetic (`before +/- fill = after`);
- the instrument's current portfolio position is not negative;
- the portfolio wallet independently reconciles against its ledger.

## Results

Each run persists `execution_reconciliations` with `ok`, issues, counts and a
timestamp. The engine publishes `novamarket.execution.reconciled` when clean and
`novamarket.execution.reconciliation.failed` when issues exist. The API returns
the full issue evidence:

```
POST /market/execution/reconcile   {"order_id": "ord_..."}   # or no body for all
GET  /market/execution/reconciliations
```

## Known boundary

The report detects cross-store gaps; it does not make them disappear. A missing
ledger entry, a wallet mismatch or a position mismatch remains in the report for
an operator or a later correction workflow.
