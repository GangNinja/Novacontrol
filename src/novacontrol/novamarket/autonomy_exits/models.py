"""AT-05 stop-loss exit supervision models and contract constants."""

from __future__ import annotations

from enum import StrEnum

AT05_CONTRACT_VERSION = "1.1.0"
PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
SUPPORTED_EXIT_CONDITIONS = ("stop_loss", "target_price", "trailing_stop")
UNSUPPORTED_EXIT_CONDITIONS = ()
SUPPORTED_EXIT_DIRECTIONS = ("long",)


class ExitStatus(StrEnum):
    MONITORING = "monitoring"
    TRIGGERED = "triggered"
    RISK_REJECTED = "risk_rejected"
    RISK_APPROVED = "risk_approved"
    PREFLIGHT_FAILED = "preflight_failed"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


OPEN_DECISION_STATUSES = frozenset(
    {
        ExitStatus.MONITORING.value,
        ExitStatus.TRIGGERED.value,
        ExitStatus.RISK_APPROVED.value,
        ExitStatus.SUBMITTED.value,
        ExitStatus.PARTIALLY_FILLED.value,
        ExitStatus.UNKNOWN.value,
        ExitStatus.RECONCILIATION_REQUIRED.value,
    }
)

TERMINAL_DECISION_STATUSES = frozenset(
    {
        ExitStatus.RISK_REJECTED.value,
        ExitStatus.PREFLIGHT_FAILED.value,
        ExitStatus.FILLED.value,
        ExitStatus.REJECTED.value,
        ExitStatus.CANCELLED.value,
        ExitStatus.EXPIRED.value,
        ExitStatus.FAILED.value,
    }
)


class AutonomyExitError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message}


class ExitNotFound(AutonomyExitError):
    def __init__(self, message: str) -> None:
        super().__init__("EXIT_NOT_FOUND", message, http_status=404)


class InvalidExitConfiguration(AutonomyExitError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_EXIT_CONFIGURATION", message, http_status=422)
