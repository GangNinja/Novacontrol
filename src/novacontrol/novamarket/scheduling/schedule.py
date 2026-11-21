"""Deterministic target and checkpoint calculation for PI-09.

All calculations use the verified exchange calendar.  A horizon that cannot
be mapped to a covered, valid session is refused rather than silently using a
weekday approximation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

from novacontrol.novamarket.marketdata.calendar import (
    ExchangeCalendar,
    SessionState,
    calendar_by_mic,
)
from novacontrol.novamarket.prediction.models import (
    ForecastHorizon,
    PredictionValidationError,
    prediction_id,
)
from novacontrol.novamarket.scheduling.models import (
    CheckpointKind,
    CheckpointState,
    CycleState,
    ForecastCheckpoint,
    ForecastCycle,
    SchedulerConfig,
    horizon_policy,
)


class ScheduleValidationError(PredictionValidationError):
    """A cycle cannot be scheduled without fabricating a session or target."""


def _roll_forward(calendar: ExchangeCalendar, day: date, *, limit: int = 20) -> date | None:
    for offset in range(limit):
        candidate = day + timedelta(days=offset)
        if calendar.is_trading_day(candidate):
            return candidate
    return None


def _sessions_ahead(
    calendar: ExchangeCalendar, start: date, count: int, *, limit: int = 400
) -> date | None:
    """The ``count``-th verified session strictly after ``start``."""
    if count < 1:
        raise ScheduleValidationError("a trading-session target must be positive")
    remaining = count
    for offset in range(1, limit + 1):
        candidate = start + timedelta(days=offset)
        if calendar.is_trading_day(candidate):
            remaining -= 1
            if remaining == 0:
                return candidate
    return None


def _require_calendar(exchange: str) -> ExchangeCalendar:
    calendar = calendar_by_mic(exchange)
    if calendar is None:
        raise ScheduleValidationError(
            f"no verified exchange calendar exists for {exchange!r}; failing closed"
        )
    return calendar


def _validate_target(
    calendar: ExchangeCalendar, target: datetime, *, horizon: ForecastHorizon
) -> tuple[datetime, datetime]:
    """Return the target session's open/close for a verified target time."""

    if target.tzinfo is None or target.utcoffset() is None:
        raise ScheduleValidationError("the forecast target must be timezone-aware")
    target = target.astimezone(UTC)
    if not calendar.coverage(target):
        raise ScheduleValidationError(
            f"calendar coverage for {calendar.mic} does not include {target.year}; "
            "a target in an unverified year is refused"
        )
    local = calendar.local(target)
    bounds = calendar.session_bounds(local.date())
    if bounds is None:
        raise ScheduleValidationError(
            f"{local.date().isoformat()} is not a verified trading day for {calendar.mic}"
        )
    opens, closes = bounds
    if not opens <= local <= closes:
        raise ScheduleValidationError(
            f"target {target.isoformat()} is outside {calendar.mic}'s verified "
            f"session ({opens.isoformat()} .. {closes.isoformat()})"
        )
    if horizon in (
        ForecastHorizon.MINUTES_5,
        ForecastHorizon.MINUTES_15,
        ForecastHorizon.MINUTES_20,
        ForecastHorizon.HOUR_1,
    ) and local >= closes:
        raise ScheduleValidationError(
            "an intraday target must be strictly before the verified session close"
        )
    return opens, closes


def default_target(
    *, horizon: ForecastHorizon, now: datetime, calendar: ExchangeCalendar
) -> datetime:
    """The deterministic default target for a new cycle."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleValidationError("the scheduling clock must be timezone-aware")
    now = now.astimezone(UTC)
    if not calendar.coverage(now):
        raise ScheduleValidationError(
            f"calendar coverage for {calendar.mic} does not include the current year; "
            "failing closed"
        )
    spec = horizon_policy(horizon)
    local_now = calendar.local(now)
    if spec.target_rule == "intraday_duration":
        duration = timedelta(seconds=_horizon_seconds(horizon))
        if calendar.continuous:
            return now + duration
        # If the current session can accommodate the whole duration, target it;
        # otherwise move to the next verified session's open + duration.
        for offset in range(0, 20):
            day = local_now.date() + timedelta(days=offset)
            bounds = calendar.session_bounds(day)
            if bounds is None:
                continue
            opens, closes = bounds
            candidate = max(local_now, opens) + duration
            if candidate <= closes:
                return candidate.astimezone(UTC)
        raise ScheduleValidationError(
            f"{horizon.value} cannot fit inside the verified {calendar.mic} session"
        )

    duration_days = max(1, int(timedelta(seconds=_horizon_seconds(horizon)).days))
    if spec.target_rule == "swing_sessions":
        target_day = _sessions_ahead(calendar, local_now.date(), duration_days)
    elif spec.target_rule == "calendar_days":
        target_day = _roll_forward(
            calendar, local_now.date() + timedelta(days=duration_days)
        )
    else:
        target_day = _roll_forward(
            calendar, local_now.date() + timedelta(days=duration_days)
        )
    if target_day is None:
        raise ScheduleValidationError(
            f"no verified trading day was found for {horizon.value} after {local_now.date()}"
        )
    if not calendar.coverage(datetime.combine(target_day, local_now.timetz())):
        raise ScheduleValidationError(
            f"calendar coverage for {calendar.mic} does not include target year "
            f"{target_day.year}; failing closed"
        )
    bounds = calendar.session_bounds(target_day)
    if bounds is None:
        raise ScheduleValidationError(f"{target_day} is not a verified trading day")
    return bounds[1].astimezone(UTC)


def _horizon_seconds(horizon: ForecastHorizon) -> int:
    # The PI-01 horizon contract owns the duration.  Import lazily so the
    # scheduler package can be imported by contracts without a cycle.
    from novacontrol.novamarket.prediction.models import HORIZON_SPECS

    return int(HORIZON_SPECS[ForecastHorizon(horizon)].duration.total_seconds())


def schedule_times(
    *,
    horizon: ForecastHorizon,
    target: datetime,
    calendar: ExchangeCalendar,
    initial_at: datetime | None = None,
) -> tuple[datetime, datetime, datetime]:
    """Initial, first-confirmation and final-confirmation instants."""

    opens, closes = _validate_target(calendar, target, horizon=horizon)
    target = target.astimezone(UTC)
    policy = horizon_policy(horizon)
    if policy.initial_rule == "cycle_created":
        initial = (
            initial_at.astimezone(UTC)
            if initial_at is not None
            else target - timedelta(seconds=policy.first_lead_seconds + 1)
        )
    elif policy.initial_rule == "session_open":
        initial = opens.astimezone(UTC)
    else:
        initial = target - timedelta(seconds=policy.initial_lead_seconds)
    first = target - timedelta(seconds=policy.first_lead_seconds)
    final = target - timedelta(seconds=policy.final_lead_seconds)
    # Early closes and unusual sessions can make the nominal T-60m fall before
    # the open.  Keep the ordering explicit rather than silently reordering.
    if initial >= first:
        first = initial + timedelta(seconds=60)
    if first >= final:
        final = first + timedelta(seconds=1)
    if final >= target:
        raise ScheduleValidationError(
            "the checkpoint policy leaves no room before the target"
        )
    return initial, first, final


def build_cycle(
    *,
    instrument_id: str,
    exchange: str,
    horizon: ForecastHorizon | str,
    now: datetime,
    target_at: datetime | None = None,
    resolution: str | None = None,
    config: SchedulerConfig | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ForecastCycle:
    """Create a stable cycle and its three ordered checkpoint plans."""

    del clock  # the explicit ``now`` is the single scheduling clock
    config = config or SchedulerConfig()
    try:
        horizon_value = ForecastHorizon(horizon)
    except ValueError as exc:
        raise ScheduleValidationError(f"unknown horizon {horizon!r}") from exc
    if now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleValidationError("the scheduling clock must be timezone-aware")
    now = now.astimezone(UTC)
    calendar = _require_calendar(exchange)
    if not calendar.coverage(now):
        raise ScheduleValidationError(
            f"calendar coverage for {calendar.mic} does not include the current year; "
            "failing closed"
        )
    target = (
        target_at.astimezone(UTC)
        if target_at is not None
        else default_target(horizon=horizon_value, now=now, calendar=calendar)
    )
    if target <= now:
        raise ScheduleValidationError("a forecast target must be in the future")
    initial, first, final = schedule_times(
        horizon=horizon_value, target=target, calendar=calendar, initial_at=now
    )
    spec = horizon_policy(horizon_value)
    if spec.target_rule in {"session_close", "swing_sessions", "calendar_days"}:
        _, verified_close = _validate_target(calendar, target, horizon=horizon_value)
        if target != verified_close.astimezone(UTC):
            raise ScheduleValidationError(
                "swing/long targets must be the verified target session close; "
                "omit target_at to let the calendar choose it"
            )
    resolved = resolution or _default_resolution(horizon_value)
    cycle_id = prediction_id(
        "pcyc_",
        instrument_id.upper(),
        exchange.upper(),
        horizon_value.value,
        target.isoformat(),
    )
    request_id = prediction_id("preq_", cycle_id)
    return ForecastCycle(
        cycle_id=cycle_id,
        request_id=request_id,
        instrument_id=instrument_id.upper(),
        exchange=exchange.upper(),
        horizon=horizon_value,
        resolution=resolved,
        target_at=target,
        initial_at=initial,
        first_confirmation_at=first,
        final_confirmation_at=final,
        state=CycleState.SCHEDULED,
        created_at=now,
        updated_at=now,
        target_rule=spec.target_rule,
        metadata={
            "scheduled_by": "pi09",
            "calendar_mic": calendar.mic,
            "calendar_timezone": calendar.timezone,
            "calendar_coverage": True,
            "horizon_policy": spec.to_dict(),
            "checkpoint_plans": [
                {"kind": kind.value, "scheduled_at": when.isoformat()}
                for kind, when in (
                    (CheckpointKind.INITIAL, initial),
                    (CheckpointKind.FIRST, first),
                    (CheckpointKind.FINAL, final),
                )
            ],
        },
    )


def build_checkpoints(
    cycle: ForecastCycle, *, config: SchedulerConfig | None = None
) -> tuple[ForecastCheckpoint, ...]:
    config = config or SchedulerConfig()
    plans = (
        (CheckpointKind.INITIAL, cycle.initial_at),
        (CheckpointKind.FIRST, cycle.first_confirmation_at),
        (CheckpointKind.FINAL, cycle.final_confirmation_at),
        (CheckpointKind.OUTCOME, cycle.target_at),
    )
    return tuple(
        ForecastCheckpoint(
            checkpoint_id=prediction_id("pchk_", cycle.cycle_id, kind.value, when.isoformat()),
            cycle_id=cycle.cycle_id,
            kind=kind,
            scheduled_at=when,
            status=CheckpointState.SCHEDULED,
            max_attempts=config.max_attempts,
        )
        for kind, when in plans
    )


def _default_resolution(horizon: ForecastHorizon) -> str:
    from novacontrol.novamarket.prediction.models import HORIZON_SPECS

    return HORIZON_SPECS[ForecastHorizon(horizon)].max_input_interval.value


def session_snapshot(calendar: ExchangeCalendar, now: datetime) -> dict[str, Any]:
    """The honest session decision used by cycle validation and status."""

    if not calendar.coverage(now):
        return {
            "calendar": calendar.to_dict(),
            "state": SessionState.UNKNOWN.value,
            "coverage": False,
            "verified": False,
            "reason": "calendar coverage is missing for the current year",
        }
    state = calendar.state(now)
    bounds = calendar.session_bounds(calendar.local(now).date())
    return {
        "calendar": calendar.to_dict(),
        "state": state.value,
        "coverage": True,
        "verified": True,
        "opens_at": bounds[0].isoformat() if bounds else None,
        "closes_at": bounds[1].isoformat() if bounds else None,
        "exchange_local": calendar.local(now).isoformat(),
    }


__all__ = [
    "ScheduleValidationError",
    "build_checkpoints",
    "build_cycle",
    "default_target",
    "schedule_times",
    "session_snapshot",
]