# API

The Phase 12 API subsystem provides REST and WebSocket entrypoints through FastAPI.

## REST Routes

- `GET /health`: health check
- `GET /status`: API status and route metadata
- `POST /plan`: create and optionally execute a plan
- `POST /explore`: research a topic and return an Explore report
- `GET /plugins`: plugin API placeholder

## WebSocket Routes

- `WS /ws/events`: event channel with ping/pong and echo behavior

## Authentication

Set `NOVACONTROL_API_TOKEN` to require bearer-token authentication for protected routes.

```powershell
$env:NOVACONTROL_API_TOKEN='change-me'
python -m uvicorn novacontrol.api.app:create_app --factory --reload
```

Example protected request:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/plan `
  -Headers @{ Authorization = 'Bearer change-me' } `
  -ContentType 'application/json' `
  -Body '{"goal":"research options then implement one","execute":true}'
```

## CLI

```powershell
python -m novacontrol demo phase12
```
