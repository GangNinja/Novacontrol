# API Guide

NovaMarket exposes its HTTP API under `/market` through the shared FastAPI application. The **generated reference** is [API.md](API.md); this guide explains how to use it without duplicating every endpoint.

> **Paper-only:** no route submits a real order. `GET /market/execution/status` reports `paper_only: true`, `live_trading: false`, `broker_connected: false`. There is no broker endpoint and no live-order path.

## Contents

1. [Start the API](#1-start-the-api)
2. [Documentation endpoints](#2-documentation-endpoints)
3. [Authentication and origins](#3-authentication-and-origins)
4. [Route groups](#4-route-groups)
5. [Request and response conventions](#5-request-and-response-conventions)
6. [Errors](#6-errors)
7. [Representative examples](#7-representative-examples)
8. [Paper-order example](#8-paper-order-example)
9. [Safety and status endpoints](#9-safety-and-status-endpoints)
10. [Keeping the reference synchronized](#10-keeping-the-reference-synchronized)

## 1. Start the API

```powershell
cd novamarket
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8011
```

Or start the full application:

```powershell
.\start.ps1
```

Default base URL: `http://127.0.0.1:8011`.

The React UI normally calls relative `/market/...` paths through the Vite proxy. A built frontend may call the API origin configured with `VITE_API_BASE_URL`.

The running OpenAPI document contains **200 `/market` paths and 214 operations**. This count is a snapshot of the accepted running build; [API.md](API.md) is the exact generated route list.

## 2. Documentation endpoints

| URL | Content |
|---|---|
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | Machine-readable OpenAPI document |
| `/health` | Public application health check |

These are served by the same FastAPI process. `/docs` and `/redoc` load a browser UI; the JSON document is the source for client tooling. They document the **schema**, not market advice.

## 3. Authentication and origins

### Bearer token

Authentication is controlled by the shared `NOVACONTROL_API_TOKEN` environment variable:

- if unset, protected routes are callable under the local development posture;
- if set, send `Authorization: Bearer <token>`;
- `/health` remains public;
- the browser UI stores a token in `sessionStorage["novamarket.apiToken"]` for the tab session only.

The API does not accept the token in a query string. Do not put a real token in a URL, screenshot, log or committed file.

### CORS and origin guard

- `NOVAMARKET_CORS_ORIGINS` is a comma-separated **exact** origins allowlist. It does not support wildcards.
- `NOVAMARKET_ALLOWED_ORIGINS` adds exact origins accepted by the application's mutation origin guard; built-in loopback entries cover the default UI.
- Mutations from a foreign browser origin are rejected even if a client outside a browser could call them.
- `NOVACONTROL_TRUSTED_HOSTS` restricts accepted `Host` headers.

A `403` on a browser mutation usually means the page's origin is not in the allowlist. Add the exact origin; do not disable the guard. A `401` means the token is set but missing/invalid. CORS preflight failures appear in the browser console, not as a normal API error body.

### No multi-tenancy

The token is a local application gate, not per-user authorization. Do not expose this API directly to the public internet. If remote access is required, put it behind an authenticated TLS reverse proxy and network controls.

## 4. Route groups

The table lists the main families and representative endpoints. Use [API.md](API.md) for the complete list and request schemas.

| Family | Representative endpoints | Purpose |
|---|---|---|
| Status and lifecycle | `GET /market/status`, `/market/contracts`, `/market/overview`, `POST /market/enable`, `POST /market/disable` | Lifecycle, capabilities, health, contracts, operational overview |
| Configuration | `GET|POST /market/config`, `POST /market/notifications/test` | Validated configuration, SMTP test |
| Universe and instruments | `POST /market/universe/sync`, `GET|POST /market/instruments`, `POST /market/instruments/lookup` | Catalogue and identity |
| Market data | `GET /market/data/status`, `/market/data/providers`, `/market/data/quote/{instrument_id}`, `/market/data/history/{instrument_id}`, `/market/data/bars/{instrument_id}`, `POST /market/data/ingest` | Quotes, bars, provider health, ingestion |
| Market boards | `GET /market/screener`, `/market/orderblock`, `/market/tradingwatch`, `/market/tradingwatch/replay/{instrument_id}` | Screeners and structure/momentum analysis |
| Events | `GET /market/events`, `/market/events/sources`, `POST /market/events/refresh` | Evidence-backed event feed |
| Discovery and watchlists | `GET /market/discovery/candidates`, `POST /market/discovery/cycle`, `GET|POST /market/watchlists` | Candidate generation and persisted lists |
| Forecasts | `GET|POST /market/forecasts`, `/market/forecasts/models`, `POST /market/forecasts/train`, `POST /market/forecasts/evaluate` | Probabilistic forecasts and evaluation |
| Wallets and ledger | `GET /market/wallet/status`, `GET|POST /market/wallets`, `POST /market/wallets/{wallet_id}/deposit`, `/withdraw`, `POST /market/wallet/reconcile` | NovaCoin, transfers, immutable entries |
| Portfolios | `GET|POST /market/portfolios`, `/market/portfolios/{portfolio_id}/holdings`, `/valuation`, `/performance`, `POST .../rebalance`, `POST .../apply` | Three horizon books |
| Risk | `GET /market/risk/status`, `/contracts`, `/decisions`, `/alerts`, `/policies`, `POST /market/risk/assess`, `POST /market/risk/stress` | Independent policy gate and alerts |
| Strategies | `GET /market/strategies/catalog`, `GET|POST /market/strategies`, `/proposals`, `POST /market/strategies/run` | Signals, lifecycle and proposals |
| Paper execution | `POST /market/execution/orders`, `GET /market/execution/orders`, `POST .../process`, `POST .../cancel`, `GET /market/execution/fills`, `POST /market/execution/reconcile` | Simulated orders and fills |
| Backtesting | `POST /market/backtests`, `POST .../start`, `/results`, `/metrics`, `/verify`, `/walk-forward`, `/sensitivity` | Point-in-time replay |
| Shadow | `GET /market/shadow/status`, `GET|POST /market/shadow/sessions`, `POST .../start`, `/cycle`, `/track-outcomes`, `/analytics` | Non-executing observation |
| Learning | `GET /market/learning/status`, `POST /market/learning/evidence`, `/experiments`, `/candidates/{id}/approve`, `/promote`, `POST .../rollback` | Governed improvement |
| Overview and notifications | `GET /market/overview`, `POST /market/notifications/test` | Integrated operational overview and notification transport test |
| Workspace | `GET /market/workspace` | Legacy read-only HTML workspace; not the React UI |

## 5. Request and response conventions

- **Bodies:** JSON objects. FastAPI validates types/ranges; unknown or missing required fields produce `422`.
- **Responses:** JSON objects, usually with a domain-specific key (`orders`, `wallets`, `proposals`, `decisions`, `sessions`, etc.). There is no universal wrapper around every payload. Use the OpenAPI schema or [API.md](API.md).
- **Identifiers:** canonical instruments are `MIC:SYMBOL` (for example `XNSE:RELIANCE`); records use opaque string ids. Treat ids as opaque and URL-encode path values.
- **Amounts:** decimal strings/values in the backend; do not perform authoritative arithmetic in a client. NovaCoin is 1 NC = ₹1 for paper accounting.
- **Timestamps:** ISO 8601 with timezone where reported. A quote/bar is valid only relative to its own `observed_at`/session timestamp.
- **Pagination:** endpoints that support it document `limit`/`offset`; limits are bounded by the implementation. Do not assume unbounded lists.
- **Idempotency:** wallet mutations and paper order submission accept idempotency keys where documented. Reuse the same key only for a retry of the same intended operation; a new operation needs a new key.
- **Content type:** send `Content-Type: application/json` for JSON bodies.
- **No bulk order API:** paper orders are created one at a time from an already-approved proposal.

## 6. Errors

| Status | Meaning |
|---|---|
| `401 Unauthorized` | `NOVACONTROL_API_TOKEN` is set and the bearer token is missing/invalid |
| `403 Forbidden` | Browser mutation origin rejected, or the operation is not permitted by the application guard |
| `404 Not Found` | Unknown route/resource id |
| `409 Conflict` | NovaMarket or the subsystem is not operational, or a lifecycle/state conflict prevents the action |
| `422 Unprocessable Entity` | Schema, type, range or domain validation failed |
| `500 Internal Server Error` | Unexpected failure; inspect server logs and status, do not retry a financial mutation blindly |

Typical error body:

```json
{
  "detail": "reason the backend refused the request"
}
```

A risk refusal is not necessarily an HTTP error: risk decisions are persisted domain outcomes (`rejected`, `restricted`, `requires_review`, `insufficient_data`). Read the decision payload and explanation. A paper order can also return a rejected order record with evidence rather than a transport failure.

## 7. Representative examples

The examples use `curl.exe` for Windows PowerShell. Add the bearer header only when authentication is enabled.

### Health and status

```powershell
curl.exe -s http://127.0.0.1:8011/health
curl.exe -s http://127.0.0.1:8011/market/status
curl.exe -s http://127.0.0.1:8011/market/execution/status
```

### Contract and configuration

```powershell
curl.exe -s http://127.0.0.1:8011/market/contracts
curl.exe -s http://127.0.0.1:8011/market/config
```

Configuration changes are full validated payloads. See [Configuration](CONFIGURATION.md) for all fields and the safe `POST` pattern.

### Instruments and quote

```powershell
curl.exe -s "http://127.0.0.1:8011/market/instruments?exchange=XNSE&limit=20"
curl.exe -s "http://127.0.0.1:8011/market/data/quote/XNSE:RELIANCE"
curl.exe -s "http://127.0.0.1:8011/market/data/bars/XNSE:RELIANCE?interval=1d&limit=20"
```

A quote response carries source, observation timestamp and freshness/quality fields. A missing instrument or empty history is a legitimate empty/404 result, not a fabricated quote. Colons in path values are valid; URL-encode other reserved characters.

### Screeners

```powershell
curl.exe -s "http://127.0.0.1:8011/market/screener?limit=25&available_only=true"
curl.exe -s "http://127.0.0.1:8011/market/orderblock?interval=1d&limit=25"
curl.exe -s "http://127.0.0.1:8011/market/tradingwatch?interval=1d&limit=25"
```

These boards read stored OHLCV. They do not create orders. Derivatives availability and model-derived IV must be labelled as such in the response/UI.

### Wallet and ledger

```powershell
# List wallets
curl.exe -s http://127.0.0.1:8011/market/wallets

# Create a wallet (body matches the implemented contract)
$body = @{ owner = "operator"; label = "Paper desk" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8011/market/wallets `
  -ContentType "application/json" -Body $body
```

A new wallet has zero balance. Deposit/withdraw use the wallet path and a body with `amount`, `reference`, `description` and `idempotency_key`:

```powershell
$deposit = @{
  amount = "100000.00"
  reference = "initial-paper-capital"
  description = "Simulated deposit"
  idempotency_key = "deposit-2026-11-09-001"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8011/market/wallets/<WALLET_ID>/deposit" `
  -ContentType "application/json" -Body $deposit
```

This is a simulated ledger posting. It does not contact a bank, broker or payment rail.

### Portfolio

```powershell
$book = @{
  horizon = "short_term"
  funding_wallet_id = "<WALLET_ID>"
  capital = "50000.00"
  name = "Short-term paper book"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8011/market/portfolios `
  -ContentType "application/json" -Body $book
```

Accepted horizons are `short_term`, `medium_term`, `long_term`. Funding is an explicit wallet-to-portfolio ledger movement. The API refuses insufficient funds and invalid horizon/amounts.

### Risk

```powershell
curl.exe -s http://127.0.0.1:8011/market/risk/status
curl.exe -s http://127.0.0.1:8011/market/risk/policies
curl.exe -s http://127.0.0.1:8011/market/risk/decisions
```

Risk outcomes are `approved`, `rejected`, `restricted`, `requires_review`, `insufficient_data`. An approved decision is bound to the proposal it assessed; it is not a permanent permission to trade.

## 8. Paper-order example

Paper orders are created only from an approved, unexpired strategy proposal. First find a usable proposal:

```powershell
curl.exe -s "http://127.0.0.1:8011/market/strategies/proposals?status=approved&limit=20"
```

Then submit:

```powershell
$order = @{
  proposal_id = "<PROPOSAL_ID>"
  quantity = "10"
  order_type = "limit"
  limit_price = "2450.50"
  trigger_price = $null
  expires_at = "2026-11-10T09:15:00Z"
  idempotency_key = "paper-order-2026-11-09-001"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8011/market/execution/orders `
  -Headers @{ Authorization = "Bearer $env:NOVACONTROL_API_TOKEN" } `
  -ContentType "application/json" -Body $order
```

Implemented order types: `market`, `limit`, `stop`, `stop_limit`. A limit needs a positive limit price; stop types need a trigger. Omit `quantity` to use the proposal's quantity where supported. The UI exposes the same fields and refuses the form until required conditional prices are present.

Order lifecycle:

`created → pending_validation → accepted → open → partially_filled → filled`

with `cancel_pending`, `cancelled`, `rejected`, `expired`, `failed` as alternate/terminal states. Process an open order or cancel its remainder:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8011/market/execution/orders/<ORDER_ID>/process" `
  -ContentType "application/json" -Body "{}"

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8011/market/execution/orders/<ORDER_ID>/cancel" `
  -ContentType "application/json" -Body '{"reason":"operator cancellation"}'
```

`process` is a **simulation** step. It never calls a broker. Cancellation preserves completed fills and releases unfilled reservations. Reconcile after fills:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8011/market/execution/reconcile `
  -ContentType "application/json" -Body "{}"
```

Reconciliation compares paper effects with the immutable ledger and reports differences; it does not repair or delete evidence.

## 9. Safety and status endpoints

These are the authoritative runtime safety surfaces:

| Endpoint | Important fields |
|---|---|
| `GET /market/execution/status` | `paper_only: true`, `live_trading: false`, `broker_connected: false`, `order_types`, `statuses`, counts, fee version |
| `GET /market/shadow/status` | `simulated: true`, `real_money: false`, `execution_connected: false`, `nm13_activated: false` |
| `GET /market/learning/status` | `real_money: false`, `execution_connected: false`, `executes_trades: false`, `isolated: true` |
| `GET /market/backtests/status` | isolation/`real_money` guarantees and run counts |
| `GET /market/contracts` | lifecycle, permission matrix, horizons, domains and storage |

`GET /market/status` reports lifecycle, configuration, capabilities and subsystem status. Its **top level** does not repeat `paper_only` or `broker_connected`; read the execution status and contracts for those fields. Do not invent a field that is not returned.

## 10. Keeping the reference synchronized

`docs/API.md` contains a generated block bounded by:

```text
<!-- BEGIN GENERATED: api-reference ... -->
<!-- END GENERATED: api-reference ... -->
```

Regenerate and verify from `novamarket/`:

```powershell
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe scripts\generate_api_reference.py
..\.venv\Scripts\python.exe scripts\generate_api_reference.py --check
```

The check must pass before a release. A new route requires all of:

1. the handler in `src/novacontrol/api/app.py`;
2. its declaration in the `ApiSurface` registry (`src/novacontrol/api/models.py`);
3. its consumer declaration in `src/novacontrol/api/route_consumers.py`;
4. regenerated `docs/API.md`;
5. a test covering the route/contract.

No route is considered documented merely because it appears in Swagger: the generated reference and the test suite must agree. The accepted baseline's check passed and the generated reference contains the `/market` operations described here.

### Route-count note

The NM-14 report describes "372 NovaMarket routes" in its acceptance wording. The running OpenAPI document currently exposes **200 `/market` paths / 214 operations**; the generated reference's route list is the authoritative inventory. The difference is a counting convention (registered entries versus path operations), not missing endpoints or added functionality. Documentation therefore describes route families and links the generated list instead of using one ambiguous total.
