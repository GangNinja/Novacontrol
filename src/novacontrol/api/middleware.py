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

class SecurityHeadersMiddleware:
    """Adds basic security headers to all responses (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"x-xss-protection", b"1; mode=block"),
                        (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    ]
                )
                message = {**message, "headers": headers_list}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


# Re-exported so `from novacontrol.api.middleware import ...` keeps working.
__all__ = ["RateLimitMiddleware", "RequestLoggingMiddleware", "SecurityHeadersMiddleware"]
