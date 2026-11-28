"""Security-remediation contracts: credentials, errors, SSE and audit failures."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from novacontrol.api.auth import (
    ApiTokenAuthenticator,
    SecurityConfigurationError,
    SseSessionStore,
    load_security_policy,
)
from novacontrol.api.errors import install_error_handlers
from novacontrol.api.middleware import RequestContextMiddleware, SensitivePathAuthMiddleware
from novacontrol.application import NovaControlApplication
from novacontrol.audit.logger import AuditLogger, InMemoryAuditSink, JsonlAuditSink
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner


class SecurityConfigurationTests(unittest.TestCase):
    def test_authenticator_fails_closed_without_a_server_token(self) -> None:
        result = ApiTokenAuthenticator().authenticate(None)

        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, "Authentication is not configured.")

    def test_authenticator_handles_non_ascii_credentials_without_raising(self) -> None:
        authenticator = ApiTokenAuthenticator("pässwörd-\u4ee4\u724c")

        self.assertTrue(authenticator.authenticate("Bearer pässwörd-\u4ee4\u724c").authenticated)
        self.assertFalse(authenticator.authenticate("Bearer wrong").authenticated)

    def test_tokenless_development_requires_an_explicit_opt_in(self) -> None:
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(environ={"NOVACONTROL_ENV": "development"})

        policy = load_security_policy(
            environ={
                "NOVACONTROL_ENV": "development",
                "NOVACONTROL_ALLOW_ANONYMOUS_LOCAL": "1",
            }
        )
        self.assertTrue(policy.anonymous_local)
        self.assertFalse(policy.require_auth)

    def test_production_requires_a_long_api_token(self) -> None:
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(environ={"NOVACONTROL_ENV": "production"})
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ENV": "production",
                    "NOVACONTROL_API_TOKEN": "too-short",
                }
            )

        policy = load_security_policy(
            environ={
                "NOVACONTROL_ENV": "production",
                "NOVACONTROL_API_TOKEN": "a" * 40,
            }
        )
        self.assertTrue(policy.production_like)
        self.assertTrue(policy.require_auth)
        self.assertFalse(policy.anonymous_local)

    def test_predictable_admin_key_and_reused_operator_key_are_refused(self) -> None:
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ENV": "development",
                    "NOVACONTROL_ADMIN_TOKEN": "narasimha-novacontrol",
                }
            )
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ENV": "production",
                    "NOVACONTROL_API_TOKEN": "a" * 40,
                    "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "a" * 40,
                }
            )

    def test_a_non_loopback_host_or_cors_origin_requires_api_auth(self) -> None:
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ENV": "development",
                    "NOVACONTROL_ALLOW_ANONYMOUS_LOCAL": "1",
                },
                trusted_hosts=["api.example.com"],
            )
        with self.assertRaises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ENV": "development",
                    "NOVACONTROL_ALLOW_ANONYMOUS_LOCAL": "1",
                },
                cors_origins=["https://frontend.example.com"],
            )

    def test_application_creation_refuses_an_unsafe_production_config(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"NOVACONTROL_ENV": "production"}, clear=True),
            self.assertRaises(SecurityConfigurationError),
        ):
            from novacontrol.api.app import create_app

            create_app(data_dir=tmp)


class SseSessionStoreTests(unittest.TestCase):
    def test_sessions_expire_and_are_bounded(self) -> None:
        now = [100.0]
        store = SseSessionStore(ttl_seconds=30, max_sessions=2, clock=lambda: now[0])
        first = store.issue("alice")
        second = store.issue("bob")

        self.assertEqual(store.principal(first), "alice")
        self.assertEqual(store.count(), 2)
        now[0] += 31
        self.assertIsNone(store.principal(first))
        self.assertIsNone(store.principal(second))
        self.assertEqual(store.count(), 0)

        now[0] = 200.0
        one = store.issue("one")
        two = store.issue("two")
        three = store.issue("three")
        self.assertEqual(store.count(), 2)
        self.assertIsNone(store.principal(one))
        self.assertEqual(store.principal(two), "two")
        self.assertEqual(store.principal(three), "three")

    def test_revoke_is_immediate(self) -> None:
        store = SseSessionStore(clock=lambda: 1.0)
        token = store.issue("alice")
        self.assertTrue(store.revoke(token))
        self.assertIsNone(store.principal(token))
        self.assertFalse(store.revoke(token))


class SensitivePathMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/market/unguarded")
        async def unguarded() -> dict[str, bool]:
            # Deliberately no Depends(require_auth): the middleware is the net.
            return {"ok": True}

        app.add_middleware(
            SensitivePathAuthMiddleware,
            authenticator=ApiTokenAuthenticator("a" * 40),
            admin_authenticator=ApiTokenAuthenticator(None),
            anonymous_local=False,
        )
        app.add_middleware(RequestContextMiddleware)
        self.client = TestClient(app)

    def test_public_health_is_open(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_unprotected_sensitive_route_is_refused_without_a_credential(self) -> None:
        response = self.client.get("/market/unguarded")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("a" * 40, response.text)
        self.assertTrue(response.headers.get("x-request-id"))

    def test_valid_bearer_reaches_the_sensitive_route(self) -> None:
        response = self.client.get(
            "/market/unguarded", headers={"Authorization": f"Bearer {'a' * 40}"}
        )

        self.assertEqual(response.status_code, 200)

    def test_anonymous_local_never_bypasses_a_non_loopback_client(self) -> None:
        app = FastAPI()

        @app.get("/market/unguarded")
        async def unguarded() -> dict[str, bool]:
            return {"ok": True}

        app.add_middleware(
            SensitivePathAuthMiddleware,
            authenticator=ApiTokenAuthenticator(None),
            admin_authenticator=ApiTokenAuthenticator(None),
            anonymous_local=True,
        )
        app.add_middleware(RequestContextMiddleware)
        remote = TestClient(app, client=("203.0.113.9", 54321))

        self.assertEqual(remote.get("/market/unguarded").status_code, 401)
        local = TestClient(app, client=("127.0.0.1", 54321))
        self.assertEqual(local.get("/market/unguarded").status_code, 200)


class ApiErrorBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("password=hunter2 sqlite path=/private/finance.db")

        @app.get("/http500")
        async def http500() -> None:
            raise HTTPException(status_code=500, detail="token=super-secret-token")

        @app.get("/badgateway")
        async def badgateway() -> None:
            raise HTTPException(status_code=502, detail="provider body leaked")

        @app.get("/invalid")
        async def invalid() -> None:
            raise HTTPException(status_code=422, detail="field is required")

        install_error_handlers(app)
        app.add_middleware(RequestContextMiddleware)
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_unhandled_exception_is_generic_and_correlated(self) -> None:
        with self.assertLogs("novacontrol.api.errors", level="ERROR") as captured:
            response = self.client.get("/boom", headers={"X-Request-ID": "req-123"})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["request_id"], "req-123")
        self.assertEqual(response.headers["x-request-id"], "req-123")
        self.assertNotIn("hunter2", response.text)
        self.assertNotIn("/private/finance.db", response.text)
        self.assertIn("Internal server error", response.text)
        self.assertNotIn("hunter2", "\n".join(captured.output))

    def test_500_http_exception_detail_is_masked_but_status_is_kept(self) -> None:
        response = self.client.get("/http500")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("super-secret-token", response.text)
        self.assertEqual(response.json()["error"], "internal_server_error")

    def test_upstream_failure_keeps_502_but_hides_provider_body(self) -> None:
        response = self.client.get("/badgateway")

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("provider body leaked", response.text)
        self.assertEqual(response.json()["error"], "bad_gateway")

    def test_client_validation_detail_is_preserved(self) -> None:
        response = self.client.get("/invalid")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "field is required")


class _FailingAuditSink(InMemoryAuditSink):
    def append(self, record):  # type: ignore[no-untyped-def]
        raise OSError("password=hunter2 the audit file is locked")


class AuditFailureVisibilityTests(unittest.TestCase):
    def test_a_failed_write_is_counted_logged_and_redacted(self) -> None:
        logger = AuditLogger(sink=_FailingAuditSink())
        with self.assertLogs("novacontrol.audit", level="CRITICAL") as captured, self.assertRaises(
            OSError
        ):
            logger.record(task_id="t1", user_request="do the thing")

        report = logger.report()
        self.assertEqual(report["write_failures"], 1)
        self.assertTrue(report["degraded"])
        self.assertNotIn("hunter2", report["last_write_error"])
        self.assertNotIn("hunter2", "\n".join(captured.output))

    def test_atomic_replace_retries_a_transient_windows_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit" / "audit.jsonl"
            sink = JsonlAuditSink(path)
            sink.append({"id": "1", "task_id": "before"})

            real_replace = os.replace
            failed = [False]

            def flaky_replace(source: str, destination: str) -> None:
                if not failed[0]:
                    failed[0] = True
                    raise PermissionError("temporarily locked")
                real_replace(source, destination)

            with mock.patch(
                "novacontrol.audit.logger.os.replace",
                side_effect=flaky_replace,
            ):
                sink.replace([{"id": "2", "task_id": "after"}])

            self.assertEqual([row["task_id"] for row in sink.read()], ["after"])


class SseHttpTests(unittest.TestCase):
    """The real route: session exchange, cookie validation and query rejection."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_ENV": "development",
                "NOVACONTROL_API_TOKEN": "s" * 40,
                "NOVACONTROL_ALLOW_ANONYMOUS_LOCAL": "0",
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("NOVACONTROL_ADMIN_TOKEN", None)
        os.environ.pop("NOVACONTROL_AUTONOMY_OPERATOR_TOKEN", None)
        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=self._isolated
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            from novacontrol.api.app import create_app

            self.client = TestClient(
                create_app(data_dir=self._tmp.name), client=("127.0.0.1", 51200)
            )

    def _isolated(self, **kwargs: object) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        return NovaControlApplication(**kwargs)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.client.close()
        self._env.stop()
        self._tmp.cleanup()

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {'s' * 40}"}

    def test_session_requires_credentials(self) -> None:
        response = self.client.post("/events/session")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("s" * 40, response.text)

    def test_session_sets_an_httponly_scoped_cookie_without_returning_the_token(self) -> None:
        response = self.client.post("/events/session", headers=self._auth)

        self.assertEqual(response.status_code, 200)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Path=/events", cookie)
        self.assertNotIn("s" * 40, response.text)

    def test_stream_refuses_missing_invalid_and_query_string_credentials(self) -> None:
        self.assertEqual(self.client.get("/events/stream").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/events/stream", cookies={"novacontrol_sse": "not-a-session"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(f"/events/stream?token={'s' * 40}").status_code, 401
        )

if __name__ == "__main__":
    unittest.main()