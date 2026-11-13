"""API middleware for NovaControl.

All three middlewares are implemented as PURE ASGI middleware rather than
Starlette's ``BaseHTTPMiddleware``. BaseHTTPMiddleware wraps the response body
in an internal task queue, which BUFFERS streaming responses — the
/events/stream SSE channel would deliver every event (and even the 15-second
keep-alive comment frames) in multi-second lumps instead of the instant they
are published, breaking the live research/action progress shown in the web UI.
Pure ASGI middleware passes ``http.response.body`` messages straight through,
so streams stay incremental.

Headers are injected by intercepting the ``http.response.start`` message —
same behavior as the previous BaseHTTPMiddleware implementations, without the
buffering side effect.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send


# --- Rate Limiter ---

@dataclass
class RateLimitEntry:
    count: int = 0
    window_start: float = field(default_factory=time.time)


class RateLimitMiddleware:
    """Simple in-memory rate limiting middleware (pure ASGI, non-buffering)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_requests: int = 60,
        window_seconds: float = 60.0,
    ) -> None:
        self.app = app
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, RateLimitEntry] = defaultdict(RateLimitEntry)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client_ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or "unknown"
        client = scope.get("client")
        if client and client[0]:
            client_ip = client[0]  # direct connection: the socket peer is authoritative
        now = time.time()
        entry = self._requests[client_ip]

        if now - entry.window_start > self.window_seconds:
            entry.count = 0
            entry.window_start = now

        entry.count += 1

        async def send_with_rate_limit_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append((b"x-ratelimit-limit", str(self.max_requests).encode()))
                headers_list.append((b"x-ratelimit-remaining", str(max(0, self.max_requests - entry.count)).encode()))
                message = {**message, "headers": headers_list}
            await send(message)

        if entry.count > self.max_requests:
            # Same JSON shape the BaseHTTPMiddleware version returned.
            import json

            body = json.dumps(
                {"error": "Rate limit exceeded", "retry_after": self.window_seconds}
            ).encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-ratelimit-limit", str(self.max_requests).encode()),
                    (b"x-ratelimit-remaining", b"0"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send_with_rate_limit_headers)


# --- Request Logger ---

class RequestLoggingMiddleware:
    """Logs request method, path, and response status code (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.time()
        status_code = 0

        async def send_logging_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_logging_status)
        finally:
            path = scope.get("path", "")
            if not path.startswith("/static"):
                import logging

                logger = logging.getLogger("novacontrol.api")
                logger.info(
                    "%s %s %d %.1fms",
                    scope.get("method", ""),
                    path,
                    status_code,
                    (time.time() - start) * 1000,
                )


# --- Security Headers ---

# Frame-ancestors is the CSP replacement for X-Frame-Options; base-uri and
# object-src close two injection paths without restricting the app's own
# scripts/styles (a full script-src policy would break the existing inline UI).
_CONTENT_SECURITY_POLICY = (
    b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Adds basic security headers to all responses (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                names = {key.lower() for key, _ in headers_list}
                if b"x-content-type-options" not in names:
                    headers_list.append((b"x-content-type-options", b"nosniff"))
                if b"x-frame-options" not in names:
                    headers_list.append((b"x-frame-options", b"DENY"))
                if b"referrer-policy" not in names:
                    headers_list.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                if b"content-security-policy" not in names:
                    headers_list.append((b"content-security-policy", _CONTENT_SECURITY_POLICY))
                # Never let a browser or intermediary store API data.  The HTML
                # shell and static assets keep their own no-cache policy, which
                # the exact-header tests pin; only non-static API responses get
                # the extra no-store here.
                if path != "/" and not path.startswith("/static") and b"cache-control" not in names:
                    headers_list.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers_list}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


# --- Origin Guard ---

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _normalise_origin(value: str) -> str:
    """Return a bare scheme://host:port origin, or "" when the value is not one."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _is_loopback_origin(value: str) -> bool:
    """True for http(s) origins served by this machine, on any port.

    The Vite launcher can be moved to another port (--web-port), and a
    loopback page is not a remote CSRF source.  The Host allow-list remains
    the boundary that stops DNS rebinding from turning a hostile name into
    this machine.
    """
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


class OriginGuardMiddleware:
    """Refuse cross-site state-changing requests (a localhost CSRF guard).

    A loopback-only API is still reachable by any web page the user visits: a
    form POST or a ``fetch`` with a simple content type does not need CORS to
    reach it.  Browsers send ``Origin`` on those requests, so an unsafe method
    whose Origin is neither this API's own origin nor an explicitly allowed
    development/CORS origin is refused before it can change state.  GET/HEAD
    stay open for the UI and the SSE stream; reading those cross-origin is
    already barred by the browser's CORS response checks.
    """

    def __init__(self, app: ASGIApp, *, allowed_origins: tuple[str, ...] | list[str] = ()) -> None:
        self.app = app
        self.allowed_origins = frozenset(
            origin
            for origin in (_normalise_origin(item) for item in allowed_origins)
            if origin
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        raw_origin = headers.get("origin")
        if raw_origin is None:
            # Non-browser clients (curl, tests, the packaged desktop app) do not
            # send Origin.  The loopback/token boundary still applies to them.
            await self.app(scope, receive, send)
            return

        origin = _normalise_origin(raw_origin)
        host = headers.get("host", "").strip()
        request_origin = _normalise_origin(f"{scope.get('scheme', 'http')}://{host}")
        if origin and (
            origin == request_origin
            or origin in self.allowed_origins
            or _is_loopback_origin(origin)
        ):
            await self.app(scope, receive, send)
            return

        import json

        body = json.dumps(
            {
                "detail": "Cross-origin request refused. Use the NovaMarket UI "
                "or configure NOVAMARKET_ALLOWED_ORIGINS for a separate frontend."
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", _CONTENT_SECURITY_POLICY),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# Re-exported so `from novacontrol.api.middleware import ...` keeps working.
__all__ = [
    "OriginGuardMiddleware",
    "RateLimitMiddleware",
    "RequestLoggingMiddleware",
    "SecurityHeadersMiddleware",
]
