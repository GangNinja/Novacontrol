# NM-10 — Realistic Paper Execution Architecture

## Scope

NM-10 turns an **approved NM-09 proposed action** into a simulated order, applies
NM-02 market data and documented execution assumptions, records fills, moves
simulated NovaCoin through NM-03's ledger, updates NM-07 positions, and
reconciles the records afterwards. It never contacts a broker, never places a
real order and has no live-trading route.

The package is `novacontrol.novamarket.execution`:

| Module | Responsibility |
|---|---|
| `models.py` | Order, fill, reservation, market snapshot and lifecycle records |
| `config.py` | Versioned execution, liquidity and fee assumptions |
| `store.py` | SQLite orders, immutable fills, transitions, reservations, reconciliation |
| `pricing.py` | NM-02 snapshot, simulated slippage and deterministic fee math |
| `engine.py` | Validation, reservations, order processing, fills, cancellations |
| `reconciliation.py` | Independent read-only verification and issue reporting |

## Dependency flow

```
NM-09 proposal (approved, unexpired)
        |
        v
ExecutionEngine.submit
  NM-08 decision lookup + revalidation
  NM-07 portfolio / position read
  NM-03 wallet availability read
  NM-02 latest price, volume, freshness
        |
        v
Order (CREATED -> PENDING_VALIDATION -> ACCEPTED -> OPEN)
        |
        v
process: stop trigger -> limit check -> liquidity cap -> fill price
        |
        v
NM-03 ledger transfer(s) -> NM-07 position/history -> immutable fill -> order state
        |
        v
reconciliation (reads only; never repairs)
```

Wallet, portfolio, risk, strategy and market-data stores remain authoritative
for their own data. The execution store owns only orders, fills, transitions,
reservations and reconciliation reports. A fill insert, its order update, its
transition and its reservation changes are committed together in the execution
store; the wallet and portfolio writes happen before that commit.

## Safety boundaries

- Submission requires `proposal.status == approved`, an approved NM-08 decision,
  matching decision/proposal/portfolio references, matching state version when
  supplied, and an unexpired proposal/decision.
- Restrictions that this model cannot encode are refused rather than ignored.
- Crypto instruments, halted/delisted/closed instruments, stale observations and
  missing market data are refused.
- All prices and amounts use `Decimal`; quantities use four decimal places and
  money two decimal places with `ROUND_HALF_EVEN`.
- Orders and fills are paper-only; `status()` and `contract()` publish
  `paper_only=true`, `live_trading=false` and `broker_connected=false`.

## Transaction boundaries

Wallet and portfolio state live in separate store connections. The engine
therefore uses a deliberate order (validate → reserve → ledger transfer → position
→ fill/order/reservation commit) with idempotency keys, fail-closed exceptions
and reconciliation. The execution-owned group is atomic in SQLite, but it does
**not** claim that one SQLite transaction spans all stores. A failure after the
ledger or position write and before the execution commit remains visible to
reconciliation and is never silently repaired.

## API surface

The routes are under `/market/execution`:

`POST /orders`, `GET /orders`, `GET /orders/{id}`, `GET /orders/{id}/fills`,
`GET /orders/{id}/history`, `POST /orders/{id}/process`,
`POST /orders/{id}/cancel`, `GET /fills`, `GET /status`, `GET /contracts`,
`GET /fees`, `GET /slippage`, `POST /reconcile`, `GET /reconciliations`.

All are authenticated and declared in `ApiSurface` and `ROUTE_CONSUMERS`; the
generated `docs/API.md` is the wire reference.

## Events

The order lifecycle publishes `novamarket.order.*`; fills publish
`novamarket.execution.fill.recorded`; reconciliation publishes
`novamarket.execution.reconciled` or
`novamarket.execution.reconciliation.failed`. Events are emitted after the
corresponding record is committed.
