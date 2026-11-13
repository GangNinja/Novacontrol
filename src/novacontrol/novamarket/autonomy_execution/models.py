"""AT-04 autonomous paper-execution states, errors and safety constants.

The package composes the existing AT-01 runtime, AT-03 decisions and NM-10
paper engine.  It owns no ledger, portfolio, risk or order implementation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

AT04_CONTRACT_VERSION = "1.0.0"
PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
EXECUTION_AUTHORITY = "NM-10"
RISK_AUTHORITY = "NM-08"
MAX_EXECUTIONS_PER_CYCLE = 20
MAX_SUBMISSION_RETRIES = 3
SUBMISSION_TIMEOUT_SECONDS = 30.0


class ExecutionIntentStatus(StrEnum):
    INTENT = "intent"
    PREFLIGHT_FAILED = "preflight_failed"
    FORWARDING = "forwarding"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_INTENT_STATUSES = frozenset(
    {
        ExecutionIntentStatus.PREFLIGHT_FAILED.value,
        ExecutionIntentStatus.FILLED.value,
        ExecutionIntentStatus.REJECTED.value,
        ExecutionIntentStatus.CANCELLED.value,
        ExecutionIntentStatus.EXPIRED.value,
        ExecutionIntentStatus.FAILED.value,
    }
)

RETRYABLE_INTENT_STATUSES = frozenset(
    {
        ExecutionIntentStatus.INTENT.value,
        ExecutionIntentStatus.FORWARDING.value,
        ExecutionIntentStatus.UNKNOWN.value,
    }
)

ACTIVE_INTENT_STATUSES = frozenset(
    {
        ExecutionIntentStatus.INTENT.value,
        ExecutionIntentStatus.FORWARDING.value,
        ExecutionIntentStatus.SUBMITTED.value,
        ExecutionIntentStatus.PARTIALLY_FILLED.value,
        ExecutionIntentStatus.UNKNOWN.value,
        ExecutionIntentStatus.RECONCILIATION_REQUIRED.value,
    }
)


class AutonomyExecutionError(RuntimeError):
    """AT-04 refused an execution for a stated, auditable reason."""

    def __init__(self, code: str, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.http_status = int(http_status)

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


class ExecutionNotFound(AutonomyExecutionError):
    """No journal record exists for the requested execution."""

    def __init__(self, message: str) -> None:
        super().__init__("EXECUTION_NOT_FOUND", message, http_status=404)


class ExecutionConflict(AutonomyExecutionError):
    """An execution attempt conflicts with durable state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, http_status=409)


def status_for_order(order: Mapping[str, Any]) -> str:
    """Map NM-10's authoritative order status into the AT-04 journal state."""

    raw = str(order.get("status") or "").strip().lower()
    if raw == "filled":
        return ExecutionIntentStatus.FILLED.value
    if raw == "partially_filled":
        return ExecutionIntentStatus.PARTIALLY_FILLED.value
    if raw == "rejected":
        return ExecutionIntentStatus.REJECTED.value
    if raw == "cancelled":
        return ExecutionIntentStatus.CANCELLED.value
    if raw == "expired":
        return ExecutionIntentStatus.EXPIRED.value
    if raw == "failed":
        return ExecutionIntentStatus.FAILED.value
    if raw in {"created", "pending_validation", "accepted", "open", "cancel_pending"}:
        return ExecutionIntentStatus.SUBMITTED.value
    return ExecutionIntentStatus.UNKNOWN.value


def is_terminal(status: str) -> bool:
    return str(status) in TERMINAL_INTENT_STATUSES


def is_retryable(status: str) -> bool:
    return str(status) in RETRYABLE_INTENT_STATUSES


__all__ = [
    "ACTIVE_INTENT_STATUSES",
    "AT04_CONTRACT_VERSION",
    "AutonomyExecutionError",
    "BROKER_CONNECTED",
    "EXECUTION_AUTHORITY",
    "ExecutionConflict",
    "ExecutionIntentStatus",
    "ExecutionNotFound",
    "LIVE_TRADING",
    "MAX_EXECUTIONS_PER_CYCLE",
    "MAX_SUBMISSION_RETRIES",
    "PAPER_ONLY",
    "RETRYABLE_INTENT_STATUSES",
    "RISK_AUTHORITY",
    "SUBMISSION_TIMEOUT_SECONDS",
    "TERMINAL_INTENT_STATUSES",
    "is_retryable",
    "is_terminal",
    "status_for_order",
]
