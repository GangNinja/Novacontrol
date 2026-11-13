# NM-08 — Decision Engine

`RiskEngine.assess` is the single entry point for pre-action validation. It captures
the portfolio state, computes metrics, evaluates the applicable policy, persists the
decision, raises alerts and publishes events.

## Supported actions

`apply_proposal`, `add_position`, `increase_position`, `reduce_position`,
`remove_position`, `rebalance`, `allocate_capital`, `reserve_capital`,
`paper_order`. Capital actions need a portfolio and a positive notional; a request
above available cash is a hard `insufficient_cash` breach. Paper orders are
projected as exposure when buying and as a reduction when selling.

## Outcomes and precedence

| Outcome | Meaning |
| --- | --- |
| `APPROVED` | No breach; all mandatory checks completed. |
| `REJECTED` | At least one hard constraint is violated. |
| `RESTRICTED` | Only soft constraints are violated; proceed only at reduced size or with an explicit adjustment. |
| `REQUIRES_REVIEW` | An authorized reviewer must decide. |
| `INSUFFICIENT_DATA` | A mandatory input is missing, stale or unusable. |

Precedence: hard breach → `REJECTED`; missing input → `INSUFFICIENT_DATA`; review
constraint → `REQUIRES_REVIEW`; soft breach → `RESTRICTED`; otherwise `APPROVED`.
`REJECTED` and `INSUFFICIENT_DATA` are never converted to approval.

## Decision record

Each decision contains `decision_id`, action, portfolio, proposal, instrument,
timestamp, policy id/version, state version, outcome, metrics, breaches, warnings,
restrictions, explanation, data references and latency. Decisions are append-only
at the database level (`BEFORE UPDATE`/`BEFORE DELETE` triggers raise).

## Binding and idempotency

The decision id is derived from action, portfolio, proposal, instrument, state
version and policy version. Repeating an assessment against the same state and
policy replays the same decision instead of creating a duplicate.

`apply_decision` re-reads the portfolio, compares the stored state version and the
applied policy version, and requires an `APPROVED` outcome (or a review decision
with an active override). It then calls `PortfolioEngine.apply` for the exact
proposal. `RESTRICTED` decisions are never applied at full size, even with an
override: submit a reduced or adjusted proposal and assess it again. Any mismatch
raises a typed risk error, so one approval cannot authorize a different proposal.
The decision id includes the action's side and notional as well as the proposal,
state and policy versions.

## Explainability

Explanations name the triggered constraints and observed values (for example
"max_position_weight: projected 41.2% exceeds 25%"). They are generated from the
evaluated metrics, never from a model or hidden reasoning.

## Events

15 `novamarket.risk.*` events are emitted for assessment requested/completed, each
action outcome, limit breaches, alert lifecycle, scenario completion, policy updates,
overrides and applied actions. Events are published only after the decision is
persisted.
