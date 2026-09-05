"""API authentication helpers."""

from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    authenticated: bool
    principal: str = "anonymous"
    reason: str | None = None


class ApiTokenAuthenticator:
    """Bearer-token authenticator used by REST and WebSocket entrypoints."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def authenticate(self, authorization: str | None) -> AuthenticationResult:
        if not self.enabled:
            return AuthenticationResult(True, principal="local")
        if not authorization or not authorization.startswith("Bearer "):
            return AuthenticationResult(False, reason="Missing bearer token.")
        candidate = authorization.removeprefix("Bearer ").strip()
        if compare_digest(candidate, self.token or ""):
            return AuthenticationResult(True, principal="api-token")
        return AuthenticationResult(False, reason="Invalid bearer token.")
