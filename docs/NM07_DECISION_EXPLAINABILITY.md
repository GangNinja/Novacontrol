# NM-07 — Decision Explainability

Every autonomous act leaves a record: creation, each proposal, each skip, each
apply, and each lifecycle change. Decisions are append-only and readable through
`GET /market/portfolios/{id}/decisions` and
`GET /market/portfolios/{id}/lifecycle`.

## Decision record

```
decision_id, portfolio_id, at, kind, payload
```

`payload` carries:

| Key | Content |
| --- | --- |
| `rationale` | one or two sentences stating why |
| `selected` | the assessments that passed, with score, forecast and reasons |
| `rejected` | every rejection with the human-readable reason |
| `allocations` | target weights and the notes from `allocate()` |
| `constraints` | the policy that was applied |
| `references` | forecast ids, event ids, candidate evidence |
| `data_quality` | `good`, `insufficient eligible evidence`, `no candidate data`, … |

Kinds currently written: `create`, `rebalance_proposed`,
`rebalance_skipped`, `rebalance_applied`, plus lifecycle rows in the separate
`portfolio_lifecycle` table.

## Rejection reasons

Selection returns a reason per rejected candidate, for example:

- `discovery score 41.0 is below the policy minimum 50.0`
- `one_week forecast is 96.4h old; the limit is 72h`
- `model 'm1' has no measured state for this horizon`
- `model state suspended is excluded by this policy`
- `uncertainty high exceeds the policy limit medium`
- `a confirmed high-importance event is open on this instrument and this horizon
  excludes event pressure`

The same strings appear in the decision payload, so an operator can see exactly
which gate held a name out — not merely that it was absent.

## Skips are decisions too

A cooldown, drift, turnover or no-candidate outcome is not an error: it is a
recorded decision with a reason. This is deliberate. "Nothing happened" must be
as auditable as "something happened", otherwise silence is indistinguishable
from a bug.

## Immutability

Triggers on `portfolio_decisions` reject updates and deletes. A correction is a
new decision, never an edit, so the history always shows what was believed at
the time.

## Reading a book

A portfolio detail request returns the current portfolio, policy, cash,
valuation, holdings, constraints and counts. Adding `decisions` reconstructs the
path: the opening decision states the initial evidence, each proposal states the
desired book and the reasons, each apply states what was actually posted.

## Limits

- The rationale is generated from measured inputs, not from a language model.
- Correlations are not available, so no explanation claims a diversification
  benefit.
- A `skipped` result is not an error and carries no approval token: nothing ran,
  so there is nothing to approve.
