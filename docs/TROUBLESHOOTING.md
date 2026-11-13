# Troubleshooting

This guide covers verified failure modes of the accepted NovaMarket build. Diagnose the cause before changing configuration or data. **Never bypass risk controls, the origin guard, the paper-only boundary or the ledger.**

> **Safety:** do not delete a database, edit a balance, promote a candidate by hand or add a broker connection to "fix" a symptom. Those actions destroy evidence and are outside this build's contract.

## Contents

1. [First checks](#1-first-checks)
2. [Backend does not start](#2-backend-does-not-start)
3. [Frontend does not start](#3-frontend-does-not-start)
4. [API connection, 401, 403 and CORS](#4-api-connection-401-403-and-cors)
5. [Database initialization and locking](#5-database-initialization-and-locking)
6. [Missing or stale market data](#6-missing-or-stale-market-data)
7. [Empty history or instrument gaps](#7-empty-history-or-instrument-gaps)
8. [Missing sector classification](#8-missing-sector-classification)
9. [Unavailable derivatives data or unexpected IV](#9-unavailable-derivatives-data-or-unexpected-iv)
10. [Wallet, portfolio and reconciliation issues](#10-wallet-portfolio-and-reconciliation-issues)
11. [Paper-order rejection](#11-paper-order-rejection)
12. [Insufficient funds](#12-insufficient-funds)
13. [Risk-policy rejection or insufficient data](#13-risk-policy-rejection-or-insufficient-data)
14. [Backtest and shadow problems](#14-backtest-and-shadow-problems)
15. [Build and dependency errors](#15-build-and-dependency-errors)
16. [Stale UI and version confusion](#16-stale-ui-and-version-confusion)
17. [Notifications and email](#17-notifications-and-email)
18. [Diagnostic command reference](#18-diagnostic-command-reference)

## 1. First checks

```powershell
curl.exe -s http://127.0.0.1:8011/health
curl.exe -s http://127.0.0.1:8011/market/status
curl.exe -s http://127.0.0.1:8011/market/execution/status
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:5173/
```

Read the **first real failure**, not a downstream empty card. `GET /market/status` includes lifecycle/health/config; `/market/execution/status` is the paper-only safety surface. `/health` is public even when a token protects other routes.

If a token is configured, add `Authorization: Bearer <token>` to protected checks. Do not print a real token in a terminal transcript or screenshot.

## 2. Backend does not start

### Symptoms

- `ModuleNotFoundError: novacontrol`
- `No such file ... .venv\Scripts\python.exe`
- `[Errno 10048]` / `EADDRINUSE`
- Uvicorn exits before `/health` responds
- `NovaMarket` is constructed but status is not operational

### Likely causes and diagnosis

| Cause | Check |
|---|---|
| Wrong interpreter / missing package | `..\.venv\Scripts\python.exe -c "import novacontrol, fastapi"` |
| `PYTHONPATH` missing | Run from `novamarket` with `$env:PYTHONPATH="src"`, or use `start.ps1` |
| Port already used | `Get-NetTCPConnection -LocalPort 8011 -State Listen` and inspect the owning process |
| Parent `.venv` missing/moved | `Test-Path ..\.venv\Scripts\python.exe` |
| Invalid configuration | `GET /market/status` `config_error`, or start logs; config schema errors name the field |
| Initialization failure | Read the `POST /market/enable` response / server traceback; do not treat it as ready |

### Safe resolution

- Use the launcher's expected parent venv and install command; do not copy a `.venv` from another machine.
- Choose another port with `.\start.ps1 --api-port 8012` rather than killing an unrelated process. Stop only a process you identified as yours.
- Fix the named configuration field through `POST /market/config`; do not hand-edit the SQLite database.
- If NovaMarket is disabled, enable it from **System Health** or `POST /market/enable`; review the returned failure if initialization still fails.

## 3. Frontend does not start

### Symptoms

- `npm` not found or dependency install fails
- Vite port occupied
- Blank page, white screen, or module-resolution error
- UI loads but every API card fails

### Diagnosis

```powershell
node --version
npm --version
cd web
npm install
npm run dev
```

Check:

- Node is 18+; the accepted environment used Node 24.19/npm 11.17.
- `node_modules` exists and matches the lockfile.
- The Vite terminal shows the local URL and no TypeScript error.
- The API is running and the proxy target is correct (`NOVAMARKET_API_ORIGIN`, default `http://127.0.0.1:8011`).

### Safe resolution

- Reinstall with `npm install` (or `npm ci` for a clean lockfile install).
- Choose another web port: `.\start.ps1 --web-port 5174`, or `npm run dev -- --port 5175`.
- Do not change the API to a remote broker; NovaMarket has none.
- A UI-only restart does not restart the API.

## 4. API connection, 401, 403 and CORS

### 401 Unauthorized

**Cause:** `NOVACONTROL_API_TOKEN` is set and the request lacks a valid bearer header.

**Fix:** enter the token in the in-app dialog or send:

```powershell
curl.exe -s -H "Authorization: Bearer $env:NOVACONTROL_API_TOKEN" http://127.0.0.1:8011/market/status
```

The token is session-only in the browser. Clearing it or opening a new session may require re-entry.

### 403 Forbidden

**Cause:** a browser mutation's `Origin` is not allowed, or the operation is refused by the application guard.

**Fix:** set `NOVAMARKET_CORS_ORIGINS` to the exact page origin (for example `http://127.0.0.1:5173`) and, where used, `NOVAMARKET_ALLOWED_ORIGINS`. Do not use `*`, disable the guard, or disable authentication to silence it.

### No response / network error

- API not running or listening on a different port.
- Vite proxy target wrong.
- A built frontend has `VITE_API_BASE_URL` empty while served from a different origin.
- TLS/reverse-proxy not forwarding `/market`.
- Browser console shows a CORS preflight failure even if a direct curl succeeds.

Check `/health` directly, then the browser network tab. A direct 200 plus a browser failure usually indicates origin/proxy configuration.

## 5. Database initialization and locking

### Symptoms

- Tables/columns missing in a custom data directory
- `database is locked`
- Empty cards after switching data directories
- Permission error when creating a database

### Causes

- The app was started with a different working/data directory than expected.
- Two API processes are writing the same SQLite file.
- The directory is read-only or the file is open by a backup/sync tool.
- A custom path was pointed at an unreadable location.

### Safe resolution

- Confirm the actual data directory used by the running service (`/market/status`, `/market/config`). The accepted launcher uses `novamarket/data`.
- Stop the extra API process; do not run two writers against the same file.
- Ensure the directory is writable by your user. Do not use administrator privileges unless genuinely required.
- Back up while stopped. Do not delete the SQLite file to "reset" a financial issue; that erases the ledger.
- For a new environment, allow the service to create its schema; do not copy `.ui_audit` into runtime data.

## 6. Missing or stale market data

### Symptoms

- Market Overview is empty or a row says unavailable.
- Quote exists but is delayed/stale.
- `GET /market/data/quote/{instrument_id}` returns an error/empty payload.
- "Fetch latest" does not change data.

### Diagnosis

```powershell
curl.exe -s http://127.0.0.1:8011/market/data/status
curl.exe -s "http://127.0.0.1:8011/market/data/providers?probe=true"
curl.exe -s "http://127.0.0.1:8011/market/data/quote/XNSE:RELIANCE"
```

Check provider health, observed timestamp, freshness and quality. A provider may be rate-limited, delayed or closed; an empty catalogue may simply not have been synced.

### Safe resolution

- **Settings → Sync public universe**, or `POST /market/universe/sync`.
- Import an official bhavcopy for coverage; see `scripts/import_*.py --help`.
- Configure the correct provider/reference, not a fake value.
- Wait for a delayed provider to recover; do not lower a freshness threshold just to make stale data look fresh.
- Never insert synthetic bars or alter a timestamp to pass a check.

## 7. Empty history or instrument gaps

**Known accepted limitations:**

- 99 BSE instruments have no bars because their symbols were absent from the downloaded BSE session.
- 151 recent listings have only 3–29 bars.
- The full audit's 4,535-instrument coverage is not the default runtime store.
- No synthetic historical bars were created.

**Diagnosis:** inspect history count, first/last session/provider and data-quality output. Confirm the selected exchange/symbol and whether the official file actually contained it.

**Safe resolution:** import the correct official session or accept/display the gap. Do not backfill with made-up prices; do not claim a full-history result when coverage is short.

## 8. Missing sector classification

### Symptoms

Sector filters or sector flow show "Unclassified"/no rows.

### Causes

- The instrument was registered without sector metadata.
- The importer/provider did not supply a sector mapping.
- A symbol mismatch prevented the classification lookup.

### Diagnosis

Inspect the instrument record and its sector/industry fields; confirm the canonical `MIC:SYMBOL`. Compare with the source catalogue.

### Safe resolution

Register/update the instrument through the supported metadata path or importer. Do not assign a sector by guess in SQL. A missing classification is reported as unavailable; it is not silently assigned.

## 9. Unavailable derivatives data or unexpected IV

### Symptoms

- Index options/futures board is empty or reports no snapshot.
- Contract counts/strikes differ from an earlier view.
- IV looks different from a broker/vendor value.

### Causes and facts

- The accepted official F&O snapshot is EOD for its trade date (6 November 2026 in the audit); it is not a live option chain.
- Only imported contracts are present; missing symbols/expiries are not fabricated.
- **Options IV is model-derived with Black-Scholes**, not provider-reported IV.
- The audit's 6,625 option contracts, 147 NIFTY strikes and PCR 1.6528 are isolated-database evidence, not current runtime data.
- Provider-reported IV is not implemented.

### Safe resolution

Import the correct official F&O bhavcopy/session and verify the trade date. Do not label model IV as provider IV. Do not invent missing strikes or extend an expiry. If comparing to a vendor, state the methodology difference.

## 10. Wallet, portfolio and reconciliation issues

### "Insufficient funds" when funding a portfolio

Check the selected wallet's **available** balance, existing reservations/commitments, currency/amount precision and whether the wallet is frozen. A wallet's total and available balances can differ. Reconcile it before retrying. Do not deposit a fake balance or edit the DB.

### Wallet balance disagreement

1. Stop financial mutations.
2. Open the wallet's ledger and transaction history.
3. Run wallet reconciliation and paper execution reconciliation.
4. Compare transaction ids/entries; a correction should be a posted reversal.
5. Back up the database before any maintenance.

Reconciliation reports differences; it does not repair them. A real discrepancy is an implementation evidence issue, not a UI refresh problem.

### Portfolio value says "not funded" / holdings missing

A book is only funded through an explicit ledger movement. Check the creation/funding response, wallet transaction and portfolio ID. A position appears only after a recorded fill/apply, not from a proposal or shadow projection.

### Rebalance proposal cannot be applied

Only a `validated`, unexpired proposal can be applied. A stale/superseded/rejected proposal is not retryable as-is; create a new proposal. Applying a proposal is not a broker order.

## 11. Paper-order rejection

### Symptoms

- `Submit paper order` returns an error or a rejected order.
- Order is `open` but has no fill.
- Order is `partially_filled` and the remainder is uncertain.
- Order is expired.

### Verified causes

- proposal is not approved, expired or already forwarded;
- risk decision is no longer current/eligible;
- invalid quantity, limit/trigger price or order type;
- insufficient wallet/portfolio cash or reservation;
- stale quote/no fresh data; market order refuses old data;
- insufficient liquidity/participation;
- operation not operational (409) or authentication/origin failure;
- the proposal belongs to a different portfolio/order context.

### Diagnosis

Read the order detail, transitions and rejection reason; check `/market/execution/status`, the proposal's status/expiry and the quote timestamp. Check `/market/execution/fees` and `/slippage` for assumptions, not as a reason to change risk.

### Safe resolution

Create a fresh valid proposal/order with correct prices/quantity and a new idempotency key for a new intent. If data is stale, refresh the source; if risk refused, address the policy condition. **Never** bypass the risk gate or set a fake fill price by editing the database.

### Partial fill

A partial fill is expected under the participation/liquidity model. Inspect filled vs remaining quantity; process again only if the order remains eligible, or cancel the remainder. Completed fills are preserved.

## 12. Insufficient funds

Check:

- wallet **available** balance (not only total);
- portfolio cash and reservations;
- deposit status and ledger entries;
- amount precision (max 2 decimals for wallet operations);
- frozen/closed wallet status.

Post a simulated deposit or transfer through the wallet operation if you intentionally want more paper capital. Do not alter the ledger directly. A withdrawal/close may be refused when it would violate balance or open commitments.

## 13. Risk-policy rejection or insufficient data

### Rejection

Read the decision's `outcome`, `explanation` and policy version. Typical causes include position/sector concentration, cash floor, drawdown, volatility/liquidity, turnover, stale forecast/data or aggregate exposure.

**Do not** change a policy or delete a decision merely to make a proposal pass. If the policy is genuinely wrong for the intended scenario, change it through the supported versioned policy path and record the reason. Otherwise reduce/adjust the proposal or choose a suitable instrument.

### `insufficient_data`

The risk engine cannot form a defensible decision because a required input is missing/stale/insufficient. Fix the data/freshness or wait. It is not an approval and must not be treated as one.

### `requires_review` / `restricted`

Only the allowed/reduced scope may proceed. Do not submit the full intent and assume approval. Review conditions/evidence and use the supported override/review flow only where the implementation permits it.

## 14. Backtest and shadow problems

### Backtest

| Symptom | Check |
|---|---|
| Cannot create | Strategy catalogue/policy available? instruments and window provided? |
| No trades/equity curve | Data coverage, strategy signals, run actually completed |
| Different result | Config hash/seed, window, fees/slippage, policy snapshot |
| Data-quality warnings | Missing bars/short history; do not synthesize |
| Verification failure | Inspect reproduced hash/evidence; do not edit stored results |

A backtest is not a paper book. It should not change wallet/portfolio values. If it does, stop and report an implementation defect.

### Shadow

| Symptom | Check |
|---|---|
| New session disabled | Needs a portfolio and at least one strategy/instrument |
| No decisions | Start/cycle the session against available data |
| No outcomes | Later observations must exist; unresolved intervals are legitimate |
| Projection absent from portfolio | Correct: shadow is non-executing and never included in value |
| `execution_connected` not false | Stop and inspect status; the accepted baseline requires false |

Do not attach a broker/gateway to fix shadow behavior.

## 15. Build and dependency errors

### Python

- Ensure the parent `.venv` interpreter is used; `pip install -e ".[dev]"` from the correct project.
- A version conflict should be resolved using the declared project dependencies, not ad-hoc global installs.
- Mypy errors in both views (`src` and `--platform win32`) are code issues; do not suppress them to finish a documentation task.
- `pytest` import failures usually mean `PYTHONPATH=src` is missing.

### Frontend

```powershell
cd web
npm install
npm run build
```

- TypeScript errors fail the build; fix the type rather than disabling strictness.
- Vite's chunk-size warning is informational unless it causes a real loading problem.
- Node older than 18 is unsupported by Vite 6.
- Do not switch frameworks/build tools to work around a missing dependency.
- `npm run preview` serves static output and does not start the API.

## 16. Stale UI and version confusion

**Symptoms:** old labels/layout, sidebar missing, values from a previous page, changes not visible.

The API sends no-cache headers and cache-busts assets with a content-derived token. A VS Code/Blackbox/embedded browser may restore a stale DOM without refetching.

**Resolution:** fully close and reopen the tab (not just scroll/refresh), then compare the sidebar `UI v...` stamp. A sidebar-less render is a stale DOM; the current CSS always retains the sidebar (shrinking to 200px under 900px). If a fresh tab still disagrees, check the server response and `/health`; do not conclude the backend is broken from an embedded tab alone.

## 17. Notifications and email

- In-app notifications appear even without SMTP.
- A test email requires `NOVAMARKET_SMTP_HOST`, `FROM`, and credentials where the relay requires them; port/TLS must match.
- A failure is reported; it does not mean the notification was sent.
- Do not put the SMTP password in the UI, a screenshot, a repository file or an error report.
- A recipient address is not a secret, but avoid personal addresses in shared screenshots.

## 18. Diagnostic command reference

```powershell
# From novamarket/
$env:PYTHONPATH = "src"

# API/service
curl.exe -s http://127.0.0.1:8011/health
curl.exe -s http://127.0.0.1:8011/market/status
curl.exe -s http://127.0.0.1:8011/market/data/status
curl.exe -s http://127.0.0.1:8011/market/execution/status
curl.exe -s http://127.0.0.1:8011/market/risk/status
curl.exe -s http://127.0.0.1:8011/market/contracts

# Focused tests/type checks
..\.venv\Scripts\python.exe -m pytest tests/test_novamarket*.py -q
..\.venv\Scripts\python.exe -m mypy src
..\.venv\Scripts\python.exe -m mypy src --platform win32

# API reference sync
..\.venv\Scripts\python.exe scripts\generate_api_reference.py --check

# Frontend
cd web
npm run build
```

If a check fails, preserve the command's real exit status and read the first error. Do not pipe a failing test into `head`/`tail` and call it green. Report unresolved failures; do not hide them by skipping tests or weakening assertions.

For setup, see [Installation](INSTALLATION.md). For the full lifecycle, see [Paper Trading Guide](PAPER_TRADING_GUIDE.md).
