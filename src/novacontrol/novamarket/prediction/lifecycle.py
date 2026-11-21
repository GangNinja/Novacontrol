"""Forecast lifecycle timing and change detection.

The target is T.  The required evidence points are T-20 (initial), T-5 (first
confirmation) and T-1 (final confirmation).  This module only computes those
times and compares two immutable forecasts; persistence and validation live in
the store and service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from novacontrol.novamarket.prediction.models import (
    ForecastOutput,
    ForecastValidity,
    LifecycleStatus,
    PredictionPolicy,
    PredictionValidationError,
    ConfirmationStage,
)


@dataclass(frozen=True, slots=True)
class LifecycleSchedule:
    target_at: datetime
    initial_at: datetime
    first_confirmation_at: datetime
    final_confirmation_at: datetime

    def stage_time(self, stage: ConfirmationStage | str) -> datetime:
        chosen = ConfirmationStage(stage)
        return {
            ConfirmationStage.INITIAL: self.initial_at,
            ConfirmationStage.FIRST: self.first_confirmation_at,
            ConfirmationStage.FINAL: self.final_confirmation_at,
        }[chosen]

    def to_dict(self) -> dict[str, str]:
        return {
            "target_at": self.target_at.isoformat(),
            "initial_at": self.initial_at.isoformat(),
            "first_confirmation_at": self.first_confirmation_at.isoformat(),
            "final_confirmation_at": self.final_confirmation_at.isoformat(),
        }


def schedule_for(
    target_at: datetime, policy: PredictionPolicy | None = None
) -> LifecycleSchedule:
    policy = policy or PredictionPolicy()
    if target_at.tzinfo is None or target_at.utcoffset() is None:
        raise PredictionValidationError("target_at must be timezone-aware")
    initial = target_at - timedelta(minutes=policy.initial_lead_minutes)
    first = target_at - timedelta(minutes=policy.first_confirmation_lead_minutes)
    final = target_at - timedelta(minutes=policy.final_confirmation_lead_minutes)
    if not initial < first < final < target_at:
        raise PredictionValidationError(
            "lifecycle schedule must order initial < first < final < target"
        )
    return LifecycleSchedule(
        target_at=target_at,
        initial_at=initial,
        first_confirmation_at=first,
        final_confirmation_at=final,
    )


def next_stage(lifecycle_status: LifecycleStatus | str) -> ConfirmationStage | None:
    """The only confirmation stage allowed now, or ``None`` when complete."""
    status = LifecycleStatus(lifecycle_status)
    return {
        LifecycleStatus.AWAITING_FIRST_CONFIRMATION: ConfirmationStage.FIRST,
        LifecycleStatus.AWAITING_FINAL_CONFIRMATION: ConfirmationStage.FINAL,
    }.get(status)


def stage_sequence() -> tuple[ConfirmationStage, ...]:
    return (
        ConfirmationStage.INITIAL,
        ConfirmationStage.FIRST,
        ConfirmationStage.FINAL,
    )


def changed_fields(
    previous: ForecastOutput,
    current: ForecastOutput,
    *,
    return_tolerance: float = 1e-9,
    probability_tolerance: float = 1e-6,
) -> tuple[str, ...]:
    """Fields whose meaning changed between two confirmation stages."""
    changes: list[str] = []
    if previous.direction is not current.direction:
        changes.append("direction")
    if abs(previous.predicted_return - current.predicted_return) > return_tolerance:
        changes.append("expected_return")
    if (
        abs(previous.lower_return - current.lower_return) > return_tolerance
        or abs(previous.upper_return - current.upper_return) > return_tolerance
    ):
        changes.append("price_range")
    if previous.uncertainty is not current.uncertainty:
        changes.append("uncertainty")
    if previous.confidence is not None and current.confidence is not None:
        if abs(previous.confidence - current.confidence) > probability_tolerance:
            changes.append("confidence")
    elif previous.confidence is not current.confidence:
        changes.append("confidence")
    for name in ("p_up", "p_down", "p_neutral"):
        old = getattr(previous, name)
        new = getattr(current, name)
        if old is None or new is None:
            if old is not new:
                changes.append(name)
        elif abs(old - new) > probability_tolerance:
            changes.append(name)
    if previous.data_observed_at != current.data_observed_at:
        changes.append("data_timestamp")
    if previous.data_resolution is not current.data_resolution:
        changes.append("data_resolution")
    if previous.data_freshness is not current.data_freshness:
        changes.append("data_freshness")
    return tuple(changes)


def has_actionable_final_confirmation(
    forecast: ForecastOutput | None,
    *,
    required_freshness_ok: bool,
) -> bool:
    if forecast is None or not required_freshness_ok:
        return False
    return (
        forecast.lifecycle_stage is ConfirmationStage.FINAL
        and forecast.validity in (ForecastValidity.VALID, ForecastValidity.CONFIRMED)
        and forecast.direction is not None
    )


def stage_label(stage: ConfirmationStage | str) -> str:
    return {
        ConfirmationStage.INITIAL: "T-20 initial forecast",
        ConfirmationStage.FIRST: "T-5 first confirmation",
        ConfirmationStage.FINAL: "T-1 final confirmation",
    }[ConfirmationStage(stage)]
