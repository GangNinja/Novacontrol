# NM-12 Shadow Mode — API, Events and Permissions

All routes require authentication and are declared in `ApiSurface` and
`ROUTE_CONSUMERS`. The market route count is **177** (22 NM-12 routes).

## Session management

| Method | Path |
|---|---|
| GET | `/market/shadow/status` |
| GET | `/market/shadow/contracts` |
| POST | `/market/shadow/sessions` |
| GET | `/market/shadow/sessions` |
| GET | `/market/shadow/sessions/{session_id}` |
| GET | `/market/shadow/sessions/{session_id}/history` |
| POST | `/market/shadow/sessions/{session_id}/start` |
| POST | `/market/shadow/sessions/{session_id}/pause` |
| POST | `/market/shadow/sessions/{session_id}/resume` |
| POST | `/market/shadow/sessions/{session_id}/stop` |
| POST | `/market/shadow/sessions/{session_id}/complete` |
| POST | `/market/shadow/sessions/{session_id}/cancel` |

## Observation and evidence

| Method | Path |
|---|---|
| POST | `/market/shadow/sessions/{session_id}/cycle` |
| POST | `/market/shadow/sessions/{session_id}/track-outcomes` |
| GET | `/market/shadow/sessions/{session_id}/decisions` |
| GET | `/market/shadow/sessions/{session_id}/projections` |
| GET | `/market/shadow/sessions/{session_id}/outcomes` |
| GET | `/market/shadow/sessions/{session_id}/analytics` |
| GET | `/market/shadow/sessions/{session_id}/errors` |
| GET | `/market/shadow/sessions/{session_id}/health` |
| POST | `/market/shadow/sessions/{session_id}/compare` |
| GET | `/market/shadow/sessions/{session_id}/comparisons` |

`cycle` and `track-outcomes` accept an optional `observations` object keyed by
instrument for deterministic/operator-supplied observations. Without it the
service reads NM-02.

Error mapping: unknown session → 404, invalid lifecycle edge → 409, invalid
payload/configuration → 422, unexpected failure → 500.

## Events

Eighteen typed events are published after the corresponding row is committed:

`novamarket.shadow.session.{created,started,paused,resumed,stopped,completed,cancelled,failed}`,
`novamarket.shadow.decision.recorded`, `novamarket.shadow.risk.evaluated`,
`novamarket.shadow.projection.created`, `novamarket.shadow.outcome.updated`,
`novamarket.shadow.cycle.completed`, `novamarket.shadow.data.quality.failed`,
`novamarket.shadow.recovered`, `novamarket.shadow.comparison.completed`,
`novamarket.shadow.analytics.updated`, `novamarket.shadow.divergence.detected`.

Payload fields are declared in `core/events.py` and pinned by the NovaMarket
event-contract test. No payload contains a wallet movement, order id or live
execution claim.

## Permissions and capabilities

| Permission | Risk | Capability |
|---|---|---|
| `market.shadow.read` | low | `market.shadow.read` |
| `market.shadow.manage` | medium, confirmation | `market.shadow.manage` |
| `market.shadow.run` | medium | `market.shadow.run` |
| `market.shadow.compare` | low | `market.shadow.compare` |

The permission model grants these four and still denies
`market.trade.live` and sensitive configuration. The capability table grows to
43 entries; no live/broker capability is advertised.

## Projection wire shape

Every projection includes:

```json
{
  "label": "hypothetical order projection",
  "simulated": true,
  "execution_order_id": null,
  "execution_permitted": false,
  "is_execution": false,
  "estimated_price": "...",
  "estimated_slippage_pct": "...",
  "estimated_fees": {"brokerage": "...", "total": "..."},
  "assumptions": ["...", "no order was created, submitted, filled or reserved"]
}
```

## Relationship to paper trades, backtests and real trades

| Object | Data source | Executes? | Capital/ledger effect |
|---|---|---|---|
| Shadow projection | live NM-02 observation | no | none |
| NM-10 paper order | approved NM-09 proposal | simulated fill | simulated wallet/portfolio |
| NM-11 backtest | historical bars | no | isolated simulated book |
| Real trade | broker/exchange | yes | real money (not implemented) |
