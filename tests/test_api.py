from __future__ import annotations

import asyncio
import unittest

from novacontrol.api import ApiSurface, ApiTokenAuthenticator
from novacontrol.api.middleware import RateLimitMiddleware


class ApiTests(unittest.TestCase):
    def test_authenticator_allows_local_when_no_token_configured(self) -> None:
        result = ApiTokenAuthenticator().authenticate(None)

        self.assertTrue(result.authenticated)
        self.assertEqual(result.principal, "local")

    def test_authenticator_requires_bearer_token_when_configured(self) -> None:
        authenticator = ApiTokenAuthenticator("secret")

        self.assertFalse(authenticator.authenticate(None).authenticated)
        self.assertFalse(authenticator.authenticate("Bearer wrong").authenticated)
        self.assertTrue(authenticator.authenticate("Bearer secret").authenticated)

    def test_api_surface_includes_rest_and_websocket_routes(self) -> None:
        surface = ApiSurface.default()
        paths = [route.path for route in surface.routes]

        self.assertIn("/", paths)
        self.assertIn("/health", paths)
        self.assertIn("/ask", paths)
        self.assertIn("/improve", paths)
        self.assertIn("/improve/workflow", paths)
        self.assertIn("/improve/preview", paths)
        self.assertIn("/improve/approve", paths)
        self.assertIn("/learn", paths)
        self.assertIn("/train", paths)
        self.assertIn("/explore", paths)
        self.assertIn("/events/stream", paths)
        self.assertIn("/command/plan", paths)
        self.assertIn("/command/execute", paths)
        self.assertIn("/desktop/plan", paths)
        self.assertIn("/desktop/execute", paths)
        self.assertIn("/phone/status", paths)
        self.assertIn("/phone/plan", paths)
        self.assertIn("/phone/execute", paths)
        self.assertIn("/ws/events", surface.websocket_paths)


async def _ok_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class RateLimitScopeTests(unittest.TestCase):
    """The budget is API capacity; the page shell and its assets are not."""

    def _status(self, middleware: RateLimitMiddleware, path: str) -> int:
        messages: list[dict] = []

        async def app(scope, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        middleware.app = app
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 4242),
        }
        asyncio.run(middleware(scope, receive, send))
        return int(messages[0]["status"])

    def test_the_page_and_static_assets_are_never_rate_limited(self) -> None:
        middleware = RateLimitMiddleware(_ok_app, max_requests=1, window_seconds=60.0)
        for _ in range(5):
            self.assertEqual(self._status(middleware, "/static/js/isometric.js"), 200)
            self.assertEqual(self._status(middleware, "/"), 200)
        self.assertEqual(self._status(middleware, "/status"), 200)
        self.assertEqual(self._status(middleware, "/status"), 429, "api routes must stay limited")

    def test_asset_requests_do_not_consume_the_api_budget(self) -> None:
        middleware = RateLimitMiddleware(_ok_app, max_requests=2, window_seconds=60.0)
        for _ in range(10):
            self.assertEqual(self._status(middleware, "/static/vendor/hairline/index.js"), 200)
        self.assertEqual(self._status(middleware, "/health"), 200)
        self.assertEqual(self._status(middleware, "/health"), 200)
        self.assertEqual(self._status(middleware, "/health"), 429)


if __name__ == "__main__":
    unittest.main()
