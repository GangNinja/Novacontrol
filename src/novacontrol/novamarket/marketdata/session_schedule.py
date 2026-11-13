"""Deterministic refresh cadence for the NSE/BSE workday.

The exchange calendar decides whether the market is actually open.  This module
turns that answer into one refresh plan, so the API, the background monitor and
the browser do not each invent their own timer:

* 09:00–17:00 IST is the operating window.
* While the exchange is open, the plan is LIVE (fifteen minutes by default).
* While it is still closed, the plan probes every 15 minutes, at most six times
  (09:00 through 10:30).  After those probes it falls back to hourly.
* At 17:00, and outside the operating window, it is hourly.

The data cadence is fifteen minutes; there is no millisecond polling.  The
interval is a refresh cadence, not a claim that a delayed provider has exchange
ticks.  ``provider_note`` carries that distinction into every caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from novacontrol.novamarket.marketdata.calendar import (
    ExchangeCalendar,
    SessionState,
    calendar_by_mic,
)


class RefreshPhase(StrEnum):
    """What the refresh loop is doing right now."""

    OVERNIGHT = "overnight"
    PROBING = "probing"
    LIVE = "live"
    HOURLY = "hourly"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MarketRefreshPlan:
    """One deterministic answer to "when should the next refresh happen?"."""

    phase: RefreshPhase
    interval_seconds: int
    next_check_at: datetime
    live: bool
    attempts: int
    max_attempts: int
    session_state: str
    reason: str
    timezone: str
    local_time: str
    window_start: str
    window_end: str
    provider_note: str = (
        "The configured Yahoo feed is delayed and unofficial; this is a "
        "15-minute refresh cadence, not exchange tick data."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "live": self.live,
            "interval_seconds": self.interval_seconds,
            "interval_ms": self.interval_seconds * 1000,
            "next_check_at": self.next_check_at.isoformat(),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "session_state": self.session_state,
            "reason": self.reason,
            "timezone": self.timezone,
            "local_time": self.local_time,
            "window": {"start": self.window_start, "end": self.window_end},
            "provider_note": self.provider_note,
        }


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a refresh plan needs a timezone-aware datetime")
    return moment


def plan_market_refresh(
    moment: datetime | None = None,
    *,
    calendar: ExchangeCalendar | None = None,
    window_start: time = time(9, 0),
    window_end: time = time(17, 0),
    live_interval_seconds: int = 15 * 60,
    probe_interval_seconds: int = 15 * 60,
    max_probes: int = 6,
    hourly_interval_seconds: int = 60 * 60,
) -> MarketRefreshPlan:
    """Return the single refresh plan for ``moment``.

    ``calendar`` is injectable so tests and other exchanges can use the same
    policy without changing the clock.  With no calendar, the answer is honest
    UNKNOWN and falls back to hourly.
    """
    now = _aware(moment or datetime.now(UTC))
    active_calendar = calendar or calendar_by_mic("XNSE")
    if active_calendar is None:
        return MarketRefreshPlan(
            phase=RefreshPhase.UNKNOWN,
            interval_seconds=hourly_interval_seconds,
            next_check_at=now + timedelta(seconds=hourly_interval_seconds),
            live=False,
            attempts=0,
            max_attempts=max_probes,
            session_state=SessionState.UNKNOWN.value,
            reason="no verified NSE/BSE calendar is installed",
            timezone="",
            local_time=now.isoformat(),
            window_start=window_start.isoformat(timespec="minutes"),
            window_end=window_end.isoformat(timespec="minutes"),
        )

    local = active_calendar.local(now)
    zone = ZoneInfo(active_calendar.timezone)
    start = datetime.combine(local.date(), window_start, tzinfo=zone)
    end = datetime.combine(local.date(), window_end, tzinfo=zone)
    state = active_calendar.state(now)
    common: dict[str, Any] = {
        "session_state": state.value,
        "timezone": active_calendar.timezone,
        "local_time": local.isoformat(),
        "window_start": window_start.isoformat(timespec="minutes"),
        "window_end": window_end.isoformat(timespec="minutes"),
        "max_attempts": max_probes,
    }

    if local < start:
        # Before the window, wake at 09:00 unless the next hourly tick is sooner.
        next_check = min(now + timedelta(seconds=hourly_interval_seconds), start)
        return MarketRefreshPlan(
            phase=RefreshPhase.OVERNIGHT,
            interval_seconds=max(
                1, min(hourly_interval_seconds, int((next_check - now).total_seconds()))
            ),
            next_check_at=next_check,
            live=False,
            attempts=0,
            reason="outside the 09:00-17:00 operating window",
            **common,
        )

    if local >= end:
        return MarketRefreshPlan(
            phase=RefreshPhase.HOURLY,
            interval_seconds=hourly_interval_seconds,
            next_check_at=now + timedelta(seconds=hourly_interval_seconds),
            live=False,
            attempts=max_probes,
            reason="the 17:00 operating window has ended; hourly refresh",
            **common,
        )

    if state in (SessionState.OPEN, SessionState.CONTINUOUS):
        return MarketRefreshPlan(
            phase=RefreshPhase.LIVE,
            interval_seconds=max(1, live_interval_seconds),
            next_check_at=now + timedelta(seconds=max(1, live_interval_seconds)),
            live=True,
            attempts=0,
            reason=f"the exchange is {state.value}; the session refresh window is active",
            **common,
        )

    elapsed = max(0.0, (local - start).total_seconds())
    attempt = int(elapsed // probe_interval_seconds)
    if attempt < max_probes:
        next_check = start + timedelta(seconds=(attempt + 1) * probe_interval_seconds)
        if next_check > end:
            next_check = end
        return MarketRefreshPlan(
            phase=RefreshPhase.PROBING,
            interval_seconds=probe_interval_seconds,
            next_check_at=next_check,
            live=False,
            attempts=attempt + 1,
            reason=(
                f"the exchange is {state.value}; "
                f"open probe {attempt + 1}/{max_probes} in {probe_interval_seconds // 60} minutes"
            ),
            **common,
        )

    return MarketRefreshPlan(
        phase=RefreshPhase.HOURLY,
        interval_seconds=hourly_interval_seconds,
        next_check_at=now + timedelta(seconds=hourly_interval_seconds),
        live=False,
        attempts=max_probes,
        reason=(
            f"the exchange is {state.value}; all {max_probes} open probes are "
            "exhausted, so refresh hourly"
        ),
        **common,
    )


__all__ = ["MarketRefreshPlan", "RefreshPhase", "plan_market_refresh"]
