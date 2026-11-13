# Installation and Setup

This guide installs and starts the **accepted NM-01–NM-14 NovaMarket baseline**. It documents the verified Windows development path in this repository. It does not add deployment infrastructure, broker access or live trading.

## Contents

1. [Before you start](#1-before-you-start)
2. [Supported environment](#2-supported-environment)
3. [Repository layout](#3-repository-layout)
4. [Install Python dependencies](#4-install-python-dependencies)
5. [Install frontend dependencies](#5-install-frontend-dependencies)
6. [Configure the application](#6-configure-the-application)
7. [Create the databases](#7-create-the-databases)
8. [Configure market data](#8-configure-market-data)
9. [Start NovaMarket](#9-start-novamarket)
10. [Verify the installation](#10-verify-the-installation)
11. [First-run sequence](#11-first-run-sequence)
12. [Manual startup](#12-manual-startup)
13. [Common setup problems](#13-common-setup-problems)

## 1. Before you start

NovaMarket is a paper-only financial intelligence application. You do not need a broker account, brokerage credentials, a bank connection or real money.

You do need:

- the NovaMarket project source;
- Python and Node.js;
- network access for the initial dependency installation and, optionally, public market data;
- a modern Chromium/Firefox browser for the React UI.

> **Do not** configure live brokerage credentials. No such integration exists. Do not place API tokens, provider keys or passwords in a committed file.

## 2. Supported environment

### Verified platform

The repository's verified launcher is **Windows PowerShell**:

- Windows 10/11;
- PowerShell 5.1 or PowerShell 7;
- Python 3.12 or newer (the declared package requirement is `>=3.12`; acceptance used Python 3.13.0);
- Node.js 18 or newer for Vite 6; Node 20/22 LTS is recommended (acceptance used Node 24.19.0 and npm 11.17.0);
- a local browser.

Linux and macOS commands are not exercised by `start.ps1`. The Python/FastAPI and Vite code is generally portable, but this project does not claim a verified Linux/macOS installer. If you use another platform, treat it as an unverified development environment and use the manual startup section.

### Do not use

- the stale `novamarket/scripts/run_api.ps1` or `run_tests.ps1` as setup instructions: they reference a local `novamarket/.venv` and `unittest`, while the accepted launcher uses the parent `.venv` and pytest;
- a system-wide Python installation for project packages if you can use the project virtual environment;
- `npm run dev` without the API running (the UI will load but API cards will fail).

## 3. Repository layout

The accepted launcher expects this shape:

```text
NovaControl/                 # parent directory
├── .venv/                   # virtual environment used by start.ps1
├── pyproject.toml           # NovaControl parent project
├── src/novacontrol/         # NovaControl core runtime
└── novamarket/              # this project
    ├── pyproject.toml
    ├── start.ps1
    ├── src/novacontrol/novamarket/
    ├── web/
    ├── data/                # runtime state (usually gitignored)
    ├── docs/
    └── tests/
```

`novamarket/start.ps1` resolves the virtual environment as `<parent>/.venv/Scripts/python.exe`. If you move the project, preserve that relationship or edit the launcher locally for your environment.

## 4. Install Python dependencies

Open PowerShell in the **parent** directory (the directory that contains `novamarket`).

```powershell
cd C:\path\to\NovaControl

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# Parent runtime dependencies
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# NovaMarket package and its declared dependencies
cd novamarket
..\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

If your checkout is a standalone NovaMarket repository without the NovaControl parent runtime, use a parent-level virtual environment only if the parent project is present; otherwise adapt `start.ps1` to the environment you actually have. Do not copy a `.venv` between machines.

Confirm the interpreter:

```powershell
..\.venv\Scripts\python.exe --version
..\.venv\Scripts\python.exe -c "import fastapi, uvicorn, pydantic; print('runtime imports OK')"
```

The package also requires the NovaControl modules imported by `src/novacontrol/novamarket`. They are available when the parent project is installed or when `PYTHONPATH` includes both source trees. The launcher sets `PYTHONPATH` to `novamarket/src`.

## 5. Install frontend dependencies

```powershell
cd novamarket\web
npm install
```

The lockfile is `package-lock.json`. Prefer `npm install` for the documented path; use `npm ci` only when you want a clean lockfile-only install.

The frontend is a Vite application. Its `npm run dev` server proxies `/market`, `/health`, `/status` and the other API paths to the API origin. See [Configuration](CONFIGURATION.md) for `NOVAMARKET_API_ORIGIN` and the optional build-time `VITE_API_BASE_URL`.

## 6. Configure the application

NovaMarket works with its validated defaults. For a first run, the only optional setting is API authentication:

```powershell
# Optional: require a bearer token on protected API routes.
$env:NOVACONTROL_API_TOKEN = "replace-with-a-long-random-local-token"
```

`/health` remains public. If the variable is unset, the API relies on its loopback development posture and does not require a bearer token. Do **not** commit a token or put it in `web/.env`.

You can set the variable in the PowerShell session that starts the API, or use a local secret store and environment loader that is not committed. If you use an inline token in a terminal, remember that shell history may retain it.

Other commonly used variables:

| Variable | Purpose | Default |
|---|---|---|
| `NOVAMARKET_CORS_ORIGINS` | Comma-separated exact browser origins allowed to call the API | empty |
| `NOVAMARKET_ALLOWED_ORIGINS` | Additional exact origins accepted by the application's origin guard | loopback defaults, including `http://127.0.0.1:5173` |
| `NOVACONTROL_TRUSTED_HOSTS` | Accepted `Host` headers | `127.0.0.1,localhost,::1,testserver` |
| `NOVAMARKET_API_ORIGIN` | Vite proxy target | `http://127.0.0.1:8011` |
| `NOVAMARKET_SMTP_HOST` / `PORT` / `USER` / `PASSWORD` / `FROM` / `TLS` | Optional SMTP notification transport | unset / `587` / unset / unset / unset / `true` |

Full names, defaults and validation rules are in [Configuration](CONFIGURATION.md). Application settings such as forecast and risk policy are normally changed in **Settings** or `PATCH /market/config`, not by editing Python files.

## 7. Create the databases

NovaMarket creates its SQLite schema on first service use. There is no separate migration command or external database server for the accepted baseline.

The normal runtime creates:

```text
novamarket/data/novamarket.sqlite3
novamarket/data/novamarket_shadow.sqlite3
novamarket/data/novamarket_learning.sqlite3
novamarket/data/novamarket.json
novamarket/data/settings.json              # shared NovaControl settings when used
novamarket/data/audit/audit.jsonl          # shared audit trail
```

The exact `data` directory is the one the application is configured to use. Do not rename or replace a database while the service is running. Back up the directory while the application is stopped.

If the app starts but cards show empty lists, the database is not necessarily broken: a new database has no instruments, wallets, portfolios or orders. Use the first-run sequence below.

## 8. Configure market data

NovaMarket has two broad data paths:

1. **Provider adapters** — Yahoo Finance and CoinGecko are used without a required key; Alpha Vantage is optional and needs a credential reference configured through `market_data.providers`.
2. **Official exchange files** — import scripts under `scripts/` ingest NSE/BSE bhavcopy and NSE F&O bhavcopy data into the local store. These scripts are the path used for the official EOD acceptance evidence.

Inspect the available importers and their current options rather than guessing flags:

```powershell
Get-ChildItem scripts\import_*.py | Select-Object Name
..\.venv\Scripts\python.exe scripts\import_nse_bhavcopy.py --help
..\.venv\Scripts\python.exe scripts\import_bse_bhavcopy.py --help
..\.venv\Scripts\python.exe scripts\import_nse_fo_bhavcopy.py --help
```

Importers write to their own database path when the script requires one; read the help for the exact argument. The acceptance evidence used `.ui_audit/nse_import.sqlite3`, an **isolated audit database**, not the normal `data/novamarket.sqlite3`.

To create a basic runtime catalogue from the public universe, start NovaMarket and use:

- **Settings → Sync public universe**, or
- `POST /market/universe/sync`.

Optional global market data settings are available through `GET /market/config` / `PATCH /market/config`; provider credentials are references, not plaintext key fields in documentation or source control.

## 9. Start NovaMarket

From `novamarket/`:

```powershell
.\start.ps1
```

The launcher:

- checks prerequisites;
- reuses a healthy API already listening on the selected port;
- starts the API on `127.0.0.1:8011` by default;
- starts the Vite UI on `127.0.0.1:5173` by default;
- waits for HTTP readiness;
- opens the browser.

Useful modes:

```powershell
.\start.ps1 --no-open          # do not open a browser
.\start.ps1 --dry-run          # print the resolved commands without starting
.\start.ps1 --api-port 8012 --web-port 5174
```

If port 8000 is in use, do not assume it is NovaMarket: the parent NovaControl application also uses 8000. NovaMarket's default API port is 8011 and its Vite port is 5173.

## 10. Verify the installation

With both processes running:

```powershell
curl.exe -s http://127.0.0.1:8011/health
curl.exe -s http://127.0.0.1:8011/market/status
curl.exe -s http://127.0.0.1:8011/market/execution/status
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8011/docs
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:5173/
```

Expected characteristics:

- `/health` returns HTTP 200 with a health payload.
- `/market/status` returns a NovaMarket status payload and does not need to be empty.
- `/market/execution/status` reports `paper_only: true`, `live_trading: false`, `broker_connected: false`.
- `/docs` returns 200 for the Swagger UI.
- The Vite root returns 200.

If you set `NOVACONTROL_API_TOKEN`, add `-H "Authorization: Bearer <token>"` to protected requests. Do not paste a real token into screenshots or documentation.

The UI's own version check is the sidebar `UI v...` stamp; a stale embedded browser tab can retain an old DOM. Reload the page in a fresh tab if the interface appears inconsistent while the API is healthy.

## 11. First-run sequence

1. Open `http://127.0.0.1:5173/#/dashboard`.
2. If API authentication is enabled, enter the token in the in-app token dialog. It is stored in `sessionStorage` under `novamarket.apiToken`, not `localStorage`.
3. Confirm **System Health** shows the API and modules.
4. Go to **Settings** and **Sync public universe** if the instrument catalogue is empty.
5. Open **Market Overview**, find an instrument and inspect its quote timestamp and provider.
6. Create a **NovaCoin Wallet** in the Wallet screen. It opens at zero.
7. **Deposit** simulated NovaCoin. This is a ledger operation, not a bank transfer.
8. Create a **Short/Medium/Long-Term Portfolio** and fund it from the wallet.
9. Review **Risk Management** before creating a strategy proposal.
10. Create a strategy, generate a proposal, have it risk-evaluated, then submit an approved proposal from **Paper Trading**.
11. Verify the order, fill, position, ledger entries and reconciliation.
12. Use **Backtesting** with a separate run; it does not change the paper book.
13. Open **Shadow Mode** only for non-executing observation.
14. Use **Adaptive Learning** only after there is evidence to evaluate.

None of these steps creates a broker connection.

## 12. Manual startup

If you do not use `start.ps1`, use the same interpreter and source path:

```powershell
cd novamarket
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8011
```

In a second PowerShell window:

```powershell
cd novamarket\web
npm run dev -- --host 127.0.0.1 --port 5173
```

The Vite dev server proxies API paths to `http://127.0.0.1:8011` by default. If you change the API port, set `NOVAMARKET_API_ORIGIN` before starting Vite or set `VITE_API_BASE_URL` for a built frontend.

Production preview:

```powershell
cd web
npm run build
npm run preview -- --host 127.0.0.1 --port 4173
```

The preview command serves the built frontend; it does not start the API. Keep the API running separately.

## 13. Common setup problems

| Symptom | Likely cause | Safe fix |
|---|---|---|
| `No such file ... .venv\Scripts\python.exe` | The virtual environment is not one level above `novamarket`, or the path was moved | Create it at the expected parent or edit the local launcher; do not download a random venv |
| `ModuleNotFoundError: novacontrol` | `PYTHONPATH` or the parent installation is missing | Run through `start.ps1`, or set `$env:PYTHONPATH="src"` from `novamarket` |
| `EADDRINUSE` on 8011 or 5173 | Another process owns the port | Stop the process you started, or choose another port; never kill an unknown process by name |
| UI loads, cards fail | API not running, wrong proxy origin, token missing or CORS blocked | Check `/health`, Vite proxy target, token dialog and `NOVAMARKET_CORS_ORIGINS` |
| API requests return 401 | `NOVACONTROL_API_TOKEN` is set and the request lacks the bearer header | Enter the token in the UI or send `Authorization: Bearer ...` |
| API returns 403 for a mutation | Origin guard rejected the browser origin | Add the exact origin to `NOVAMARKET_CORS_ORIGINS`; do not disable the guard |
| Empty market catalogue | Fresh database, no universe sync or ingestion | Use **Sync public universe** or the import scripts |
| No options/futures | The official F&O snapshot was not imported | Import the NSE F&O bhavcopy; do not fabricate contracts |
| Old UI after an edit | Browser restored a stale DOM | Fully close and reopen the tab; the server sends no-cache headers |
| Build memory/time error | Node process constrained or dependencies incomplete | Reinstall from the lockfile and retry; do not switch frameworks |

For deeper diagnosis, use [Troubleshooting](TROUBLESHOOTING.md) and the generated [API Guide](API_GUIDE.md).
