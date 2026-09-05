"""API middleware for NovaControl."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp


# --- Rate Limiter ---

@dataclass
class RateLimitEntry:
    count: int = 0
    window_start: float = field(default_factory=time.time)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting middleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_requests: int = 60,
        window_seconds: float = 60.0,
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, RateLimitEntry] = defaultdict(RateLimitEntry)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        entry = self._requests[client_ip]

        if now - entry.window_start > self.window_seconds:
            entry.count = 0
            entry.window_start = now

        entry.count += 1
        if entry.count > self.max_requests:
            return JSONResponse(
                {"error": "Rate limit exceeded", "retry_after": self.window_seconds},
                status_code=429,
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(
            max(0, self.max_requests - entry.count)
        )
        return response


# --- Request Logger ---

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs request method, path, and response status code."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000
        # Status is accessible after the response is generated
        status_code = getattr(response, "status_code", 0)
        # Avoid logging static file requests to reduce noise
        if not request.url.path.startswith("/static"):
            import logging
            logger = logging.getLogger("novacontrol.api")
            logger.info(
                "%s %s %d %.1fms",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
            )
        return response


# --- Security Headers ---

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds basic security headers to all responses."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
