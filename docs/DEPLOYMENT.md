# Deployment Guide

NovaControl currently runs as a local Python application with CLI, API, GUI, and module demos.

## Local Deployment

```powershell
cd "C:\Users\NARASIMHA\OneDrive\文档\NovaControl"
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m unittest discover -s tests
python -m novacontrol demo all
```

## API Deployment

```powershell
$env:NOVACONTROL_API_TOKEN='change-me'
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Cache-header check (after starting the server, verify no HTML/CSS/JS response is storable so a stale cached UI can never survive a deploy):

```powershell
powershell -File scripts\check_no_cache.ps1 -BaseUrl http://127.0.0.1:8000
# or:  scripts\check_no_cache.cmd http://127.0.0.1:8000
```

Both scripts curl `/` and every `/static/*` asset with GET (the index route is GET-only — HEAD answers 405), assert each returns HTTP 200 with the `cache-control: no-cache, no-store, max-age=0, must-revalidate` trio, and exit non-zero on the first missing header. Run against the Docker container the same way, pointing `-BaseUrl` at its published port.

## GUI Deployment

```powershell
python -m novacontrol gui
```

## Configuration

Configuration can come from:

- Environment variables
- `configs/default.yaml`
- Future runtime settings persisted by the GUI/API

Useful environment variables:

- `NOVACONTROL_ENV`
- `NOVACONTROL_LOG_LEVEL`
- `NOVACONTROL_DATABASE_URL`
- `NOVACONTROL_REDIS_URL`
- `NOVACONTROL_REQUIRE_APPROVAL`
- `NOVACONTROL_API_TOKEN`

## Docker Roadmap

Docker and multi-service deployment should be added after real external services are connected. The current repository already declares the intended service boundaries for API, GUI, workers, Redis, and database-backed storage.
