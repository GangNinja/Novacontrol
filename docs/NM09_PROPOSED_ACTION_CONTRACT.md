# NM-09 Proposed Action Contract

A proposed action contains `proposal_id`, signal, strategy and strategy version, portfolio, instrument, action, side, notional, reference price, price timestamp, created/expiry timestamps, reason, forecast/event references, status, risk decision fields, `risk_restrictions`, `conflict_id`, `execution_order_id`, `execution_status` and `is_executed` (always false; NM-10 owns execution state).

Statuses are `created`, `pending_risk`, `approved`, `rejected`, `restricted`, plus reserved `expired`, `cancelled` and `forwarded_to_execution`. `forwarded_to_execution` is set only after NM-10 returns an accepted paper order; `execution_order_id` and `execution_status` identify that order. Only an approved proposal can be forwarded. Rejected, restricted and pending proposals are never executable; restricted proposals retain their NM-08 restriction text.

The API exposes proposal reads and a `forward` endpoint that submits an approved proposal to NM-10 paper execution. NM-09 itself never mutates a wallet or portfolio and never contacts a broker. HTTP proposal creation goes through `POST /market/strategies/run`; the engine-level `propose` path is the same risk-assessed path.
