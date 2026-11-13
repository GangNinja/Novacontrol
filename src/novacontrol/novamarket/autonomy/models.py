"""AT-01 autonomy vocabulary and owner-approved safety metadata.

This module contains no financial logic.  The runtime modes are deliberately
separate from the market-data lifecycle and from every execution engine: a
mode is a persisted control state, not an order instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AutonomyMode(StrEnum):
    """The four owner-approved operating modes."""

    DISABLED = "disabled"
    MONITORING = "monitoring"
    AUTONOMOUS = "autonomous"
    PAUSED = "paused"


class RuntimeState(StrEnum):
    """Runtime ownership/health, independent of the operating mode."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RECOVERING = "recovering"
    DEGRADED = "degraded"
    STOPPING = "stopping"


class CycleStatus(StrEnum):
    """The minimal cycle lifecycle used by the AT-01 lease table."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AutonomyError(RuntimeError):
    """Base class for autonomy refusals."""


class AutonomyValidationError(AutonomyError):
    """The request fields are invalid; nothing was changed."""


class AutonomyStateError(AutonomyError):
    """The requested transition is not legal from the current mode."""


class AutonomyLeaseError(AutonomyError):
    """Another live owner holds the runtime lease or an active cycle."""


class AutonomyStoreError(AutonomyError):
    """The durable autonomy store could not be read or written."""


#: The owner-approved portfolio horizon mapping.  The portfolio storage keys
#: remain short_term/medium_term/long_term for NM-07 compatibility; autonomy,
#: strategy configuration, API contracts and UI terminology use the trading
#: style names on the right.
HORIZON_MAPPING: dict[str, str] = {
    "short_term": "day_trading",
    "medium_term": "swing_trading",
    "long_term": "long_term",
}

#: Owner decision 4: only verified calendar years may be used by autonomy.
CALENDAR_STATUS: dict[str, object] = {
    "verified_years": [2026],
    "verified_through": 2026,
    "source": "marketdata/calendar.py",
    "fail_closed_outside_verified_years": True,
    "maintenance": (
        "Annual source review and verification; never extrapolate holidays "
        "from a prior year or assume weekdays are trading days."
    ),
}

#: The only legal transitions.  ``disabled`` has no self-edge here because
#: stop is handled idempotently by the runtime rather than by a mode change.
MODE_TRANSITIONS: dict[AutonomyMode, frozenset[AutonomyMode]] = {
    AutonomyMode.DISABLED: frozenset(
        {AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS}
    ),
    AutonomyMode.MONITORING: frozenset(
        {AutonomyMode.AUTONOMOUS, AutonomyMode.PAUSED, AutonomyMode.DISABLED}
    ),
    AutonomyMode.AUTONOMOUS: frozenset(
        {AutonomyMode.MONITORING, AutonomyMode.PAUSED, AutonomyMode.DISABLED}
    ),
    AutonomyMode.PAUSED: frozenset(
        {AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS, AutonomyMode.DISABLED}
    ),
}

#: Safety constants.  AT-01 has no order, fill, cash, position or ledger path;
#: these are also exposed by status/contracts so a UI cannot imply otherwise.
PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
TRADING_IMPLEMENTED = False
RELEASE_GATE = "closed"
ONE_CYCLE_AT_A_TIME = True
PAUSED_EXIT_POLICY = "protective"
EXIT_SUPERVISION_IMPLEMENTED = False
LEARNING_PROMOTION_IMPLEMENTED = False


def coerce_mode(value: object) -> AutonomyMode:
    """Return a known mode or refuse the value by name."""

    text = str(value or "").strip().lower()
    try:
        return AutonomyMode(text)
    except ValueError as exc:
        known = ", ".join(item.value for item in AutonomyMode)
        raise AutonomyValidationError(
            f"unknown autonomy mode {value!r}; known modes: {known}"
        ) from exc


@dataclass(frozen=True, slots=True)
class AutonomyConfig:
    """The small configuration AT-01 persists; no strategy or order fields."""

    config_version: int = 1
    portfolio_ids: tuple[str, ...] = ()
    paused_exit_policy: str = PAUSED_EXIT_POLICY
    lease_seconds: int = 60
    max_cycle_seconds: int = 30

    def __post_init__(self) -> None:
        if self.config_version < 1:
            raise AutonomyValidationError("config_version must be at least 1")
        if self.paused_exit_policy != PAUSED_EXIT_POLICY:
            raise AutonomyValidationError(
                "paused_exit_policy is owner-approved as 'protective'; "
                "monitor-only paused exits are not an AT-01 option"
            )
        if not 10 <= self.lease_seconds <= 3600:
            raise AutonomyValidationError("lease_seconds must be between 10 and 3600")
        if not 5 <= self.max_cycle_seconds <= 600:
            raise AutonomyValidationError(
                "max_cycle_seconds must be between 5 and 600"
            )
        if any(not item.strip() for item in self.portfolio_ids):
            raise AutonomyValidationError("portfolio_ids must not contain blanks")

    def to_dict(self) -> dict[str, object]:
        return {
            "config_version": self.config_version,
            "portfolio_ids": list(self.portfolio_ids),
            "paused_exit_policy": self.paused_exit_policy,
            "lease_seconds": self.lease_seconds,
            "max_cycle_seconds": self.max_cycle_seconds,
        }


class MonitoringCycleStatus(StrEnum):
    """AT-02 monitoring-cycle lifecycle; a terminal status is the only success."""

    CREATED = "created"
    CALENDAR_VALIDATION = "calendar_validation"
    SESSION_CHECK = "session_check"
    DISCOVERY = "discovery"
    ELIGIBILITY_FILTERING = "eligibility_filtering"
    BATCH_PREPARATION = "batch_preparation"
    MONITORING = "monitoring"
    COMPLETED = "completed"
    DEFERRED = "deferred"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: A cycle still in one of these statuses was interrupted by a dead owner.
ACTIVE_MONITORING_STATUSES: frozenset[str] = frozenset(
    {
        # A cycle is persisted as ``running`` while it walks the phases below;
        # recovery must interrupt it, so ``running`` is itself an active state.
        "running",
        MonitoringCycleStatus.CREATED.value,
        MonitoringCycleStatus.CALENDAR_VALIDATION.value,
        MonitoringCycleStatus.SESSION_CHECK.value,
        MonitoringCycleStatus.DISCOVERY.value,
        MonitoringCycleStatus.ELIGIBILITY_FILTERING.value,
        MonitoringCycleStatus.BATCH_PREPARATION.value,
        MonitoringCycleStatus.MONITORING.value,
    }
)


class MonitoringErrorCode(StrEnum):
    """Stable failure categories; no failed cycle is reported as success."""

    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    CALENDAR_UNVERIFIED = "CALENDAR_UNVERIFIED"
    OUTSIDE_TRADING_SESSION = "OUTSIDE_TRADING_SESSION"
    EXCHANGE_HOLIDAY = "EXCHANGE_HOLIDAY"
    RUNTIME_DISABLED = "RUNTIME_DISABLED"
    RUNTIME_PAUSED = "RUNTIME_PAUSED"
    LEASE_INVALID = "LEASE_INVALID"
    DISCOVERY_PROVIDER_FAILURE = "DISCOVERY_PROVIDER_FAILURE"
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    CYCLE_TIMEOUT = "CYCLE_TIMEOUT"
    CYCLE_CANCELLED = "CYCLE_CANCELLED"
    MONITORING_DISABLED = "MONITORING_DISABLED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    NO_CANDIDATES = "NO_CANDIDATES"


class MonitoringTrigger(StrEnum):
    """What asked for this monitoring cycle."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class MonitoringConfig:
    """AT-02 monitoring configuration; validated, bounded and order-free."""

    enabled: bool = True
    exchange: str = "XNSE"
    interval_seconds: int = 900
    max_instruments_per_cycle: int = 20
    retry_max: int = 2
    cycle_timeout_seconds: int = 120

    def __post_init__(self) -> None:
        exchange = str(self.exchange or "").strip().upper()
        object.__setattr__(self, "exchange", exchange)
        if not exchange:
            raise AutonomyValidationError("monitoring.exchange must not be blank")
        if not 900 <= self.interval_seconds <= 86_400:
            raise AutonomyValidationError(
                "monitoring.interval_seconds must be between 900 and 86400 "
                "(the market-data refresh floor is 15 minutes)"
            )
        if not 1 <= self.max_instruments_per_cycle <= 20:
            raise AutonomyValidationError(
                "monitoring.max_instruments_per_cycle must be between 1 and 20"
            )
        if not 0 <= self.retry_max <= 5:
            raise AutonomyValidationError("monitoring.retry_max must be between 0 and 5")
        if not 5 <= self.cycle_timeout_seconds <= 600:
            raise AutonomyValidationError(
                "monitoring.cycle_timeout_seconds must be between 5 and 600"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "exchange": self.exchange,
            "interval_seconds": self.interval_seconds,
            "max_instruments_per_cycle": self.max_instruments_per_cycle,
            "retry_max": self.retry_max,
            "cycle_timeout_seconds": self.cycle_timeout_seconds,
            "refresh_floor_seconds": 900,
            "batching_cap": 20,
        }


def transition_allowed(current: AutonomyMode, wanted: AutonomyMode) -> bool:
    """Whether a non-idempotent mode transition is legal."""

    return wanted in MODE_TRANSITIONS.get(current, frozenset())


__all__ = [
    "AutonomyConfig",
    "AutonomyError",
    "AutonomyLeaseError",
    "AutonomyMode",
    "AutonomyStateError",
    "AutonomyStoreError",
    "ACTIVE_MONITORING_STATUSES",
    "AutonomyValidationError",
    "BROKER_CONNECTED",
    "CALENDAR_STATUS",
    "MonitoringConfig",
    "MonitoringCycleStatus",
    "MonitoringErrorCode",
    "MonitoringTrigger",
    "CycleStatus",
    "EXIT_SUPERVISION_IMPLEMENTED",
    "HORIZON_MAPPING",
    "LEARNING_PROMOTION_IMPLEMENTED",
    "LIVE_TRADING",
    "MODE_TRANSITIONS",
    "ONE_CYCLE_AT_A_TIME",
    "PAPER_ONLY",
    "PAUSED_EXIT_POLICY",
    "RELEASE_GATE",
    "RuntimeState",
    "TRADING_IMPLEMENTED",
    "coerce_mode",
    "transition_allowed",
]
