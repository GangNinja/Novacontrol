# NM-07 — Architecture

NM-07 adds an autonomous, three-horizon portfolio manager on top of NM-01…NM-06.
It is a **simulated book**: every unit of capital is a real NovaCoin ledger
balance moved by NM-03, and every price is a timestamped NM-02 observation.
No order is placed, no broker is contacted and no real money exists anywhere in
the flow.

## Composition, not duplication

| Concern | Owner | NM-07 use |
| --- | --- | --- |
| Wallets, transfers, ledger, reversals | NM-03 | one wallet per portfolio, funded by a ledger transfer |
| Prices and observation timestamps | NM-02 | every valuation and trade price |
| Candidate discovery and scores | NM-05 | the only instrument universe |
| Probabilistic forecasts, model states, calibration | NM-06 | the only forward-looking input |
| Verified market events | NM-04 | an exclusion gate for the short horizon |
| Instrument metadata | NM-02 | sector/asset-class when exposed |

Portfolios never read a price feed, never compute a forecast and never score a
candidate themselves.

## Package

```
src/novacontrol/novamarket/portfolio/
  models.py      horizons, statuses and transitions, PortfolioPolicy,
                 HORIZON_POLICIES, PortfolioRecord, Position, TradeIntent,
                 RebalanceProposal, DecisionRecord, ValuationSnapshot
  store.py       SQLite schema, append-only triggers, positions, proposals,
                 capital moves, decisions, valuations, metrics
  accounting.py  value_portfolio(), performance(), drawdown, turnover
  selection.py   assess_candidates(): gates, ranking and rejection reasons
  allocation.py  allocate(): weights under every policy limit
  engine.py      PortfolioEngine: create/valuate/propose/apply/close/reconcile
  __init__.py    the public surface
```

`PortfolioEngine` composes the other engines through constructor injection and
setters (`set_market_data`, `set_discovery`, `set_forecasts`, `set_radar`), the
same seams the application wires at boot.

## Creation

1. Resolve the deterministic portfolio id from `(horizon, funding wallet, name)`.
2. Refuse early when the funding wallet cannot cover the capital.
3. Create the portfolio wallet `pfw_<id>` if absent.
4. Transfer the capital with the idempotency key `pf-fund-<id>`.
5. Record the capital move, the creation decision and the `created`/`activated`
   events.
6. Immediately propose and apply the opening allocation. The proposal preview
   is still recorded, so the first book is explainable like any later one.
7. A repeated create replays: no second transfer, no second proposal, no second
   book.

## Lifecycle

```
initializing → active ⇄ paused
                 │  ⇄ under_review
                 ├──→ suspended → active
                 └──→ closed
```

Every transition is checked by `ensure_portfolio_transition`; an illegal move
raises `PortfolioStateError` instead of overwriting the state. `apply` moves the
book to `rebalancing` and back to `active` inside one guarded operation.

## Events

Thirteen `novamarket.portfolio.*` events are published through the application's
event bus: created, activated, allocation.updated, position.added/updated/removed,
valuation.updated, rebalance.proposed/completed, performance.updated, paused,
suspended and closed. Payloads name stored records or measured counts; none
carries a price promise. A publish failure is logged and never rolls back a
committed book.

## API

Seventeen routes under `/market/portfolios` (see `docs/API.md`): create/list/
status/horizons, per-portfolio detail, holdings, valuation, valuations,
performance, decisions, proposals, lifecycle, reconcile, and the mutating
`rebalance`, `apply`, `status` and `close`. Errors are typed: unknown ids are
404, validation is 422, state conflicts are 409, data problems are 422.

## Failure semantics

- A refused funding leaves no portfolio record.
- A selection failure leaves capital in cash and records a skipped decision.
- An apply failure leaves the previous book intact: all prices and the cash
  reserve are checked before the first ledger entry, and every entry is
  idempotent.
- Missing evidence never forces a liquidation; existing holdings are retained.
- Stale prices are marked, not silently treated as live.
