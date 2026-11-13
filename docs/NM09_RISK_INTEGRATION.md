# NM-09 Risk Integration

Every proposal calls the existing NM-08 `RiskEngine.assess`:

| signal | risk action |
|---|---|
| BUY | `add_position` |
| SELL | `reduce_position` |
| EXIT | `remove_position` |

The call carries portfolio, instrument, side, notional and actor. NM-09 does not reproduce risk arithmetic; it records the returned decision id, outcome, policy version, state version and any restrictions. Outcomes map to proposal states `approved`, `rejected` and `restricted`. `requires_review` and `insufficient_data` remain `pending_risk` and are never executable. A risk-engine exception is also `pending_risk`, with the error retained; it is never approval.

Restricted proposals preserve the NM-08 restriction text in `risk_restrictions`. A proposal is bound to the strategy version, signal, risk decision, policy version and state version that produced it. Reconfiguring the strategy or expiring the signal prevents a stale proposal. An approved proposal is still not an order: `forward` hands it to NM-10, which independently revalidates the NM-08 decision before any simulated order exists.

SELL and EXIT proposals are bounded by the current holding value. A SELL cannot exceed the position, and an EXIT must close the entire position.
