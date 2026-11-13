# Autonomous Paper Trading — API Design

**Status:** **AT-01 IMPLEMENTED SUBSET.** Only `GET /market/autonomy/{contracts,status,health}` and `POST /market/autonomy/{start,pause,resume,stop}` exist. Every other route in this document is design for AT-02+, which is not approved or implemented. Where this document conflicts with `AUTONOMOUS_TRADING_OWNER_DECISIONS.md` (owner-approved 10 November 2026), the owner decisions win.
**Date:** 9 November 2026
**Base:** Existing FastAPI application in `src/novacontrol/api/app.py`
**Rule:** No existing route is replaced. The autonomy group is additive. All order state remains authoritative in the existing `/market/execution/*` endpoints.

---

## 1. Design principles

1. **Control is explicit.** Starting autonomous mode is a separate action from starting NovaMarket. Starting NovaMarket does not start trading.
2. **Paper-only is immutable.** No request body, query parameter or configuration field can set live trading, a broker, or a real-money route.
3. **Reads are authoritative.** The UI reads cycle, decision and health records from the server. It does not derive an order state, price, P&L or risk outcome locally.
4. **Mutations are idempotent.** Repeating a start/pause/resume/stop request returns the current state. A repeated scan request with the same `request_id` returns the existing cycle.
5. **Fail closed.** An unavailable dependency returns an error and no order is created. The API does not fall back to a manual execution path.
6. **Orders use the existing engine.** Autonomy has no `POST /market/autonomy/order` route; approved proposals flow through `StrategyEngine.forward()` to `ExecutionEngine.submit()`.

---

## 2. Route group

All routes are under `/market/autonomy`. Ordinary `require_auth` is necessary but **not sufficient**: owner decision OD-05 requires a dedicated server-side autonomy-operator credential (`X-Autonomy-Operator-Token`, validated against `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`). A deployment without that credential fails closed with 403. Mutations also use the existing same-origin validation and rate-limit middleware.

| Method | Path | Purpose | Milestone |
|---|---|---|---|
| `GET` | `/market/autonomy/contracts` | Static modes, states, safety flags, invariants | AT-01 |
| `GET` | `/market/autonomy/status` | Current mode, runtime, lease, last/next work, counts, health | AT-01 |
| `GET` | `/market/autonomy/config` | Validated configuration + allowed ranges + version | Not implemented (deferred with AT-02) |
| `PUT` | `/market/autonomy/config` | Replace configuration (disabled/paused only) | Not implemented (deferred with AT-02) |
| `POST` | `/market/autonomy/start` | Start the shell in `monitoring` or the state-only `autonomous` | AT-01 |
| `POST` | `/market/autonomy/pause` | Pause new entries; retain protective monitoring | AT-01 |
| `POST` | `/market/autonomy/resume` | Resume monitoring or autonomous operation | AT-01 |
| `POST` | `/market/autonomy/stop` | Orderly stop; mode becomes disabled | AT-01 |
| `POST` | `/market/autonomy/kill` | Emergency kill; revoke lease; optional cancel | AT-06 |
| `POST` | `/market/autonomy/scan` | Run one operator-requested cycle | AT-02 |
| `GET` | `/market/autonomy/cycles` | Durable cycle history | AT-02 |
| `GET` | `/market/autonomy/cycles/{cycle_id}` | One cycle with decisions and links | AT-02 |
| `GET` | `/market/autonomy/decisions` | Decision/read-model history | AT-03 |
| `GET` | `/market/autonomy/decisions/{decision_id}` | One decision and its trace chain | AT-03 |
| `GET` | `/market/autonomy/positions` | Positions with exit-supervisor state | AT-05 |
| `GET` | `/market/autonomy/health` | Autonomy health/lease detail | AT-01 (implemented) |
| `GET` | `/market/autonomy/activity` | Durable autonomy activity feed | AT-06 |

No route is provided for creating, filling, cancelling or editing a paper order. Those operations remain at the existing `/market/execution/orders` endpoints and are not autonomous controls.

---

## 3. Configuration contract

The configuration is a separate autonomy document, not a rename of `NovaMarketConfig`. It is validated by `AutonomyConfig` and stored in `autonomy_config` with a monotonically increasing `config_version`.

```json
{
  "portfolio_ids": ["pf_demo_short"],
  "strategy_ids": [],
  "scan_intervals_seconds": {
    "short_term": 900,
    "medium_term": 3600,
    "long_term": 86400
  },
  "max_instruments_per_cycle": 20,
  "max_new_entries_per_cycle": 3,
  "max_new_notional_fraction_per_cycle": "0.10",
  "conflict_policy": "review",
  "paused_exit_policy": "protective",
  "max_cycle_seconds": 120,
  "lease_seconds": 180,
  "max_consecutive_failures": 3,
  "exit_policies": {
    "short_term": {
      "stop_loss_fraction": "0.02",
      "take_profit_fraction": "0.04",
      "trailing_stop_fraction": "0.015",
      "max_holding_days": 1,
      "portfolio_drawdown_exit": false
    },
    "medium_term": {
      "stop_loss_fraction": "0.05",
      "take_profit_fraction": "0.12",
      "trailing_stop_fraction": "0.04",
      "max_holding_days": 30,
      "portfolio_drawdown_exit": false
    },
    "long_term": {
      "stop_loss_fraction": "0.08",
      "take_profit_fraction": "0.25",
      "trailing_stop_fraction": "0.08",
      "max_holding_days": 365,
      "portfolio_drawdown_exit": false
    }
  }
}
```

### 3.1 Field rules

| Field | Rule |
|---|---|
| `portfolio_ids` | Required non-empty at start; each must resolve to an active or pausable portfolio when the cycle runs |
| `strategy_ids` | Empty means all active strategies; explicit IDs still must be active and horizon-compatible |
| `scan_intervals_seconds` | Positive; short-term must not be below the market-refresh floor; long-term may be daily |
| `max_instruments_per_cycle` | 1–20 in the initial implementation; a larger value requires the `_instruments` cap extension in AT-02 |
| `max_new_entries_per_cycle` | 0–10; a cap, never a permission |
| `max_new_notional_fraction_per_cycle` | Decimal in `(0, 1]`; a pre-filter, never a risk override |
| `conflict_policy` | `review` or `block`; `priority` is intentionally not available to autonomy in v1 |
| `paused_exit_policy` | Fixed to `protective` (OD-02); `monitor_only` is not supported |
| `max_cycle_seconds` | 30–600; bounds the lease and a cycle |
| `lease_seconds` | At least `max_cycle_seconds`; bounded |
| `max_consecutive_failures` | 1–10; opens the runtime breaker |
| `exit_policies` | One policy per portfolio horizon; fractions are decimal strings; `portfolio_drawdown_exit` is opt-in and default false |

Unknown fields, empty portfolio allowlists, `paper_only=false`, `live_trading=true`, `broker_connected=true`, or any credential-shaped field are rejected with `422`.

`PUT /config` is allowed only in `disabled` or `paused`. It requires `expected_version` for optimistic concurrency. If another operator changed the configuration, return `409 config_version_conflict`; do not overwrite it.

```json
{
  "expected_version": 3,
  "reason": "reduce max entries during review",
  "config": { "...": "full config object" }
}
```

---

## 4. Control requests

### 4.1 Start

```http
POST /market/autonomy/start
```

```json
{
  "mode": "autonomous",
  "reason": "operator-authorized paper session",
  "acknowledge_paper_only": true,
  "request_id": "req_01J..."
}
```

- `monitoring` does not require the acknowledgement.
- `autonomous` requires `acknowledge_paper_only: true` and a paper-only execution contract.
- The response is the current `AutonomyStatus`.
- Repeating the same desired mode is idempotent.
- Starting from `paused` is `resume`, not `start`; `409 wrong_state`.
- AT-01 may persist `autonomous` as a **state-only shell** (`trading_implemented=false`, `orders_possible=false`). The `409 autonomy_not_released` rule applies only to future order/scan work (AT-04/AT-06), not to entering the mode shell.

### 4.2 Pause

```json
{
  "reason": "market event review",
  "request_id": "req_01J..."
}
```

Effect: no new entries; runtime continues; protective exits follow `paused_exit_policy`. Repeated pause is idempotent.

### 4.3 Resume

```json
{
  "mode": "autonomous",
  "reason": "review complete",
  "acknowledge_paper_only": true,
  "request_id": "req_01J..."
}
```

The requested mode must not be `disabled`. Resume from `paused` performs a fresh preflight and recovery check. If the runtime is `recovering`, return `409 recovery_in_progress`; an operator cannot skip reconciliation.

### 4.4 Stop

```json
{
  "reason": "end of session",
  "cancel_open_orders": false,
  "request_id": "req_01J..."
}
```

Effect: mode `disabled`; no new cycles; lease released. `cancel_open_orders` defaults to false so an orderly stop does not destroy a legitimate working paper order. If true, only orders whose actor begins with `autonomy:` are cancelled.

### 4.5 Kill

```json
{
  "reason": "emergency stop",
  "confirm": "KILL AUTONOMY",
  "cancel_open_orders": true,
  "request_id": "req_01J..."
}
```

- `confirm` is exact and required.
- Kill sets mode `disabled`, revokes the lease immediately, cancels open autonomy orders when requested, and preserves fills, ledger entries and positions.
- Kill is not a substitute for a broker kill switch; there is no broker.
- Repeat kill is idempotent and returns the recorded kill timestamp.

### 4.6 Scan now

```json
{
  "portfolio_ids": ["pf_demo_short"],
  "force": false,
  "submit_orders": false,
  "request_id": "req_01J..."
}
```

- A manual scan is useful for acceptance and operations.
- `force` bypasses the cadence, never the session/calendar/coverage/data/risk gates.
- `submit_orders=true` is accepted only when the current mode is `autonomous`; otherwise `409 not_autonomous`.
- In `monitoring`, the scan creates signals/proposals/risk records but no orders.
- A repeated `request_id` returns the same cycle or `409 cycle_in_progress`.

---

## 5. Read models

### 5.1 Status

`GET /market/autonomy/status` returns a self-contained status document:

```json
{
  "ok": true,
  "mode": "paused",
  "desired_mode": "autonomous",
  "runtime_state": "running",
  "paper_only": true,
  "live_trading": false,
  "broker_connected": false,
  "config_version": 3,
  "lease": {
    "owner": "nvmkt-7f3c...",
    "expires_at": "2026-11-09T10:15:00Z",
    "held": false
  },
  "last_tick_at": "2026-11-09T10:12:03Z",
  "last_successful_scan_at": "2026-11-09T10:10:00Z",
  "last_successful_execution_at": "2026-11-09T10:10:04Z",
  "next_due_at": "2026-11-09T10:25:00Z",
  "last_cycle_id": "acycle_...",
  "last_cycle_status": "completed",
  "recovery_required": false,
  "kill_switch_at": null,
  "counts": {
    "cycles_today": 6,
    "decisions_today": 24,
    "risk_approved_today": 4,
    "risk_rejected_today": 3,
    "orders_today": 3,
    "fills_today": 2,
    "open_positions": 2,
    "exit_proposals_open": 1
  },
  "health": {
    "state": "ok",
    "reasons": [],
    "data": {"state": "fresh", "oldest_age_seconds": 420},
    "risk": {"available": true},
    "execution": {"available": true, "paper_only": true},
    "circuit_breaker": "closed"
  }
}
```

`mode` is the current mode. `desired_mode` is the last operator-selected mode, used by restart policy. A process that died may show `runtime_state=stopped` until recovery; it never shows `running` solely because the previous process wrote that value.

### 5.2 Cycle

```json
{
  "ok": true,
  "cycle": {
    "cycle_id": "acycle_01J...",
    "cycle_key": "autonomous:pf_demo_short:short_term:2026-11-09T10:10:00Z:3",
    "portfolio_id": "pf_demo_short",
    "horizon": "short_term",
    "trigger": "scheduled",
    "mode": "autonomous",
    "status": "completed",
    "started_at": "2026-11-09T10:10:00Z",
    "finished_at": "2026-11-09T10:10:04Z",
    "session_state": "open",
    "data_state": "fresh",
    "counts": {
      "scanned": 18,
      "signals": 4,
      "proposals": 2,
      "risk_approved": 1,
      "risk_rejected": 1,
      "submitted": 1,
      "filled": 1,
      "exits_proposed": 0,
      "skipped": 1,
      "errors": 0
    },
    "summary": "1 paper order filled; 1 risk rejection recorded.",
    "error": "",
    "decision_ids": ["adec_01J...", "adec_01J..."]
  }
}
```

### 5.3 Decision

```json
{
  "ok": true,
  "decision": {
    "decision_id": "adec_01J...",
    "cycle_id": "acycle_01J...",
    "portfolio_id": "pf_demo_short",
    "instrument_id": "NSE:RELIANCE",
    "strategy_id": "strat_trend",
    "action": "open",
    "status": "filled",
    "reason": "BUY signal; NM-08 approved; paper order filled.",
    "signal_id": "sig_...",
    "proposal_id": "prop_...",
    "risk_decision_id": "risk_...",
    "order_id": "ord_...",
    "fill_ids": ["fill_..."],
    "created_at": "2026-11-09T10:10:01Z",
    "updated_at": "2026-11-09T10:10:04Z"
  }
}
```

The decision view is a trace index. The authoritative details remain on the linked strategy, risk, execution and portfolio endpoints.

### 5.4 Health

`GET /market/autonomy/health` returns the same health block as status plus per-dependency detail and breaker history:

```json
{
  "ok": true,
  "state": "degraded",
  "reasons": ["market data older than the configured limit"],
  "calendar": {"coverage": "2026", "session": "open", "verified": true},
  "data": {"state": "stale", "oldest_age_seconds": 5400},
  "risk": {"available": true},
  "execution": {"available": true, "paper_only": true},
  "portfolio": {"available": true},
  "lease": {"held": false, "owner": "", "expires_at": null},
  "circuit_breaker": {"state": "closed", "failures": 1},
  "last_error": "no usable timestamped price for NSE:XYZ",
  "recovery_required": false
}
```

### 5.5 Contracts

`GET /market/autonomy/contracts` returns static metadata:

- modes and runtime states;
- cycle/decision statuses and allowed transitions;
- safety flags;
- invariants (`paper_only`, no broker, no live orders, no `PortfolioEngine.apply` path);
- the route list and the `AutonomyConfig` field map;
- no secret values and no mutable account data.

---

## 6. List and filter contracts

`GET /market/autonomy/cycles`

| Query | Meaning |
|---|---|
| `limit` | 1–200, default 50 |
| `status` | `pending|running|completed|skipped|failed|interrupted|cancelled` |
| `portfolio_id` | Exact portfolio |
| `horizon` | `short_term|medium_term|long_term` |
| `since` | ISO-8601 timestamp |

Response: `{ "ok": true, "cycles": [...], "total": n, "limit": n }`.

`GET /market/autonomy/decisions`

| Query | Meaning |
|---|---|
| `limit` | 1–500, default 100 |
| `cycle_id` | Exact cycle |
| `portfolio_id` | Exact portfolio |
| `instrument_id` | Exact instrument |
| `status` | Decision status |
| `action` | `open|add|reduce|close|hold` |
| `since` | ISO-8601 timestamp |

`GET /market/autonomy/activity?limit=100` returns durable activity rows with `type`, `at`, `cycle_id`, `decision_id`, `order_id`, `severity` and `detail`. It is a read model over cycles/decisions and does not replace the application event bus.

---

## 7. Error contract

Errors use the existing FastAPI detail shape:

```json
{
  "detail": {
    "error": "not_autonomous",
    "message": "orders can only be submitted in autonomous mode",
    "mode": "monitoring"
  }
}
```

| HTTP | Code | When |
|---|---|---|
| 401 | `unauthenticated` | No/invalid session |
| 403 | `forbidden` | Foreign origin or insufficient permission |
| 404 | `unknown_cycle` / `unknown_decision` | Unknown ID |
| 409 | `wrong_mode` / `recovery_in_progress` / `cycle_in_progress` / `config_version_conflict` / `lease_conflict` / `not_autonomous` / `autonomy_not_released` | State conflict; no order is submitted |
| 422 | `invalid_config` / `invalid_request` / `confirmation_required` | Validation |
| 503 | `risk_unavailable` / `execution_unavailable` / `portfolio_unavailable` / `data_unavailable` | Dependency unavailable; no order |
| 500 | `internal_error` | Unexpected; the runtime records the error and fails closed |

A `409` or `503` is never converted into a manual execution fallback.

---

## 8. API registration requirements

Adding the route group is a five-place change, consistent with `AGENTS.md` and the existing NovaMarket API:

1. Handler in `src/novacontrol/api/app.py`.
2. `ApiSurface` route declaration in `src/novacontrol/api/models.py`.
3. `ROUTE_CONSUMERS` entry in `src/novacontrol/api/route_consumers.py` (use `no-render` for API-backed React pages, matching existing NovaMarket routes).
4. Regenerate `docs/API.md` with `python scripts/generate_api_reference.py`.
5. Tests for the route, auth, same-origin checks and the frontend consumer contract.

`python scripts/generate_api_reference.py --check` must be clean before acceptance. A route without a `ROUTE_CONSUMERS` entry fails the generator and the contract test.

---

## 9. Frontend mapping

| UI surface | Endpoint(s) | Existing code |
|---|---|---|
| Mode/health card | `GET /status`, `GET /health` | `useApi`, `Metric`, `StatusBadge` |
| Start/pause/resume/stop | control endpoints | `Button`, `ConfirmDialog`, toast |
| Scan history | `GET /cycles` | `DataTable`, `Tabs` |
| Cycle detail | `GET /cycles/{id}` | existing detail-grid patterns |
| Decisions/risk | `GET /decisions`, decision detail | `DataTable`, `StatusBadge` |
| Positions/exits | `GET /positions`, existing portfolio endpoints | `DataTable`, `Money` |
| Orders/fills | existing `/market/execution/orders` and `/fills` | existing Paper table components |
| Activity/errors | `GET /activity`, `/health`, existing events | existing activity list |
| Strategy performance | existing `/market/strategies/{id}/performance` | existing performance cards |

No UI component computes a risk result or a ledger value.

---

## 10. Backward compatibility

- Existing routes and response shapes are unchanged.
- Existing manual paper-order submission remains available and independent.
- The optional `actor` parameter on the internal `forward_strategy_proposal` service method is additive; the API's existing manual forward continues to work.
- Existing execution routes remain the only order surface. Autonomy decisions link to them rather than replacing them.
- The API version and contract version remain additive; no existing NM-01–NM-14 route is removed.
