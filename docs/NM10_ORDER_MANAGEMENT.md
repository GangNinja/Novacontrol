# NM-10 Order Management

## Identity and traceability

Every order carries `order_id`, `proposal_id`, `strategy_id`, `portfolio_id`,
`instrument_id`, side, type, requested/filled/remaining quantity, reference price,
risk decision reference, execution-config version and timestamps. The
`proposal_id` is unique in the execution store, so a repeated submission replays
the original order rather than creating a second one.

An order cannot be submitted without an approved NM-09 proposal and its stored,
unexpired NM-08 decision. `submit` records a rejection and its reason when
validation fails; it never marks a signal or proposal as filled.

## Lifecycle

The valid edges are defined in `ORDER_TRANSITIONS` and enforced by
`ExecutionStore.transition`:

```
CREATED -> PENDING_VALIDATION -> ACCEPTED -> OPEN
                                      |       |
                                      |       +-> PARTIALLY_FILLED -> FILLED
                                      |       +-> CANCEL_PENDING -> CANCELLED
                                      |       +-> EXPIRED / REJECTED / FAILED
                                      +-> EXPIRED / REJECTED / FAILED
```

`FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED` and `FAILED` are terminal.
Illegal edges raise instead of overwriting state. Every successful transition
writes an `execution_order_transitions` row with timestamp and reason.

## Supported types

- `market`: executes at the modelled price when data is current and liquidity
  exists; there is no promise of the last traded price.
- `limit`: a buy does not execute above the limit and a sell does not execute
  below it.
- `stop`: waits until the reference price crosses the trigger, then behaves as a
  market order. The trigger is persisted in `triggered_at`.
- `stop_limit`: waits for the trigger, then applies the limit condition.

An unknown type receives an explicit `UnsupportedOrderType` response. The API
maps it to HTTP 422.

## Cancellation and expiry

Cancellation is allowed on accepted, open and partially filled orders. It persists
`CANCEL_PENDING`, releases active reservations, then persists `CANCELLED`.
Existing fills are retained. A fully filled order cannot be cancelled.

Expiry is evaluated when the order is processed. An expired accepted, open or
partially filled order becomes `EXPIRED`, releases remaining reservations and
retains fills. There is no autonomous background loop; callers must call
`process` for open orders.
