"""Typed errors for Phase 38 business operations and finance."""

from __future__ import annotations

from typing import Any


class FinOpsError(Exception):
    """Base error for the finance domain."""


class FinOpsNotFound(FinOpsError):
    pass


class VentureNotFound(FinOpsNotFound):
    def __init__(self, venture_id: str) -> None:
        super().__init__(f"Venture not found: {venture_id}")
        self.venture_id = venture_id


class FinOpsRecordNotFound(FinOpsNotFound):
    def __init__(self, kind: str, record_id: str) -> None:
        super().__init__(f"{kind} not found: {record_id}")
        self.kind = kind
        self.record_id = record_id


class FinOpsValidationError(FinOpsError):
    pass


class FinOpsConflict(FinOpsError):
    pass


class FinOpsAuthorizationError(FinOpsError):
    pass


class FinOpsBudgetExceeded(FinOpsError):
    pass


class PeriodClosed(FinOpsConflict):
    pass


class IdempotencyConflict(FinOpsConflict):
    pass


class InsufficientFinancialData(FinOpsConflict):
    pass


def error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "error": exc.__class__.__name__,
        "message": str(exc),
    }


__all__ = [
    "FinOpsAuthorizationError",
    "FinOpsBudgetExceeded",
    "FinOpsConflict",
    "FinOpsError",
    "FinOpsNotFound",
    "FinOpsRecordNotFound",
    "FinOpsValidationError",
    "IdempotencyConflict",
    "InsufficientFinancialData",
    "PeriodClosed",
    "VentureNotFound",
    "error_payload",
]
