"""API authentication, security configuration and SSE session helpers."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hmac import compare_digest

#: Cookie carrying the opaque, server-side SSE session.  It is deliberately
#: separate from the API bearer token: an EventSource request can send a cookie
#: but cannot set an Authorization header.
SSE_SESSION_COOKIE = "novacontrol_sse"

_DEVELOPMENT_ENVIRONMENTS = frozenset({"dev", "development", "local", "test", "testing"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
#: The retired launcher literal is still refused by default.  A local owner may
#: re-enable it explicitly for loopback convenience with
#: ``NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN=1``; that opt-in is ignored in any
#: production-like environment and never applies to a non-loopback client.
LEGACY_ADMIN_TOKEN = "narasimha-novacontrol"

_KNOWN_WEAK_SECRETS = frozenset(
    {
        "admin",
        "changeme",
        "change-me",
        "default",
        LEGACY_ADMIN_TOKEN,
        "password",
        "secret",
        "test",
    }
)


class SecurityConfigurationError(RuntimeError):
    """The process refused to start because its security configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    authenticated: bool
    principal: str = "anonymous"
    reason: str | None = None


class ApiTokenAuthenticator:
    """Bearer-token authenticator used by REST and WebSocket entrypoints.

    A missing server token always fails closed.  Local anonymous development is
    an explicit application policy (``NOVACONTROL_ALLOW_ANONYMOUS_LOCAL``), not
    a property of this primitive, so a future caller cannot accidentally treat
    "auth is switched off" as "this request is authenticated".
    """

    def __init__(self, token: str | None = None) -> None:
        self.token = token

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def authenticate(self, authorization: str | None) -> AuthenticationResult:
        if not self.enabled:
            return AuthenticationResult(False, reason="Authentication is not configured.")
        if not authorization or not authorization.startswith("Bearer "):
            return AuthenticationResult(False, reason="Missing bearer token.")
        candidate = authorization.removeprefix("Bearer ").strip()
        if compare_digest(candidate.encode("utf-8"), (self.token or "").encode("utf-8")):
            return AuthenticationResult(True, principal="api-token")
        return AuthenticationResult(False, reason="Invalid bearer token.")


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Validated authentication policy for one application process."""

    environment: str
    api_token: str | None
    operator_token: str | None
    admin_token: str | None
    require_auth: bool
    anonymous_local: bool
    production_like: bool
    #: The retired launcher literal, accepted as an EXTRA loopback-only admin
    #: credential when the owner opted in; ``None`` otherwise.
    legacy_admin_token: str | None = None


def _flag(value: str | None, *, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _is_loopback_name(value: str) -> bool:
    host = str(value or "").strip().lower().strip("[]")
    if host.startswith("::ffff:"):
        host = host.removeprefix("::ffff:")
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def load_security_policy(
    *,
    environ: Mapping[str, str] | None = None,
    trusted_hosts: Sequence[str] = (),
    cors_origins: Sequence[str] = (),
) -> SecurityPolicy:
    """Load and validate auth settings before the application is constructed.

    Rules:

    * production-like deployments (anything except dev/local/test) require an
      API token of at least 32 characters;
    * a non-loopback trusted host or a configured CORS origin is treated as an
      exposed deployment even when ``NOVACONTROL_ENV`` was forgotten;
    * a tokenless loopback development session is allowed only with the explicit
      ``NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1`` opt-in;
    * the old launcher default and other known weak admin secrets are refused;
      the API and autonomy-operator credentials must be distinct.

    The error messages name the missing/invalid setting, never its value.
    """

    source = os.environ if environ is None else environ
    environment = str(source.get("NOVACONTROL_ENV", "development") or "development").strip().lower()
    production_like = environment not in _DEVELOPMENT_ENVIRONMENTS
    exposed = bool(cors_origins) or any(not _is_loopback_name(host) for host in trusted_hosts)
    require_auth = _flag(source.get("NOVACONTROL_REQUIRE_AUTH")) or production_like or exposed

    api_token = str(source.get("NOVACONTROL_API_TOKEN", "") or "").strip() or None
    operator_token = (
        str(source.get("NOVACONTROL_AUTONOMY_OPERATOR_TOKEN", "") or "").strip() or None
    )
    admin_token = str(source.get("NOVACONTROL_ADMIN_TOKEN", "") or "").strip() or None

    # Explicit, development-only owner opt-in for the retired launcher literal.
    # It is ignored for any production-like environment, and the credential it
    # enables is only ever consulted for loopback requests (see app.py), so a
    # remote client can never use it.
    allow_legacy_admin = _flag(
        source.get("NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN")
    ) and not production_like
    legacy_admin_token = LEGACY_ADMIN_TOKEN if allow_legacy_admin else None

    if (
        admin_token
        and admin_token.lower() in _KNOWN_WEAK_SECRETS
        and not (allow_legacy_admin and admin_token.lower() == LEGACY_ADMIN_TOKEN)
    ):
        raise SecurityConfigurationError(
            "NOVACONTROL_ADMIN_TOKEN is a known predictable value. Generate a "
            "random per-installation secret (at least 32 characters)."
        )
    if production_like or exposed:
        if not api_token:
            raise SecurityConfigurationError(
                "NOVACONTROL_API_TOKEN is required for a production-like or "
                "non-loopback deployment. Refusing to start with anonymous access."
            )
        if len(api_token) < 32:
            raise SecurityConfigurationError(
                "NOVACONTROL_API_TOKEN must be at least 32 characters in a "
                "production-like or non-loopback deployment."
            )
        if admin_token and len(admin_token) < 32:
            raise SecurityConfigurationError(
                "NOVACONTROL_ADMIN_TOKEN must be at least 32 characters in a "
                "production-like or non-loopback deployment."
            )
    elif require_auth:
        if not api_token and not admin_token:
            raise SecurityConfigurationError(
                "NOVACONTROL_REQUIRE_AUTH is enabled but neither "
                "NOVACONTROL_API_TOKEN nor NOVACONTROL_ADMIN_TOKEN is configured."
            )
        if api_token and len(api_token) < 16:
            raise SecurityConfigurationError(
                "NOVACONTROL_API_TOKEN must be at least 16 characters when "
                "authentication is explicitly required."
            )
        if admin_token and len(admin_token) < 16:
            raise SecurityConfigurationError(
                "NOVACONTROL_ADMIN_TOKEN must be at least 16 characters when "
                "authentication is explicitly required."
            )

    if (
        api_token
        and operator_token
        and compare_digest(api_token.encode("utf-8"), operator_token.encode("utf-8"))
    ):
        raise SecurityConfigurationError(
            "NOVACONTROL_API_TOKEN and NOVACONTROL_AUTONOMY_OPERATOR_TOKEN must "
            "be different credentials."
        )

    anonymous_local = bool(
        not production_like
        and not exposed
        and not require_auth
        and not api_token
        and not admin_token
        and _flag(source.get("NOVACONTROL_ALLOW_ANONYMOUS_LOCAL"))
    )
    if not api_token and not admin_token and not anonymous_local:
        raise SecurityConfigurationError(
            "No API credential is configured. Set NOVACONTROL_API_TOKEN, or set "
            "NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1 for an explicitly tokenless "
            "loopback development session."
        )

    return SecurityPolicy(
        environment=environment,
        api_token=api_token,
        operator_token=operator_token,
        admin_token=admin_token,
        require_auth=require_auth,
        anonymous_local=anonymous_local,
        production_like=production_like,
        legacy_admin_token=legacy_admin_token,
    )


class SseSessionStore:
    """Short-lived opaque SSE sessions, held server-side and hashed at rest.

    Cookies are not the API token and are not accepted by REST routes.  The
    store keeps only a SHA-256 digest of each random cookie value, expires idle
    sessions, and caps the table so an attacker cannot grow it without bound.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_sessions: int = 512,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self._clock = clock or time.monotonic
        self._sessions: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        expired = [key for key, (_, expires) in self._sessions.items() if expires <= now]
        for key in expired:
            self._sessions.pop(key, None)

    def _make_room(self, now: float) -> None:
        self._prune(now)
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=lambda key: self._sessions[key][1])
            self._sessions.pop(oldest, None)

    def issue(self, principal: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        self._make_room(now)
        self._sessions[self._digest(token)] = (str(principal), now + self.ttl_seconds)
        return token

    def principal(self, token: str | None) -> str | None:
        if not token:
            return None
        key = self._digest(token)
        row = self._sessions.get(key)
        if row is None:
            return None
        principal, expires = row
        if expires <= self._clock():
            self._sessions.pop(key, None)
            return None
        return principal

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        return self._sessions.pop(self._digest(token), None) is not None

    def count(self) -> int:
        self._prune(self._clock())
        return len(self._sessions)
