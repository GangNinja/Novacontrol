"""Tool execution with permission and approval enforcement."""

from __future__ import annotations

from dataclasses import dataclass

from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.tools.models import ToolRequest, ToolResult, ToolStatus
from novacontrol.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    require_approval_for_sensitive_tools: bool = True


class ToolExecutor:
    """Executes registered tools after validating schemas and permissions."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        approval_gateway: ApprovalGateway | None = None,
        policy: ToolExecutionPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.policy = policy or ToolExecutionPolicy()

    async def execute(self, request: ToolRequest) -> ToolResult:
        registered = self.registry.get(request.tool_name)
        errors = registered.schema.validate(request.arguments)
        if errors:
            return ToolResult(
                request_id=request.id,
                tool_name=request.tool_name,
                status=ToolStatus.FAILED,
                error="; ".join(errors),
            )

        permissions = _normalize_permissions(registered.tool.required_permissions)
        if permissions and self.policy.require_approval_for_sensitive_tools:
            approval = await self.approval_gateway.request_approval(
                ApprovalRequest(
                    action=f"Run tool {request.tool_name}",
                    reason=request.reason or "Tool execution requested.",
                    permissions=permissions,
                    risk=RiskLevel.MEDIUM,
                    metadata={"tool": request.tool_name, "request_id": request.id},
                )
            )
            if not approval.approved:
                return ToolResult(
                    request_id=request.id,
                    tool_name=request.tool_name,
                    status=ToolStatus.DENIED,
                    error=approval.reason or "Tool execution was not approved.",
                    approval_id=approval.request_id,
                )

        try:
            output = await registered.tool.run(request.arguments)
            return ToolResult(
                request_id=request.id,
                tool_name=request.tool_name,
                status=ToolStatus.COMPLETED,
                output=dict(output),
            )
        except Exception as exc:
            return ToolResult(
                request_id=request.id,
                tool_name=request.tool_name,
                status=ToolStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )


def _normalize_permissions(values: object) -> tuple[PermissionScope, ...]:
    permissions: list[PermissionScope] = []
    for value in values if isinstance(values, tuple | list) else ():
        if isinstance(value, PermissionScope):
            permissions.append(value)
        else:
            permissions.append(PermissionScope(str(value)))
    return tuple(permissions)
