# Configuration Reference

NovaMarket configuration is validated, versioned and secret-free. This reference documents the settings exposed by the running API and the environment variables read by the server, Vite proxy and browser build.

> **Paper-only:** no configuration value creates a broker connection or enables real-money trading. `mode: "live"` is refused by the schema. Do not put credentials in `data/novamarket.json`, frontend files or documentation.

## Contents

1. [Configuration sources and precedence](#1-configuration-sources-and-precedence)
2. [Server environment variables](#2-server-environment-variables)
3. [Core NovaMarket settings](#3-core-novamarket-settings)
4. [Ingestion settings](#4-ingestion-settings)
5. [Freshness thresholds](#5-freshness-thresholds)
6. [Data-quality thresholds](#6-data-quality-thresholds)
7. [Cache settings](#7-cache-settings)
8. [Notifications and SMTP](#8-notifications-and-smtp)
9. [Provider credentials](#9-provider-credentials)
10. [Paper-execution assumptions](#10-paper-execution-assumptions)
11. [Database and file locations](#11-database-and-file-locations)
12. [Frontend configuration](#12-frontend-configuration)
13. [Security settings](#13-security-settings)
14. [Logging and observability](#14-logging-and-observability)
15. [Inspecting and changing configuration](#15-inspecting-and-changing-configuration)

## 1. Configuration sources and precedence

| Layer | Source | Behaviour |
|---|---|---|
| Application config | `GET /market/config`, posted through `POST /market/config`; persisted at the configured data directory (`data/novamarket.json`) | Validated by `NovaMarketConfig`; unknown fields and secret-shaped fields are refused |
| Server environment | Process environment at API start | Read by the shared FastAPI/auth layer and optional SMTP transport |
| Frontend build/dev environment | `NOVAMARKET_API_ORIGIN` at Vite dev-server start; `VITE_API_BASE_URL` at build time | Changes the API target/origin; never provides server authority |
| Local Vite proxy | `NOVACONTROL_ADMIN_TOKEN` at Vite dev-server start | Supplies the loopback-only admin session for `/market` and `/events`; the credential stays in the dev server, not the browser bundle |
| Runtime browser token | `sessionStorage["novamarket.apiToken"]` | A session credential entered in the UI for separately hosted frontends; never persisted in `localStorage` or source |
| Project `.env` | `.env` beside `novamarket/` (gitignored) | Read by the API at startup and by `start.ps1`. Real process variables always win; a file value only fills a variable that is not already set. Accepts `NAME=value`, `export NAME=value` and PowerShell `$env:NAME = "value"`. Values are never returned by any API and names only are applied. Ignored under pytest and when `NOVACONTROL_SKIP_DOTENV=1`. |

The stored payload has `schema_version: 1` and `contract_version: "1.0.0"` in the accepted baseline. A newer/incompatible schema is refused rather than best-effort parsed. `POST /market/config` is a full validated configuration application; inspect the current payload before editing it.

## 2. Server environment variables

These are verified names. A local first run needs either `NOVACONTROL_API_TOKEN` (or the
launcher-generated `NOVACONTROL_ADMIN_TOKEN`) or the explicit tokenless-loopback opt-in
`NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1`; a production-like configuration requires the API token.

| Variable | Purpose | Default | Accepted / notes |
|---|---|---|---|
| `NOVACONTROL_API_TOKEN` | Shared bearer token for protected API routes | unset | Required (32+ random characters) for production-like/non-loopback deployments; 16+ when `NOVACONTROL_REQUIRE_AUTH=1`; `/health` stays public. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Send `Authorization: Bearer <token>` |
| `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN` | Dedicated token for autonomy mutations (`start`, `pause`, `resume`, `stop`, `recover`, decision/execution/exit runs) | unset | Must be a non-empty secret distinct from `NOVACONTROL_API_TOKEN`; a missing or equal value fails closed with 403. Send `X-Autonomy-Operator-Token: <token>` with ordinary authentication. Never put it in frontend source or `VITE_*`. |
| `NOVACONTROL_ADMIN_TOKEN` | Local admin credential accepted only from loopback clients | unset in the backend; `start.ps1` generates a random 64-hex value once and stores it in `.runtime/admin-token` | The old `narasimha-novacontrol` literal is refused unless the owner opts in with `NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN=1` (next row). Acts as both the ordinary API credential and the autonomy-operator credential for `127.0.0.1` requests, from either `Authorization: Bearer <key>` or `X-Autonomy-Operator-Token: <key>`. The Vite dev proxy sends it automatically; a key pasted into the API dialog also unlocks operator actions. Remote clients are always refused this credential; a deployed backend still requires the two configured tokens above. |
| `NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN` | Development-only opt-in that accepts the retired launcher literal as an EXTRA loopback admin credential | `0` | `1` permits exactly `narasimha-novacontrol`, only for loopback clients and only outside a production-like environment; it is ignored (and the literal still refused) when `NOVACONTROL_ENV` is production-like. Every other weak secret remains refused. |
| `NOVACONTROL_ALLOW_ANONYMOUS_LOCAL` | Explicit tokenless loopback development | `0` | Only `1` enables anonymous access, and only when no API/admin token is set, the environment is development/local/test, and every trusted host is loopback. Production-like or exposed configurations refuse to start. |
| `NOVACONTROL_REQUIRE_AUTH` | Force authentication in a development environment | `0` | `1` requires `NOVACONTROL_API_TOKEN` (16+ characters) or `NOVACONTROL_ADMIN_TOKEN`; missing credentials fail startup. |
| `NOVACONTROL_ENV` | Deployment classification | `development` | Any value outside `dev`, `development`, `local`, `test`, `testing` is production-like: API token required. |
| `NOVAMARKET_CORS_ORIGINS` | Exact browser origins allowed by CORS | empty | Comma-separated origins, e.g. `http://127.0.0.1:5173`; exact match, no wildcard |
| `NOVAMARKET_ALLOWED_ORIGINS` | Additional exact origins accepted by the application's mutation origin guard | built-in loopback entries, including `http://127.0.0.1:5173` | Comma-separated; do not disable the guard |
| `NOVACONTROL_TRUSTED_HOSTS` | Accepted `Host` headers | `127.0.0.1,localhost,::1,testserver` | Comma-separated host names; keep loopback values for local use |
| `NOVAMARKET_SMTP_HOST` | SMTP server hostname | unset | Required only for test/notification email; may come from the process environment or the project `.env` |
| `NOVAMARKET_SMTP_PORT` | SMTP port | `587` | Integer; transport uses the configured TLS policy |
| `NOVAMARKET_SMTP_USER` | SMTP username | unset | Required by most authenticated relays |
| `NOVAMARKET_SMTP_PASSWORD` | SMTP password | unset | Secret; supply through the process environment, never a committed file |
| `NOVAMARKET_SMTP_FROM` | From address | unset | Valid email address |
| `NOVAMARKET_SMTP_TLS` | STARTTLS/TLS behaviour | `true` | Boolean (`true`/`false`) |
| `NOVAMARKET_API_ORIGIN` | Vite dev proxy target | `http://127.0.0.1:8011` | Loopback HTTP origin used by `web/vite.config.ts` |
| `VITE_API_BASE_URL` | Build-time frontend API base | empty | Optional absolute API origin; empty means same-origin/relative requests |
| `PYTHONPATH` | Module search path for manual backend startup | unset | Set to `src` when launching from `novamarket/` outside `start.ps1` |

`NOVAMARKET_API_TOKEN` is **not** a documented variable in this build. API authentication uses the shared `NOVACONTROL_API_TOKEN`.

> **Secret handling:** set `NOVAMARKET_SMTP_PASSWORD` and any provider key in the process environment or the operator's secret store. Do not paste them into chat, screenshots, logs, `README.md` or `web/.env` committed to source control. The frontend receives only the token the user types; it never receives server SMTP/provider secrets.

PowerShell session example:

```powershell
$env:NOVACONTROL_API_TOKEN = "use-a-long-random-local-token"
$env:NOVACONTROL_AUTONOMY_OPERATOR_TOKEN = "use-a-different-long-random-local-token"
# The loopback-only key used by the backend and the local Vite proxy. The
# launcher generates it; export the same value only if you run Vite yourself.
# $env:NOVACONTROL_ADMIN_TOKEN = "<64 random hex characters from .runtime/admin-token>"
$env:NOVAMARKET_CORS_ORIGINS = "http://127.0.0.1:5173"
$env:NOVAMARKET_API_ORIGIN = "http://127.0.0.1:8011"
```

## 3. Core NovaMarket settings

Defaults below are from the live `GET /market/config` and `NovaMarketConfig`.

| Field | Default | Accepted values / meaning | Required? |
|---|---|---|---|
| `enabled` | `true` | boolean; allows the subsystem to run | No |
| `mode` | `"observe"` | only `observe` is implemented. `paper` is reserved by the config schema for a future phase; `live` is refused because no real-money trading exists. Paper execution is the separate NM-10 subsystem and works in `observe` mode | No |
| `data_providers` | `[]` | list of up to 8 short provider references; names only, no URLs/keys | No |
| `credentials_ref` | `""` | `env:NAME` or `store:NAME`; a reference, never a key | No |
| `provider_credentials` | `{}` | map of provider name → `env:NAME` / `store:NAME` reference (max 8) | No |
| `universe` | `[]` | list of canonical `MIC:SYMBOL` instrument ids; length ≤ `resources.max_assets` | No |
| `stale_after_seconds` | `900` | 1–604800; coarse compatibility window; the detailed `freshness` rules are authoritative | No |
| `resources.max_assets` | `50` | 1–10000 | No |
| `resources.max_quotes_per_asset` | `500` | 1–100000 | No |
| `resources.max_concurrent_fetches` | `2` | 1–16 | No |
| `history_page_limit` | `200` | 1–1000; default history page size | No |
| `log_level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | No |
| `emit_events` | `true` | boolean; publish lifecycle events | No |
| `notification_email` | `""` | one plain email address or blank | No |
| `notifications_enabled` | `true` | boolean master switch | No |
| `notify_market_open` | `true` | boolean; NSE/BSE pre-open alert | No |
| `notify_market_close` | `true` | boolean; NSE/BSE pre-close alert | No |
| `profit_alert_pct` | `5.0` | 0.1–100; per-instrument alert threshold | No |
| `loss_alert_pct` | `3.0` | 0.1–100; per-instrument alert threshold | No |
| `alert_lead_minutes` | `10` | 5–30; minutes before open/close | No |
| `paused` | `false` | boolean; pauses the continuous monitor without deleting records | No |
| `contract_version` | `"1.0.0"` | must equal the running build's contract | No* |
| `schema_version` | `1` | must equal the running build's schema | No* |

\*These identify the stored contract. Do not hand-edit them; a mismatch is a validation error by design.

Unknown top-level fields, unknown nested fields, duplicate provider/universe entries, invalid instrument ids and secret-shaped field names are refused. A typo cannot silently become "configured".

## 4. Ingestion settings

Background ingestion is **off** by default. Enabling it is an explicit operator action.

| Field | Default | Range / notes |
|---|---|---|
| `ingestion.enabled` | `false` | boolean |
| `ingestion.poll_seconds` | `900` | 15–86400 |
| `ingestion.intervals` | `["1d"]` | 1–4 unique values from `5m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M` |
| `ingestion.include_quotes` | `true` | quotes or bars must be selected |
| `ingestion.include_bars` | `true` | quotes or bars must be selected |
| `ingestion.run_when_closed` | `false` | skip closed-exchange cycles; crypto remains continuous |
| `ingestion.allow_partial_bars` | `false` | do not store partial bars unless explicitly enabled |
| `ingestion.max_concurrency` | `2` | 1–16; cannot exceed `resources.max_concurrent_fetches` |
| `ingestion.history_limit` | `250` | 1–5000 |
| `ingestion.jitter_seconds` | `30` | 0–300; avoids synchronized polling |

The worker never overlaps two cycles. A disabled worker means the UI reads stored data only; it does not mean the market is closed.

## 5. Freshness thresholds

Freshness is computed on every read from the observation timestamp; a cache entry is never reported as fresher than its source.

| Field | Default | Range |
|---|---|---|
| `freshness.spot_fresh_seconds` | `120` | 1–86400 |
| `freshness.spot_delayed_seconds` | `900` | 1–604800; ≥ fresh |
| `freshness.intraday_fresh_bars` | `1` | 1–100 |
| `freshness.intraday_delayed_bars` | `3` | 1–1000 |
| `freshness.intraday_stale_bars` | `10` | 1–10000; ≥ delayed |
| `freshness.daily_delayed_sessions` | `2` | 1–100 |
| `freshness.daily_stale_sessions` | `5` | 1–1000; ≥ delayed |
| `freshness.weekly_stale_days` | `21` | 1–3650 |
| `freshness.monthly_stale_days` | `90` | 1–3650 |
| `freshness.closed_market_max_age_days` | `7` | 1–365 |
| `freshness.provider_delay_grace_seconds` | `60` | 0–3600 |

Spot classifications are `FRESH` → `DELAYED` → `STALE`. A closed market's last completed session is `CLOSED_MARKET`, not stale, while younger than the configured maximum. These fields do not guarantee a provider is live.

## 6. Data-quality thresholds

| Field | Default | Range | Meaning |
|---|---|---|---|
| `quality.jump_warning_ratio` | `0.20` | 0–10 | 20% move triggers a warning |
| `quality.jump_suspect_ratio` | `0.50` | 0–100; ≥ warning | 50% move is suspect and needs evaluation |
| `quality.volume_warning_multiple` | `10.0` | 1–10000 | Volume multiple over median triggers warning |
| `quality.volume_suspect_multiple` | `50.0` | 1–1000000; ≥ warning | Volume multiple is suspect |
| `quality.reconciliation_tolerance_ratio` | `0.005` | 0–1 | 0.5% cross-provider tolerance |
| `quality.reconciliation_window_seconds` | `300` | 1–86400 | Comparison window |
| `quality.volume_median_window` | `20` | 3–500 | Median window for volume anomaly |

Thresholds flag data for review; they do not delete bars or "correct" a provider value.

## 7. Cache settings

| Field | Default | Range |
|---|---|---|
| `cache.quote_ttl_seconds` | `15` | 0–3600 |
| `cache.instrument_ttl_seconds` | `300` | 1–86400 |
| `cache.provider_ttl_seconds` | `60` | 1–86400 |

Only latest-quote/provider/instrument reads are cached. Historical observations read from the indexed SQLite store. Freshness is recomputed from the stored timestamp after a cache hit.

## 8. Notifications and SMTP

NovaMarket can record in-app notifications regardless of email. Email requires all of:

- `notifications_enabled: true` in NovaMarket config;
- a valid `notification_email` (or the notification's own recipient path, where supported);
- `NOVAMARKET_SMTP_HOST`, `NOVAMARKET_SMTP_FROM` and, for authenticated relays, `NOVAMARKET_SMTP_USER` / `NOVAMARKET_SMTP_PASSWORD`;
- an optional port/TLS setting matching the relay.

`POST /market/notifications/test` performs one real test email. Do not use a production password in a shared shell. A failed send is reported; the app does not claim success.

## 9. Provider credentials

Provider credentials use references, not values:

```json
{
  "data_providers": ["alpha-vantage"],
  "credentials_ref": "env:NAME",
  "provider_credentials": {
    "alpha-vantage": "env:NAME"
  }
}
```

- `env:NAME` reads the value from the process environment at use time.
- `store:NAME` refers to the operator's secret store.
- A raw key without a scheme is rejected with instructions to use a reference.
- The API returns only references; it does not resolve or expose the secret value.
- `data_providers` is a list of provider references, not URLs. Max 8. Duplicate entries are refused.
- Yahoo and CoinGecko do not require a key in the accepted build. Alpha Vantage is optional.

Never configure a "broker key" or "live trading" flag: no such setting is consumed by NovaMarket.

## 10. Paper-execution assumptions

Paper execution uses an immutable `ExecutionConfig` with version `nm10-exec-v1` in the accepted baseline. These are **simulation assumptions**, not exchange facts or broker quotes. Every order/fill records the version that produced it.

| Setting | Default | Meaning |
|---|---:|---|
| Participation rate | `0.10` | At most 10% of reference-session volume assumed available to one paper order |
| Minimum liquidity quantity | `0` | No additional minimum beyond validation |
| Spread | `8` bps | Synthetic spread approximation around the reference price |
| Impact coefficient | `12` bps | Square-root market-impact coefficient |
| Volatility weight | `0.35` | Weight applied to volatility in the impact model |
| Maximum slippage | `150` bps | Cap on simulated slippage |
| Stale quote limit | `300` s | Market order refuses older data |
| Require fresh data | `true` | Freshness is required for a market fill |
| Bar fallback | `false` | Historical bars are not silently substituted for a current quote |
| Default order TTL | `86,400` s | Used when a proposal supplies no expiry |
| Brokerage | `3` bps | Illustrative cash-equity component |
| Exchange | `0.297` bps | Illustrative component |
| SEBI | `0.01` bps | Illustrative component |
| Stamp duty | `0.015` bps | Illustrative component |
| GST | `18%` | Illustrative component |
| Minimum total fee | `0` | No minimum fee applied |
| Fee effective date | `2026-01-01` | Metadata, not a live tariff claim |

The full fee disclaimer is stored with the execution configuration: verify current broker/exchange tariffs before relying on exact costs. These values are not environment variables and are not changed by editing the UI; changing them is a code/config-version change that must preserve the paper-only boundary.

Risk policy limits are **data**, not environment settings. They are versioned by policy scope and can be read from `GET /market/risk/policies` (see [Paper Trading Guide](PAPER_TRADING_GUIDE.md)).

## 11. Database and file locations

| Path | Purpose |
|---|---|
| `data/novamarket.sqlite3` | Main market/financial/backtest state |
| `data/novamarket_shadow.sqlite3` | Shadow sessions and decisions |
| `data/novamarket_learning.sqlite3` | Learning evidence, experiments, candidates, versions |
| `data/novamarket.json` | Resolved NovaMarket configuration |
| `data/settings.json` | Shared NovaControl settings store, when used |
| `data/audit/audit.jsonl` | Shared audit trail |
| `.ui_audit/nse_import.sqlite3` | Isolated acceptance evidence; do not use as runtime state |
| `docs/API.md` | Generated reference, not runtime state |

All are relative to the configured data directory; the accepted launcher uses `novamarket/data`. NovaMarket does not run an external database server. Back up while stopped and include all three SQLite files plus the config if a complete restore is needed.

## 12. Frontend configuration

Development:

```powershell
$env:NOVAMARKET_API_ORIGIN = "http://127.0.0.1:8011"
cd web
npm run dev
```

`vite.config.ts` proxies `/market`, `/health` and related API paths to this origin. The default is `http://127.0.0.1:8011`.

Production build:

```powershell
$env:VITE_API_BASE_URL = "http://127.0.0.1:8011"
npm run build
```

- Leave `VITE_API_BASE_URL` empty when the built frontend is served from the same origin as the API or behind a reverse proxy that forwards `/market`.
- Vite embeds `VITE_*` values into client JavaScript. Never put a secret in a `VITE_` variable.
- The API token is not a build variable. The operator enters it at runtime; it lives in `sessionStorage` for that tab session.
- If the exact browser origin changes, update `NOVAMARKET_CORS_ORIGINS` (and the origin guard if needed) rather than disabling CORS.

## 13. Security settings

| Control | Setting |
|---|---|
| API authentication | `NOVACONTROL_API_TOKEN`; shared with NovaControl |
| Autonomy operator authentication | `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`; separate credential, held in Autonomy page memory only, never `sessionStorage` or `localStorage` |
| SSE authentication | `POST /events/session` exchanges the bearer/admin credential for an opaque, HttpOnly, SameSite=Strict cookie scoped to `/events`; `GET /events/stream` refuses query-string tokens |
| Anonymous local mode | `NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1` only; never inferred from a missing token, and refused for production-like/exposed deployments |
| Browser token storage | ordinary API token in `sessionStorage` key `novamarket.apiToken`; the operator token is not stored |
| CORS | `NOVAMARKET_CORS_ORIGINS` exact allowlist |
| Mutation origin guard | `NOVAMARKET_ALLOWED_ORIGINS` plus built-in loopback entries |
| Host header validation | `NOVACONTROL_TRUSTED_HOSTS` |
| Config secret rejection | `NovaMarketConfig` rejects secret-shaped field names and accepts only `env:`/`store:` references |
| Notification credentials | server-only SMTP environment variables |
| Paper-only | no broker setting exists; `live` is refused; execution reports `broker_connected: false` |

The API is not a multi-tenant authorization system. A bearer token grants access to the local application; do not expose the API to an untrusted network without a proper reverse proxy, TLS and network controls. Backtests, learning and shadow endpoints do not execute trades but still expose operational data.

## 14. Logging and observability

- `log_level` sets the application config's declared level; valid values are `DEBUG`–`CRITICAL`.
- `emit_events` controls lifecycle event publication.
- `/health`, `/market/status`, `/market/data/status` and `/market/execution/status` are the primary checks.
- Activity/audit and risk alerts are separate surfaces; an empty list is not proof that nothing happened.
- Provider latency/health is reported by `/market/data/providers` (and can be probed explicitly).
- Ingestion and reconciliation errors are surfaced in status/error fields; do not hide them by lowering log levels.
- Every HTTP response carries `X-Request-ID`; internal 5xx diagnostics are logged with the same id while the client receives a generic message.
- `/audit/status` reports `write_failures`, `last_write_error` and `degraded`; a failed audit append is logged at CRITICAL and is never reported as a successful audit record.

No log file path is promised by the app; capture stdout/stderr from the process that starts Uvicorn if you need persistent logs.

## 15. Inspecting and changing configuration

Read:

```powershell
curl.exe -s http://127.0.0.1:8011/market/config
```

If `NOVACONTROL_API_TOKEN` is set:

```powershell
$headers = @{ Authorization = "Bearer $env:NOVACONTROL_API_TOKEN" }
curl.exe -s -H "Authorization: Bearer $env:NOVACONTROL_API_TOKEN" http://127.0.0.1:8011/market/config
```

Change one value through the API (this sends a full configuration object; inspect first and preserve fields you do not intend to change):

```powershell
$body = @{
  mode = "observe"
  stale_after_seconds = 900
  paused = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8011/market/config" `
  -Headers @{ Authorization = "Bearer $env:NOVACONTROL_API_TOKEN" } `
  -ContentType "application/json" -Body $body
```

`POST /market/config` refuses an unknown field, an invalid range, a mismatched schema/contract, a raw secret or a reserved `mode`. Configuration changes are recorded; restart or reload status as indicated by the response. Do not edit the SQLite database to change a setting.
