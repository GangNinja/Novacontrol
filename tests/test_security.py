from __future__ import annotations

import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from novacontrol.api.middleware import OriginGuardMiddleware, SecurityHeadersMiddleware
from novacontrol.core.security import (
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_gateway_denies_sensitive_actions(self) -> None:
        gateway = DenyByDefaultApprovalGateway()
        request = ApprovalRequest(
            action="delete-file",
            reason="User requested cleanup",
            permissions=(PermissionScope.FILESYSTEM_WRITE,),
        )

        decision = await gateway.request_approval(request)

        self.assertEqual(decision.request_id, request.id)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.decided_by, "policy.deny_by_default")


class OriginGuardTests(unittest.TestCase):
    """The localhost API must not accept a state change from a foreign page."""

    def setUp(self) -> None:
        app = FastAPI()

        @app.get("/read")
        async def read() -> dict[str, bool]:
            return {"ok": True}

        @app.post("/write")
        async def write() -> dict[str, bool]:
            return {"ok": True}

        app.add_middleware(
            OriginGuardMiddleware,
            allowed_origins=("http://frontend.example", "http://127.0.0.1:5173"),
        )
        self.client = TestClient(app)

    def test_same_origin_write_is_allowed(self) -> None:
        response = self.client.post("/write", headers={"origin": "http://testserver"})

        self.assertEqual(response.status_code, 200)

    def test_loopback_development_origin_is_allowed(self) -> None:
        response = self.client.post(
            "/write", headers={"origin": "http://127.0.0.1:5173"}
        )

        self.assertEqual(response.status_code, 200)

    def test_configured_frontend_origin_is_allowed(self) -> None:
        response = self.client.post(
            "/write", headers={"origin": "http://frontend.example"}
        )

        self.assertEqual(response.status_code, 200)

    def test_foreign_origin_write_is_refused(self) -> None:
        response = self.client.post(
            "/write", headers={"origin": "https://evil.example"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("Cross-origin request refused", response.json()["detail"])

    def test_null_origin_write_is_refused(self) -> None:
        response = self.client.post("/write", headers={"origin": "null"})

        self.assertEqual(response.status_code, 403)

    def test_originless_client_is_still_allowed(self) -> None:
        response = self.client.post("/write")

        self.assertEqual(response.status_code, 200)

    def test_cross_origin_reads_are_not_blocked_by_the_csrf_guard(self) -> None:
        response = self.client.get("/read", headers={"origin": "https://evil.example"})

        self.assertEqual(response.status_code, 200)


class LocalOnlyShellTests(unittest.TestCase):
    """Opening the local UI must not contact a third-party font host."""

    def test_the_shell_requests_no_external_fonts(self) -> None:
        shell = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("fonts.googleapis.com", shell)
        self.assertNotIn("fonts.gstatic.com", shell)


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()

        @app.get("/api/value")
        async def value() -> dict[str, bool]:
            return {"ok": True}

        app.add_middleware(SecurityHeadersMiddleware)
        self.client = TestClient(app)

    def test_api_responses_are_not_stored(self) -> None:
        response = self.client.get("/api/value")

        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_frame_and_injection_headers_are_present(self) -> None:
        headers = self.client.get("/api/value").headers

        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", headers.get("content-security-policy", ""))
        self.assertIn("object-src 'none'", headers.get("content-security-policy", ""))


class TrustedHostBoundaryTests(unittest.TestCase):
    """A DNS name rebound to loopback still presents a foreign Host header."""

    def setUp(self) -> None:
        app = FastAPI()

        @app.get("/")
        async def root() -> dict[str, bool]:
            return {"ok": True}

        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "testserver"]
        )
        self.client = TestClient(app)

    def test_loopback_host_is_allowed(self) -> None:
        response = self.client.get("/", headers={"host": "127.0.0.1:8011"})

        self.assertEqual(response.status_code, 200)

    def test_foreign_host_is_refused(self) -> None:
        response = self.client.get("/", headers={"host": "rebound.evil.example"})

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
