"""Tool execution with validation, permissions, caching and normalization.

The order is the specification's pipeline, and every stage refuses to do the
next one's job:

    ToolRequest -> schema validation -> permission validation -> cache
                -> execution -> normalization -> (cache store) -> ToolResult

  * **Schema validation** runs first and is strict, so no argument a tool did not
    declare reaches it (see ``tools/validation.py`` for why).
  * **Permission validation** is unchanged and unreachable-around: this is still
    the only place a registered tool is called, and a tool that declares scopes
    goes through the approval gateway. Nothing in the caching or normalization
    work touched that path — a cached result is still only returned for a call
    that passed the same checks.
  * **The cache** answers only when the tool's own metadata says the operation is
    reusable, and only for successful results. A cache hit is marked
    ``cached=True`` on the result rather than disguised as fresh work.
  * **Normalization** bounds what leaves this function, so a ten-thousand-line
    test run arrives as an exit code, a summary and the lines that matter, with
    the amount dropped counted rather than forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass

from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.tools.cache import ToolResultCache
from novacontrol.tools.catalog import ToolCatalog
from novacontrol.tools.metadata import ToolMetadata
from novacontrol.tools.models import ToolRequest, ToolResult, ToolStatus
from novacontrol.tools.normalization import OutputNormalizer
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
        catalog: ToolCatalog | None = None,
        cache: ToolResultCache | None = None,
        normalizer: OutputNormalizer | None = None,
    ) -> None:
        self.registry = registry
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.policy = policy or ToolExecutionPolicy()
        #: What is known about each tool. Without it, nothing is cacheable and
        #: nothing is normalized beyond the output bounding below — the safe
        #: default, because metadata is what grants those permissions.
        self.catalog = catalog
        self.cache = cache
        self.normalizer = normalizer or OutputNormalizer()

    async def execute(self, request: ToolRequest) -> ToolResult:
        registered = self.registry.get(request.tool_name)
        metadata = self.catalog.get(request.tool_name) if self.catalog is not None else None

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
                    risk=metadata.risk if metadata is not None else RiskLevel.MEDIUM,
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

        cached = self._from_cache(request, metadata)
        if cached is not None:
            return cached

        try:
            output = await registered.tool.run(request.arguments)
        except Exception as exc:
            # Failures are never cached: a statement about a moment is not a
            # fact about the machine.
            return ToolResult(
                request_id=request.id,
                tool_name=request.tool_name,
                status=ToolStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        normalized = self.normalizer.normalize(request.tool_name, output)
        self._store(request, metadata, normalized)
        return ToolResult(
            request_id=request.id,
            tool_name=request.tool_name,
            status=ToolStatus.COMPLETED,
            output=normalized,
        )

    # -- cache ----------------------------------------------------------------

    def _from_cache(
        self, request: ToolRequest, metadata: ToolMetadata | None
    ) -> ToolResult | None:
        if self.cache is None or metadata is None:
            return None
        stored = self.cache.get(request.tool_name, request.arguments, metadata=metadata)
        if stored is None:
            return None
        return ToolResult(
            request_id=request.id,
            tool_name=request.tool_name,
            status=ToolStatus.COMPLETED,
            output=dict(stored),
            cached=True,
        )

    def _store(
        self,
        request: ToolRequest,
        metadata: ToolMetadata | None,
        output: dict[str, object],
    ) -> None:
        if self.cache is None or metadata is None:
            return
        self.cache.set(request.tool_name, request.arguments, output, metadata=metadata)

    def cache_report(self) -> dict[str, object]:
        """What the cache has done, for the tool-status readout."""
        return self.cache.to_dict() if self.cache is not None else {}


def _normalize_permissions(values: object) -> tuple[PermissionScope, ...]:
    permissions: list[PermissionScope] = []
    for value in values if isinstance(values, tuple | list) else ():
        if isinstance(value, PermissionScope):
            permissions.append(value)
        else:
            permissions.append(PermissionScope(str(value)))
    return tuple(permissions)
