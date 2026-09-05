"""Security and approval primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


class PermissionScope(StrEnum):
    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_WRITE = "filesystem:write"
    SHELL_EXECUTE = "shell:execute"
    NETWORK_ACCESS = "network:access"
    BROWSER_CONTROL = "browser:control"
    DESKTOP_CONTROL = "desktop:control"
    PHONE_CONTROL = "phone:control"
    SECRET_READ = "secret:read"
    MICROPHONE_ACCESS = "microphone:access"
    CAMERA_ACCESS = "camera:access"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    action: str
    reason: str
    permissions: tuple[PermissionScope, ...]
    risk: RiskLevel = RiskLevel.MEDIUM
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    request_id: str
    approved: bool
    decided_by: str
    reason: str | None = None
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class ApprovalGateway(Protocol):
    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Ask a human or policy engine to approve a sensitive action."""


class DenyByDefaultApprovalGateway:
    """Safe approval gateway used until a real UI/API approval flow exists."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            request_id=request.id,
            approved=False,
            decided_by="policy.deny_by_default",
            reason="No approval provider is configured.",
        )
