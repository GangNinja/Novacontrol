# Developer Guide

This guide is for maintaining and extending the accepted NM-01–NM-14 baseline without breaking its architectural boundaries or its paper-only safety contract.

## Contents

1. [Repository structure](#1-repository-structure)
2. [Development setup](#2-development-setup)
3. [Backend development](#3-backend-development)
4. [Frontend development](#4-frontend-development)
5. [Adding a backend module](#5-adding-a-backend-module)
6. [Adding an API route](#6-adding-an-api-route)
7. [Adding a frontend page](#7-adding-a-frontend-page)
8. [Database and migration practices](#8-database-and-migration-practices)
9. [Writing and running tests](#9-writing-and-running-tests)
10. [Acceptance chain and API reference](#10-acceptance-chain-and-api-reference)
11. [Frontend build and type checking](#11-frontend-build-and-type-checking)
12. [Debugging and logging](#12-debugging-and-logging)
13. [Safe changes to financial and execution logic](#13-safe-changes-to-financial-and-execution-logic)
14. [Preserving architectural boundaries](#14-preserving-architectural-boundaries)

## 1. Repository structure

```text
novamarket/
├── src/novacontrol/novamarket/
│   ├── service.py             # composition root, lifecycle, cross-module orchestration
│   ├── config.py              # validated config schema/defaults
│   ├── contracts.py           # versions, errors, shared contracts
│   ├── marketdata/            # adapters, ingestion, store, universe, screener, derivatives
│   ├── radar/                 # market events and evidence
│   ├── discovery/             # candidates, watchlists, evaluation history
│   ├── forecast/              # models, calibration, performance
│   ├── finance/               # wallet, ledger, portfolios, valuation
│   ├── risk/                  # policy, decisions, alerts, stress
│   ├── strategy/              # catalogue, signals, proposals
│   ├── execution/             # paper orders, pricing, fees, fills, positions
│   ├── backtesting/           # historical replay, walk-forward, verification
│   ├── shadow/                # non-executing sessions and projections
│   └── learning/              # evidence, experiments, candidates, versions
├── src/novacontrol/api/       # FastAPI handlers, ApiSurface, route consumers
├── web/                       # React 19 + TypeScript + Vite
├── scripts/                   # importers, acceptance chain, API reference generation
├── tests/                     # pytest suite and evidence
├── docs/                      # this documentation and acceptance records
├── data/                      # runtime state (not source; usually gitignored)
├── start.ps1                  # Windows launcher (parent .venv)
└── .ui_audit/                 # isolated acceptance evidence, not runtime data
```

`novamarket` is composed by `NovaControlApplication`. It is not a separate Python server framework and must not create a second runtime, event bus, auth layer or database engine.

## 2. Development setup

Follow [Installation](INSTALLATION.md). The launcher expects the parent virtual environment:

```powershell
cd C:\path\to\NovaControl
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd novamarket
..\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd web
npm install
```

Run the API and frontend in separate terminals while developing:

```powershell
# Terminal 1, from novamarket/
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8011

# Terminal 2, from novamarket/web/
npm run dev
```

Use port **8001** or another free port for an agent/session preview if the user's canonical API is on 8000/8011. Do not start a second server on an occupied port or kill an unfamiliar process.

The frontend's Vite config proxies API paths to `NOVAMARKET_API_ORIGIN` (default `http://127.0.0.1:8011`). For a different API port, set it before starting Vite.

## 3. Backend development

Key composition points:

- `NovaMarketService` (`src/novacontrol/novamarket/service.py`) owns lazy subsystem construction, lifecycle/status and shared orchestration.
- `NovaControlApplication` wires the service into the runtime and API.
- `create_app()` in `src/novacontrol/api/app.py` registers routes and shared auth/CORS/origin behavior.
- Each domain package owns its store/schema and deterministic business rules.

Guidelines:

- Keep domain services free of FastAPI/React concerns.
- Use the shared event bus/audit/state store; do not add a parallel event system.
- Surface a not-operational or missing-data state as an explicit error/outcome. Do not return an empty success.
- Use `NovaMarketService` methods from handlers instead of constructing feature services in a route.
- Preserve exact-decimal financial types and existing transaction boundaries.
- Keep provider values source- and timestamp-labelled.

Hot reload is normally handled by restarting Uvicorn; the accepted app does not promise an auto-reload development server. A restart must not delete or rewrite the SQLite files.

## 4. Frontend development

- `src/nav.ts` is the route/navigation registry (25 routes in 7 groups).
- `src/App.tsx` is the route shell. Hash routes are resolved to page components there.
- `src/pages/*.tsx` contain page workflows.
- `src/api.ts` is the JSON client, token/session handling and error normalization.
- `src/components/*` contains shared UI primitives.
- `src/lib/format.ts` is the single formatting authority for money/dates/numbers.

The browser is presentation/orchestration. It must not calculate authoritative balances, positions, fees or risk eligibility. After a mutation, refresh the backend response and render that.

The Vite build runs TypeScript:

```powershell
cd web
npm run build
npm run preview
```

There is no configured frontend unit-test runner in `package.json`; `npm run build` (including `tsc -b`) is the current frontend verification. Do not invent a test command or a new framework as part of a documentation change.

## 5. Adding a backend module

NovaMarket phases are already implemented; do not invent NM-15. For a genuine maintenance need:

1. Define the domain contract and data ownership first. Decide which existing SQLite store owns the data and whether a separate store is necessary.
2. Add a package under `src/novacontrol/novamarket/<module>/` with deterministic service logic and tests.
3. Compose it lazily in `NovaMarketService`; expose a narrow method used by handlers. Do not instantiate it per request.
4. Add status/health/contracts only where the subsystem genuinely reports them. A missing probe is unknown, not healthy.
5. Add a permission/capability declaration if the operation can mutate state or requires a provider.
6. Add tests for success, refusal, missing data, idempotency and persistence/reload.
7. Document the behavior in the relevant guide; update the completion/acceptance record only if a new milestone is authorized.

Do not add a second ledger, risk table, execution engine, event system or configuration loader. Reuse the existing boundaries.

## 6. Adding an API route

A new route is done in **five places**; missing one fails synchronization/coverage checks:

1. **Handler** in `src/novacontrol/api/app.py`, with the correct auth dependency and status/error mapping.
2. **`ApiSurface`** row in `src/novacontrol/api/models.py`.
3. **Consumer declaration** in `src/novacontrol/api/route_consumers.py`.
4. **Generated reference** `docs/API.md` via `scripts/generate_api_reference.py`.
5. **Test** covering the route contract (success, refusal/validation, authorization/origin where relevant).

Route conventions:

- NovaMarket routes are under `/market`.
- Protected routes use the shared `require_auth`; never add a route-local token scheme.
- Mutations use the existing origin guard/CORS configuration; do not weaken it for convenience.
- Return the domain payload, not a new universal envelope. List endpoints use their documented key.
- Map domain/state errors to the established status codes (401/403/404/409/422); do not turn a refusal into a false success.
- Document request fields in the handler/OpenAPI description; do not hand-edit the generated route block.

Run:

```powershell
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe scripts\generate_api_reference.py
..\.venv\Scripts\python.exe scripts\generate_api_reference.py --check
```

`--check` must pass. Never add a live-order or broker route to satisfy a feature request; that is out of scope by contract.

## 7. Adding a frontend page

1. Create a page in `web/src/pages/` using existing layout/card/table/dialog primitives.
2. Add its label, icon and id to the appropriate group in `web/src/nav.ts`.
3. Register the id/page in `web/src/App.tsx` routing.
4. Add API reads/mutations through `src/api.ts`; add a hook/query only if the existing pattern supports it.
5. Use backend IDs and values; no local balance/price/fee arithmetic.
6. Include loading, error, empty and stale/unavailable states. Do not render fabricated placeholders.
7. Add a `useDocumentTitle` title and accessible labels where the existing page pattern does.
8. Verify with `npm run build` and a real browser interaction against the API. A screenshot is evidence only if genuinely captured.

If the page exposes a mutation, ensure it requires the same confirmation/risk gate as the API and does not bypass the existing service. Do not add a "live trading" toggle.

## 8. Database and migration practices

NovaMarket uses SQLite with idempotent schema creation; it does **not** use Alembic or an external database server in the accepted baseline.

- Main store: `data/novamarket.sqlite3` — market data, radar, discovery, forecasts, wallets, ledger, portfolios, risk, strategies, execution, backtests.
- Shadow store: `data/novamarket_shadow.sqlite3`.
- Learning store: `data/novamarket_learning.sqlite3`.
- Evidence: `.ui_audit/nse_import.sqlite3` is isolated, never runtime.

Rules:

- Add tables/columns in a backward-compatible, idempotent initialization path. Existing rows must remain readable.
- Never edit a live database by hand to change configuration, balances or statuses.
- Use transactions for multi-row financial mutations; preserve append-only histories and idempotency.
- Schema/version changes require a migration story and tests with a pre-existing database, not just a fresh one.
- Tests must use temporary directories, never the runtime `data/` or the user's home paths.
- Do not point a test at an absolute path outside the repository; use a parent that is a regular file for an unreadable-path case where portability matters.
- Do not include database files, audit evidence or secrets in a code commit.

Back up all three DBs plus `data/novamarket.json` while the app is stopped before an upgrade.

## 9. Writing and running tests

From `novamarket/`:

```powershell
$env:PYTHONPATH = "src"

# Fast NovaMarket-focused loop
..\.venv\Scripts\python.exe -m pytest tests/test_novamarket*.py tests/test_market_screener.py -q

# Full regression
..\.venv\Scripts\python.exe -m pytest tests/ -q

# Type checks (both CI views)
..\.venv\Scripts\python.exe -m mypy src
..\.venv\Scripts\python.exe -m mypy src --platform win32
```

Test principles:

- Test the contract, not only the happy path: invalid config, unknown fields, stale data, insufficient funds, risk refusal, expired proposal, duplicate idempotency key, missing provider.
- Assert persisted/reloaded state for financial operations.
- Keep time/data deterministic; use explicit clocks/fakes where the implementation supports them.
- Do not depend on the host's running app, network providers or existing `data/`.
- A test that changes a global class registry must isolate/restore it. `NovaControlApplication._HANDLERS` is per-instance for this reason.
- Preserve test portability: use `Path(...).name` rather than splitting on `\` or `/`; do not name absolute paths outside the tree.

A focused regression is appropriate for a documentation-only change; do not rerun the entire NM-14 acceptance suite merely to edit docs.

## 10. Acceptance chain and API reference

The accepted chain verifier and reference check:

```powershell
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe scripts\verify_novamarket_chain.py
..\.venv\Scripts\python.exe scripts\generate_api_reference.py --check
```

The chain verifier is the repository's acceptance-chain check; it does not create a broker connection. Use the accepted report for the historical 9 November evidence. Do not claim a new PASS from a partial run.

The generated API reference lives between markers in `docs/API.md`. Regenerate it after route changes; never hand-edit inside the generated block. The header may describe NovaMarket, but the generated block is authoritative.

## 11. Frontend build and type checking

```powershell
cd web
npm run build
```

The accepted build is Vite 6 + TypeScript 5.8. It emits static assets into `dist/`. A chunk-size warning (the accepted build warned about JS size) is not a build failure; investigate only if it becomes an actual performance issue.

```powershell
npm run preview -- --host 127.0.0.1 --port 4173
```

Preview serves static output; it does not start the API. Keep the API running and set `VITE_API_BASE_URL` at build time if the preview/API are different origins. Vite embeds `VITE_*` into client code, so never use one for a secret.

## 12. Debugging and logging

- Check `/health`, `/market/status`, `/market/data/status`, `/market/execution/status` before guessing.
- Check the browser console/network for 401/403/CORS and proxy failures.
- Use `/market/contracts` to verify permissions and subsystem declarations.
- Use activity/audit and execution reconciliation for financial evidence; do not rely only on a toast.
- Capture Uvicorn stdout/stderr if you need persistent server logs; no specific log-file path is promised.
- For provider issues, `GET /market/data/providers` shows declared health/verification; `probe=true` performs a live check.
- For a stale UI, close and reopen the tab; the server sends no-cache headers. A sidebar-less render is a stale DOM, not the current layout.
- Do not log tokens, SMTP passwords or provider keys. Error surfaces should include a reason, not a credential.

Debugging a financial mismatch: stop mutations, inspect order/fill/ledger entries, run reconciliation, back up the database, then diagnose. Never "fix" by deleting a row or changing a balance directly.

## 13. Safe changes to financial and execution logic

Any change in this area needs extra care and tests:

| Area | Non-negotiable |
|---|---|
| Risk | Independent, versioned, cannot be bypassed; preserve outcomes and policy version |
| Paper execution | Simulated only; no broker adapter or live route; preserve statuses/idempotency |
| Ledger | Exact decimals, append-only, balanced entries/reversals; never float authority |
| Portfolio | Funding is an explicit ledger movement; no invented cash |
| Fees/slippage | Versioned assumptions with disclaimer; every fill records the version |
| Market data | Source/time/quality provenance; no synthetic bars; missing remains missing |
| Backtest | Point-in-time, reproducible seed/hash/policy, no paper wallet mutation |
| Shadow | Non-executing; no wallet/ledger/portfolio/gateway |
| Learning | No execution, explicit governance, versioned/rollbackable promotion |
| API | No live-order route; auth/origin controls preserved; docs synchronized |

Do not change a risk limit just to make a test pass. Do not backfill missing bars to make a chart look complete. Do not add "temporary" direct order execution. If a real implementation issue is discovered, record it separately rather than weakening the safety contract for documentation convenience.

## 14. Preserving architectural boundaries

- **One composition root** (`NovaMarketService`); routes must not construct parallel services.
- **One risk gate**; strategy and execution consume its decisions.
- **One paper executor**; there is no live executor.
- **One ledger** per wallet/portfolio accounting domain; no second balance store.
- **Separate shadow/learning DBs**; keep their data out of paper financial state.
- **One market-data store** for observations/bars; no synthetic or duplicate source of truth.
- **One API surface registration**; five-place route changes.
- **One frontend API client and formatting layer**; no local authority arithmetic.

For system structure, see [Architecture](ARCHITECTURE.md); for operational symptoms, see [Troubleshooting](TROUBLESHOOTING.md).
