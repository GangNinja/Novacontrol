# NovaMarket Web

Standalone frontend for NovaMarket, built as a separate Vite application beside the
NovaMarket backend. It is not embedded in NovaControl's GUI or static assets.

## Run locally

```bash
cd novamarket/web
npm install
npm run dev
```

The development server runs at `http://127.0.0.1:5173` and proxies `/market` to
`http://127.0.0.1:8011` by default. Override the backend target with
`NOVAMARKET_API_ORIGIN` when starting Vite.

Start the backend from `novamarket/`:

```bash
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8011
```

The `.venv` in the parent repository can be used:

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8011
```

## Authentication

During local development the Vite proxy supplies a loopback-only admin session
(`Authorization` and `X-Autonomy-Operator-Token`), so a new tab opens the desk
without a token prompt. The backend accepts this credential only from loopback
clients, and only when the backend was started with `NOVACONTROL_ADMIN_TOKEN`.
The local `start.ps1` generates a cryptographically random value once and keeps
it in the gitignored `.runtime/admin-token`; there is no fallback literal. If
you run Vite directly, export the same token yourself.

A separately hosted frontend still asks for the backend bearer token at runtime
and keeps it in the current tab's `sessionStorage`. Configure
`NOVACONTROL_API_TOKEN` on the backend. Tokens are never bundled, committed, or
written to local storage.

### SSE migration

The dashboard's live channel now calls `POST /events/session` once, then opens
`GET /events/stream` with the resulting opaque, HttpOnly session cookie. The old
`GET /events/stream?token=…` form is refused: an `EventSource` client must
exchange a bearer credential (or use the loopback Vite proxy) and reconnect
through the session endpoint after the cookie expires. This keeps the API token
out of URLs, history and proxy access logs.

The session cookie is `SameSite=Strict`; the browser client sends it only
same-origin. If you serve the frontend from a different origin, proxy `/events`
through that origin (as the Vite dev server does) or the live channel will not
connect. Cross-origin browser SSE is intentionally not enabled; non-browser
clients can send an `Authorization` header to `/events/stream` directly.

## Build and deploy

```bash
npm run build
```

The static output is `dist/`. Serve it from any static host. For a separately
hosted origin, add the exact frontend origin to the backend environment:

```bash
NOVAMARKET_CORS_ORIGINS=https://market.example.com
```

Cross-origin access is opt-in and exact-match; no wildcard is used. If the
frontend is served from the same origin as the API (or via a reverse proxy),
leave `NOVAMARKET_CORS_ORIGINS` unset.

Set `VITE_API_BASE_URL` at build time only if the API lives on a different
origin from the static frontend, for example:

```bash
VITE_API_BASE_URL=https://api.example.com npm run build
```

## Scope

- All financial values come from the NovaMarket backend. The frontend formats
  and combines only values the backend has computed.
- Paper orders, backtests and shadow sessions are visually and semantically
  distinct from actual portfolio cash.
- 1 NC = ₹1.
- There is no real-money or broker execution route.
