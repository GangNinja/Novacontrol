# NM-07 — Rebalancing

A rebalance is a proposal, not an action. Nothing changes in the book until a
validated proposal is explicitly applied, and the applied result is reported as
book entries and ledger transfers — never as an executed order.

## Flow

```
POST /market/portfolios/{id}/rebalance
        │
        ├── cooldown / drift / turnover / no-candidate guards → skipped envelope
        └── proposal (status=validated, TTL, digest, intents)
                    │
                    ▼
POST /market/portfolios/{id}/apply  {proposal_id}
        │
        ├── pre-flight: prices within 2%, cash reserve holds
        ├── sells → clearing wallet, realized P&L recorded
        ├── buys  ← clearing wallet, positions opened/updated
        └── decision (rebalance_applied), events, last_rebalanced_at
```

## Guards

| Guard | Behaviour |
| --- | --- |
| Portfolio status | only `active` or `under_review` may propose or apply |
| Cooldown | within `cooldown_hours` of the last applied rebalance, skip |
| No candidates | record a skipped decision; retain existing holdings |
| No eligible assets | record every rejection; retain holdings; no forced liquidation |
| Drift | below `drift_threshold`, skip (`force` overrides) |
| Turnover | above `max_turnover` with existing positions, skip (`force` overrides) |
| Cash reserve | a proposal that would breach it is refused before it is stored |
| Proposal status | only `validated` can be applied; `applied` replays; `failed` and `expired` are refused |
| Expiry | an expired proposal is marked `expired` and refused |
| Price tolerance | a price move over 2% since proposals invalidates the apply |

## Proposal identity

`proposal_id = preb_<hash(portfolio|digest)>`. The digest covers the portfolio,
the policy version and every intent (instrument, side, quantity). The same
desired book is therefore never stored twice: a repeated proposal returns the
existing record, and applying an already-applied proposal returns
`replayed: true` without a second transfer.

## Apply is atomic in effect

Before the first ledger entry, apply re-reads every price, checks the 2%
tolerance, sums buys and sells and verifies the cash reserve. Only then does it
move to `rebalancing` and post entries. Each transfer uses an idempotency key
derived from the proposal and instrument, so a retried apply cannot double-buy.
If a later intent fails, the portfolio moves to `under_review`, the proposal is
marked `failed` so it can never be re-applied, and the error is raised, not
swallowed; `reconcile()` names any ledger transfer that landed without a book
entry.

## What a proposal contains

- `intents` — instrument, side, target and current weight, notional, quantity
  delta, price and the reasons from selection.
- `drift` and `turnover` — measured, not estimated.
- `rationale` — the weighting method and the change count.
- `policy_version`, `expires_at`, `status`, `digest`.

## What rebalancing never does

- It never liquidates holdings because a forecast disappeared; missing evidence
  retains the book.
- It never sends an order, opens a broker session or creates real money.
- It never executes from a plan alone; a token or an explicit apply is required.
- It never silently ignores a failed transfer; the apply raises and the error is
  surfaced through the API as a typed conflict or data error.
