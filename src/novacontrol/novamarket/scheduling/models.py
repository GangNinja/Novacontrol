"""PI-09 forecast-cycle, checkpoint and outcome contracts.

The scheduler is a lifecycle coordinator over PI-01 through PI-08.  These
contracts deliberately contain no order, position, fill or ledger field: the
forecast scheduler has no financial authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping

from novacontrol.novamarket.prediction.models import (
    ConfirmationStage,
    ForecastHorizon,
    ForecastOutput,
    PredictionValidationError,
)

PI09_PHASE = "PI-09"
SCHEDULE_CONTRACT_VERSION = "prediction.schedule.1.0"
SCHEDULE_POLICY_VERSION = "pi10-horizon-policy-2.0"
OUTCOME_CONTRACT_VERSION = "prediction.outcome.1.0"


class CycleState(StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    EVALUATED = "evaluated"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: Cycle states that still have work outstanding: the initial/final checkpoints
#: or the outcome evaluation have not all run yet.  The operator-facing cycle
#: list groups these ahead of purely historical cycles (evaluated, cancelled,
#: failed) so an older cycle that is still due is never buried under today's
#: finished history.
OUTSTANDING_CYCLE_STATES: tuple[CycleState, ...] = (
    CycleState.SCHEDULED,
    CycleState.ACTIVE,
    CycleState.CONFIRMED,
    CycleState.UNCONFIRMED,
)


class CheckpointKind(StrEnum):
    INITIAL = "initial"
    FIRST = "first_confirmation"
    FINAL = "final_confirmation"
    OUTCOME = "outcome_evaluation"

    @property
    def stage(self) -> ConfirmationStage | None:
        return {
            CheckpointKind.INITIAL: ConfirmationStage.INITIAL,
            CheckpointKind.FIRST: ConfirmationStage.FIRST,
            CheckpointKind.FINAL: ConfirmationStage.FINAL,
            CheckpointKind.OUTCOME: None,
        }[self]

    @property
    def label(self) -> str:
        return {
            CheckpointKind.INITIAL: "T-20 initial forecast",
            CheckpointKind.FIRST: "T-5 confirmation",
            CheckpointKind.FINAL: "T-1 final confirmation",
            CheckpointKind.OUTCOME: "T outcome evaluation",
        }[self]


class CheckpointState(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    ABSTAINED = "abstained"
    FAILED = "failed"
    PENDING = "evaluation_pending"
    EVALUATED = "evaluated"
    DEAD_LETTER = "dead_letter"


class RevisionClass(StrEnum):
    STABLE = "stable"
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    DIRECTION_CHANGED = "direction_changed"
    UNCERTAIN = "uncertain"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class HorizonSchedulePolicy:
    """The explicit target and checkpoint rule for one horizon."""

    horizon: ForecastHorizon
    target_rule: str
    initial_rule: str
    initial_lead_seconds: int
    first_lead_seconds: int
    final_lead_seconds: int
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "target_rule": self.target_rule,
            "initial_rule": self.initial_rule,
            "initial_lead_seconds": self.initial_lead_seconds,
            "first_lead_seconds": self.first_lead_seconds,
            "final_lead_seconds": self.final_lead_seconds,
            "description": self.description,
        }


_INTRADAY = (
    ForecastHorizon.MINUTES_1,
    ForecastHorizon.MINUTES_5,
    ForecastHorizon.MINUTES_15,
    ForecastHorizon.MINUTES_20,
    ForecastHorizon.HOUR_1,
)

HORIZON_SCHEDULE_POLICIES: dict[ForecastHorizon, HorizonSchedulePolicy] = {
    ForecastHorizon.MINUTES_1: HorizonSchedulePolicy(
        ForecastHorizon.MINUTES_1,
        target_rule="intraday_duration",
        initial_rule="fixed_lead",
        initial_lead_seconds=60,
        first_lead_seconds=30,
        final_lead_seconds=5,
        description=(
            "A real one-minute cycle: initial at creation, T-30s confirmation "
            "and T-5s final confirmation before the one-minute target."
        ),
    ),
    ForecastHorizon.MINUTES_5: HorizonSchedulePolicy(
        ForecastHorizon.MINUTES_5,
        target_rule="intraday_duration",
        initial_rule="fixed_lead",
        initial_lead_seconds=240,
        first_lead_seconds=120,
        final_lead_seconds=60,
        description=(
            "A five-minute horizon cannot start 20 minutes before its target; "
            "initial is T-4m, confirmation T-2m and final T-1m."
        ),
    ),
    ForecastHorizon.MINUTES_15: HorizonSchedulePolicy(
        ForecastHorizon.MINUTES_15,
        target_rule="intraday_duration",
        initial_rule="fixed_lead",
        initial_lead_seconds=720,
        first_lead_seconds=300,
        final_lead_seconds=60,
        description=(
            "Initial is T-12m, confirmation T-5m and final T-1m so the "
            "checkpoints remain distinct inside a 15-minute horizon."
        ),
    ),
    ForecastHorizon.MINUTES_20: HorizonSchedulePolicy(
        ForecastHorizon.MINUTES_20,
        target_rule="intraday_duration",
        initial_rule="fixed_lead",
        initial_lead_seconds=1200,
        first_lead_seconds=300,
        final_lead_seconds=60,
        description="The full T-20/T-5/T-1 intraday lifecycle.",
    ),
    ForecastHorizon.HOUR_1: HorizonSchedulePolicy(
        ForecastHorizon.HOUR_1,
        target_rule="intraday_duration",
        initial_rule="fixed_lead",
        initial_lead_seconds=1200,
        first_lead_seconds=300,
        final_lead_seconds=60,
        description=(
            "The full T-20/T-5/T-1 lifecycle, applied near the end of the "
            "one-hour target rather than minutes after it was created."
        ),
    ),
    ForecastHorizon.DAY_1: HorizonSchedulePolicy(
        ForecastHorizon.DAY_1,
        target_rule="swing_sessions",
        initial_rule="cycle_created",
        initial_lead_seconds=0,
        first_lead_seconds=3600,
        final_lead_seconds=60,
        description=(
            "One verified trading session ahead at its close; the initial "
            "version is created with the cycle, then T-60m/T-1m confirmations."
        ),
    ),
    ForecastHorizon.DAYS_3: HorizonSchedulePolicy(
        ForecastHorizon.DAYS_3,
        target_rule="swing_sessions",
        initial_rule="cycle_created",
        initial_lead_seconds=0,
        first_lead_seconds=86_400,
        final_lead_seconds=60,
        description=(
            "Three verified trading sessions ahead at its close; initial at "
            "cycle creation, confirmation one day before target, final T-1m."
        ),
    ),
    ForecastHorizon.DAYS_5: HorizonSchedulePolicy(
        ForecastHorizon.DAYS_5,
        target_rule="swing_sessions",
        initial_rule="cycle_created",
        initial_lead_seconds=0,
        first_lead_seconds=172_800,
        final_lead_seconds=60,
        description=(
            "Five verified trading sessions ahead at its close; initial at "
            "cycle creation, confirmation two days before target, final T-1m."
        ),
    ),
    ForecastHorizon.DAYS_30: HorizonSchedulePolicy(
        ForecastHorizon.DAYS_30,
        target_rule="calendar_days",
        initial_rule="cycle_created",
        initial_lead_seconds=0,
        first_lead_seconds=604_800,
        final_lead_seconds=172_800,
        description=(
            "Thirty calendar days, then the first verified session close; "
            "initial at cycle creation, seven-day confirmation and T-2d final."
        ),
    ),
    ForecastHorizon.DAYS_90: HorizonSchedulePolicy(
        ForecastHorizon.DAYS_90,
        target_rule="calendar_days",
        initial_rule="cycle_created",
        initial_lead_seconds=0,
        first_lead_seconds=1_209_600,
        final_lead_seconds=259_200,
        description=(
            "Ninety calendar days, then the first verified session close; "
            "initial at cycle creation, fourteen-day confirmation and T-3d final."
        ),
    ),
}


def horizon_policy(horizon: ForecastHorizon | str) -> HorizonSchedulePolicy:
    try:
        return HORIZON_SCHEDULE_POLICIES[ForecastHorizon(horizon)]
    except (ValueError, KeyError) as exc:
        raise PredictionValidationError(f"unknown horizon {horizon!r}") from exc


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    enabled: bool = True
    auto_schedule: bool = True
    auto_schedule_limit: int = 20
    tick_seconds: int = 10
    max_tasks_per_tick: int = 5
    lease_seconds: int = 90
    max_attempts: int = 3
    retry_backoff_seconds: int = 30
    max_outcome_delay_seconds: int = 86_400
    small_sample_size: int = 30
    revision_return_threshold: float = 0.0005
    revision_price_threshold: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise PredictionValidationError("scheduler.enabled must be boolean")
        if not isinstance(self.auto_schedule, bool):
            raise PredictionValidationError("scheduler.auto_schedule must be boolean")
        for name, low, high in (
            ("auto_schedule_limit", 1, 20),
            ("tick_seconds", 1, 300),
            ("max_tasks_per_tick", 1, 20),
            ("lease_seconds", 10, 600),
            ("max_attempts", 1, 10),
            ("retry_backoff_seconds", 1, 3600),
            ("max_outcome_delay_seconds", 60, 604_800),
            ("small_sample_size", 1, 10_000),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise PredictionValidationError(
                    f"scheduler.{name} must be an integer between {low} and {high}"
                )
        threshold = float(self.revision_return_threshold)
        if not math.isfinite(threshold) or not 0.0 < threshold <= 0.25:
            raise PredictionValidationError(
                "scheduler.revision_return_threshold must be within (0, 0.25]"
            )
        try:
            price_threshold = Decimal(str(self.revision_price_threshold))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PredictionValidationError(
                "scheduler.revision_price_threshold is not a decimal"
            ) from exc
        if not price_threshold.is_finite() or price_threshold <= 0:
            raise PredictionValidationError(
                "scheduler.revision_price_threshold must be finite and positive"
            )
        object.__setattr__(self, "revision_return_threshold", threshold)
        object.__setattr__(self, "revision_price_threshold", price_threshold)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_schedule": self.auto_schedule,
            "auto_schedule_limit": self.auto_schedule_limit,
            "tick_seconds": self.tick_seconds,
            "max_tasks_per_tick": self.max_tasks_per_tick,
            "lease_seconds": self.lease_seconds,
            "max_attempts": self.max_attempts,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "max_outcome_delay_seconds": self.max_outcome_delay_seconds,
            "small_sample_size": self.small_sample_size,
            "revision_return_threshold": self.revision_return_threshold,
            "revision_price_threshold": format(self.revision_price_threshold, "f"),
        }


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PredictionValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise PredictionValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PredictionValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ForecastCycle:
    """One stable identity for all versions of a target forecast."""

    cycle_id: str
    request_id: str
    instrument_id: str
    exchange: str
    horizon: ForecastHorizon
    resolution: str
    target_at: datetime
    initial_at: datetime
    first_confirmation_at: datetime
    final_confirmation_at: datetime
    state: CycleState
    created_at: datetime
    updated_at: datetime
    target_rule: str
    policy_version: str = SCHEDULE_POLICY_VERSION
    initial_forecast_id: str = ""
    first_forecast_id: str = ""
    final_forecast_id: str = ""
    outcome_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "cycle_id",
            "request_id",
            "instrument_id",
            "exchange",
            "resolution",
            "target_rule",
        ):
            if not str(getattr(self, name) or "").strip():
                raise PredictionValidationError(f"cycle.{name} is required")
        if ":" not in str(self.instrument_id):
            raise PredictionValidationError("cycle.instrument_id must be MIC:SYMBOL")
        try:
            horizon = ForecastHorizon(self.horizon)
            state = CycleState(self.state)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid cycle enum: {exc}") from exc
        for name in (
            "target_at",
            "initial_at",
            "first_confirmation_at",
            "final_confirmation_at",
            "created_at",
            "updated_at",
        ):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if not self.initial_at < self.first_confirmation_at < self.final_confirmation_at < self.target_at:
            raise PredictionValidationError(
                "cycle checkpoints must order initial < first < final < target"
            )
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "resolution", str(self.resolution))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "request_id": self.request_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon.value,
            "resolution": self.resolution,
            "target_at": self.target_at.isoformat(),
            "initial_at": self.initial_at.isoformat(),
            "first_confirmation_at": self.first_confirmation_at.isoformat(),
            "final_confirmation_at": self.final_confirmation_at.isoformat(),
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "target_rule": self.target_rule,
            "policy_version": self.policy_version,
            "initial_forecast_id": self.initial_forecast_id,
            "first_forecast_id": self.first_forecast_id,
            "final_forecast_id": self.final_forecast_id,
            "outcome_id": self.outcome_id,
            "metadata": dict(self.metadata),
            "contract_version": SCHEDULE_CONTRACT_VERSION,
            "is_forecast_cycle": True,
        }


@dataclass(frozen=True, slots=True)
class ForecastCheckpoint:
    """One durable scheduled step; the identity is stable across retries."""

    checkpoint_id: str
    cycle_id: str
    kind: CheckpointKind
    scheduled_at: datetime
    status: CheckpointState = CheckpointState.SCHEDULED
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_at: datetime | None = None
    lease_owner: str = ""
    lease_expires_at: datetime | None = None
    executed_at: datetime | None = None
    forecast_id: str = ""
    previous_forecast_id: str = ""
    revision: Mapping[str, Any] | None = None
    reasons: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.checkpoint_id or not self.cycle_id:
            raise PredictionValidationError("checkpoint and cycle identifiers are required")
        try:
            kind = CheckpointKind(self.kind)
            status = CheckpointState(self.status)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid checkpoint enum: {exc}") from exc
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 0:
            raise PredictionValidationError("checkpoint.attempts must be a non-negative integer")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise PredictionValidationError("checkpoint.max_attempts must be positive")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "scheduled_at", _aware(self.scheduled_at, "scheduled_at"))
        for name in ("next_attempt_at", "lease_expires_at", "executed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "revision", dict(self.revision) if self.revision else None)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            CheckpointState.COMPLETED,
            CheckpointState.SKIPPED,
            CheckpointState.ABSTAINED,
            CheckpointState.FAILED,
            CheckpointState.EVALUATED,
            CheckpointState.DEAD_LETTER,
        }

    def due_at(self) -> datetime:
        return self.next_attempt_at or self.scheduled_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "cycle_id": self.cycle_id,
            "kind": self.kind.value,
            "label": self.kind.label,
            "scheduled_at": self.scheduled_at.isoformat(),
            "due_at": self.due_at().isoformat(),
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "late": bool(self.executed_at and self.executed_at > self.scheduled_at),
            "forecast_id": self.forecast_id,
            "previous_forecast_id": self.previous_forecast_id,
            "revision": dict(self.revision) if self.revision else None,
            "reasons": list(self.reasons),
            "payload": dict(self.payload),
            "contract_version": SCHEDULE_CONTRACT_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ForecastRevision:
    previous_stage: ConfirmationStage
    current_stage: ConfirmationStage
    previous_forecast_id: str
    current_forecast_id: str
    price_revision: Decimal
    return_revision: float
    direction_changed: bool
    probability_changes: Mapping[str, float | None]
    interval_lower_change: float
    interval_upper_change: float
    uncertainty_changed: bool
    classification: RevisionClass
    previous_uncertainty: str = "unknown"
    current_uncertainty: str = "unknown"
    uncertainty_width_change: float = 0.0
    previous_members: tuple[str, ...] = ()
    current_members: tuple[str, ...] = ()
    previous_data_freshness: str = ""
    current_data_freshness: str = ""
    previous_ensemble_id: str = ""
    current_ensemble_id: str = ""
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_stage": self.previous_stage.value,
            "current_stage": self.current_stage.value,
            "previous_forecast_id": self.previous_forecast_id,
            "current_forecast_id": self.current_forecast_id,
            "price_revision": format(self.price_revision, "f"),
            "return_revision": round(self.return_revision, 8),
            "direction_changed": self.direction_changed,
            "probability_changes": dict(self.probability_changes),
            "interval_lower_change": round(self.interval_lower_change, 8),
            "interval_upper_change": round(self.interval_upper_change, 8),
            "uncertainty_changed": self.uncertainty_changed,
            "previous_uncertainty": self.previous_uncertainty,
            "current_uncertainty": self.current_uncertainty,
            "uncertainty_width_change": round(self.uncertainty_width_change, 8),
            "previous_members": list(self.previous_members),
            "current_members": list(self.current_members),
            "previous_data_freshness": self.previous_data_freshness,
            "current_data_freshness": self.current_data_freshness,
            "previous_ensemble_id": self.previous_ensemble_id,
            "current_ensemble_id": self.current_ensemble_id,
            "classification": self.classification.value,
            "reasons": list(self.reasons),
            "is_forecast_revision": True,
        }


@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    """Target-time evaluation of one immutable forecast version."""

    outcome_id: str
    cycle_id: str
    forecast_id: str
    stage: ConfirmationStage
    target_at: datetime
    actual_observed_at: datetime
    evaluated_at: datetime
    observation_delay_seconds: float
    actual_price: Decimal
    actual_return: float
    predicted_price: Decimal
    predicted_return: float
    predicted_direction: str
    actual_direction: str
    direction_correct: bool
    price_error: Decimal
    absolute_price_error: Decimal
    percentage_price_error: float
    return_error: float
    absolute_return_error: float
    inside_interval: bool
    interval_lower: float
    interval_upper: float
    interval_width: float
    status: str = "evaluated"
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.outcome_id or not self.cycle_id or not self.forecast_id:
            raise PredictionValidationError("outcome identifiers are required")
        try:
            stage = ConfirmationStage(self.stage)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid outcome stage: {exc}") from exc
        for name, dtype in (("actual_price", Decimal), ("predicted_price", Decimal)):
            value = getattr(self, name)
            try:
                number = value if isinstance(value, Decimal) else Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise PredictionValidationError(f"outcome.{name} is not decimal") from exc
            if not number.is_finite() or number <= 0:
                raise PredictionValidationError(f"outcome.{name} must be finite and positive")
            object.__setattr__(self, name, number)
        for name in ("price_error", "absolute_price_error"):
            value = getattr(self, name)
            try:
                number = value if isinstance(value, Decimal) else Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise PredictionValidationError(f"outcome.{name} is not decimal") from exc
            if not number.is_finite():
                raise PredictionValidationError(f"outcome.{name} must be finite")
            object.__setattr__(self, name, number)
        for name in (
            "target_at",
            "actual_observed_at",
            "evaluated_at",
        ):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.actual_observed_at < self.target_at:
            raise PredictionValidationError(
                "an outcome observation may not precede the target timestamp"
            )
        delay = max((self.actual_observed_at - self.target_at).total_seconds(), 0.0)
        object.__setattr__(self, "observation_delay_seconds", delay)
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "cycle_id": self.cycle_id,
            "forecast_id": self.forecast_id,
            "stage": self.stage.value,
            "target_at": self.target_at.isoformat(),
            "actual_observed_at": self.actual_observed_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat(),
            "observation_delay_seconds": round(self.observation_delay_seconds, 3),
            "actual_price": format(self.actual_price, "f"),
            "actual_return": round(self.actual_return, 8),
            "predicted_price": format(self.predicted_price, "f"),
            "predicted_return": round(self.predicted_return, 8),
            "predicted_direction": self.predicted_direction,
            "actual_direction": self.actual_direction,
            "direction_correct": self.direction_correct,
            "price_error": format(self.price_error, "f"),
            "absolute_price_error": format(self.absolute_price_error, "f"),
            "percentage_price_error": round(self.percentage_price_error, 8),
            "return_error": round(self.return_error, 8),
            "absolute_return_error": round(self.absolute_return_error, 8),
            "inside_interval": self.inside_interval,
            "interval_lower": round(self.interval_lower, 8),
            "interval_upper": round(self.interval_upper, 8),
            "interval_width": round(self.interval_width, 8),
            "status": self.status,
            "reason": self.reason,
            "metadata": dict(self.metadata),
            "contract_version": OUTCOME_CONTRACT_VERSION,
            "is_forecast_outcome": True,
        }


__all__ = [
    "CheckpointKind",
    "CheckpointState",
    "CycleState",
    "ForecastCheckpoint",
    "ForecastCycle",
    "ForecastOutcome",
    "ForecastRevision",
    "HORIZON_SCHEDULE_POLICIES",
    "HorizonSchedulePolicy",
    "OUTCOME_CONTRACT_VERSION",
    "OUTSTANDING_CYCLE_STATES",
    "PI09_PHASE",
    "RevisionClass",
    "SCHEDULE_CONTRACT_VERSION",
    "SCHEDULE_POLICY_VERSION",
    "SchedulerConfig",
    "horizon_policy",
]