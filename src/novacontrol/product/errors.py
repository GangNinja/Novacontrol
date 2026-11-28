"""Phase 36 error types, mapped to HTTP status codes by the API layer."""

from __future__ import annotations

from typing import Any


class ProductError(Exception):
    """Base class for product-pipeline refusals."""


class ProductNotFound(ProductError):
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        super().__init__(f"Product project not found: {product_id}")


class ProductConflict(ProductError):
    """The request conflicts with lifecycle state or stored records."""


class OpportunityNotEligible(ProductError):
    """A Phase 35 opportunity may not enter product development."""

    def __init__(
        self,
        message: str,
        *,
        state: str = "",
        opportunity_id: str = "",
        conditions: tuple[str, ...] = (),
    ) -> None:
        self.state = state
        self.opportunity_id = opportunity_id
        self.conditions = conditions
        super().__init__(message)


class ApprovalRequired(ProductError):
    """A human decision is required before the requested transition."""

    def __init__(self, message: str, *, approval_kind: str = "") -> None:
        self.approval_kind = approval_kind
        super().__init__(message)


class QualityGateError(ProductError):
    """One or more configured quality gates are not satisfied."""

    def __init__(self, message: str, *, failed_gates: tuple[str, ...] = ()) -> None:
        self.failed_gates = failed_gates
        super().__init__(message)


class WorkspaceError(ProductError):
    """A workspace path or file refused by the isolation policy."""


class BudgetExceeded(ProductError):
    """A bounded product-pipeline budget was reached."""


class TaskExecutionError(ProductError):
    """A task could not be executed safely."""

    def __init__(self, message: str, *, task_id: str = "") -> None:
        self.task_id = task_id
        super().__init__(message)


class ProductValidationError(ProductError, ValueError):
    """A payload is malformed."""


def error_payload(exc: Exception) -> dict[str, Any]:
    """Safe, serializable detail for an HTTP error body."""

    payload: dict[str, Any] = {"message": str(exc), "error": type(exc).__name__}
    for name in ("product_id", "opportunity_id", "state", "approval_kind", "task_id"):
        value = getattr(exc, name, "")
        if value:
            payload[name] = value
    conditions = getattr(exc, "conditions", ())
    if conditions:
        payload["conditions"] = list(conditions)
    failed = getattr(exc, "failed_gates", ())
    if failed:
        payload["failed_gates"] = list(failed)
    return payload


__all__ = [
    "ApprovalRequired",
    "BudgetExceeded",
    "OpportunityNotEligible",
    "ProductConflict",
    "ProductError",
    "ProductNotFound",
    "ProductValidationError",
    "QualityGateError",
    "TaskExecutionError",
    "WorkspaceError",
    "error_payload",
]
