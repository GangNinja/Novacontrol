"""AT-02 verified exchange-session evaluation for autonomy.

This module is the ONLY session gate for autonomy.  It composes the verified
NM-02 exchange calendar and never duplicates holiday knowledge, never assumes a
weekday is a trading day, and never extrapolates coverage beyond the years the
calendar declares.

Owner decision OD-04: only explicitly verified calendar coverage may be used.
A missing calendar or an unlisted year fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from novacontrol.novamarket.marketdata.calendar import (
    ExchangeCalendar,
    SessionState,
    calendar_by_mic,
)


class SessionVerdict(StrEnum):
    """The honest answer to "may monitoring run for this exchange now?"."""

    TRADING_SESSION = "trading_session"
    CONTINUOUS = "continuous"
    PRE_OPEN = "market_not_open"
    CLOSED = "market_closed"
    WEEKEND = "weekend"
    HOLIDAY = "exchange_holiday"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    CALENDAR_UNVERIFIED = "calendar_unverified"
    UNKNOWN = "unknown"


class SessionReason(StrEnum):
    """Stable reason codes carried by status, cycles and the API."""

    TRADING_SESSION = "TRADING_SESSION"
    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
    MARKET_CLOSED = "MARKET_CLOSED"
    WEEKEND = "WEEKEND"
    EXCHANGE_HOLIDAY = "EXCHANGE_HOLIDAY"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    CALENDAR_UNVERIFIED = "CALENDAR_UNVERIFIED"


_VERDICT_REASON: dict[SessionVerdict, SessionReason] = {
    SessionVerdict.TRADING_SESSION: SessionReason.TRADING_SESSION,
    SessionVerdict.CONTINUOUS: SessionReason.TRADING_SESSION,
    SessionVerdict.PRE_OPEN: SessionReason.MARKET_NOT_OPEN,
    SessionVerdict.CLOSED: SessionReason.MARKET_CLOSED,
    SessionVerdict.WEEKEND: SessionReason.WEEKEND,
    SessionVerdict.HOLIDAY: SessionReason.EXCHANGE_HOLIDAY,
    SessionVerdict.CALENDAR_UNAVAILABLE: SessionReason.CALENDAR_UNAVAILABLE,
    SessionVerdict.CALENDAR_UNVERIFIED: SessionReason.CALENDAR_UNVERIFIED,
    SessionVerdict.UNKNOWN: SessionReason.CALENDAR_UNAVAILABLE,
}

_ELIGIBLE = frozenset({SessionVerdict.TRADING_SESSION, SessionVerdict.CONTINUOUS})

_REASON_TEXT: dict[SessionReason, str] = {
    SessionReason.TRADING_SESSION: "the exchange is inside a verified trading session",
    SessionReason.MARKET_NOT_OPEN: "the exchange has not opened yet",
    SessionReason.MARKET_CLOSED: "the exchange is closed",
    SessionReason.WEEKEND: "the exchange is closed for the weekend",
    SessionReason.EXCHANGE_HOLIDAY: "the exchange is closed for a verified holiday",
    SessionReason.CALENDAR_UNAVAILABLE: (
        "no verified exchange calendar is installed for this exchange"
    ),
    SessionReason.CALENDAR_UNVERIFIED: (
        "the calendar does not have verified coverage for this date"
    ),
}


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """A deterministic, timezone-aware session answer with its evidence."""

    exchange: str
    eligible: bool
    verdict: SessionVerdict
    reason_code: str
    reason: str
    trading_date: str
    local_time: str
    timezone: str
    session_state: str
    session_open: str
    session_close: str
    coverage_year: int
    calendar_verified: bool
    calendar_source: str
    next_open: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "eligible": self.eligible,
            "verdict": self.verdict.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "trading_date": self.trading_date,
            "local_time": self.local_time,
            "timezone": self.timezone,
            "session_state": self.session_state,
            "session_open": self.session_open,
            "session_close": self.session_close,
            "coverage_year": self.coverage_year,
            "calendar_verified": self.calendar_verified,
            "calendar_source": self.calendar_source,
            "next_open": self.next_open,
            "timezone_aware": True,
        }


def _blank_decision(
    mic: str,
    moment: datetime,
    verdict: SessionVerdict,
    reason: SessionReason,
    *,
    calendar: ExchangeCalendar | None = None,
) -> SessionDecision:
    local = calendar.local(moment) if calendar is not None else moment
    return SessionDecision(
        exchange=mic,
        eligible=False,
        verdict=verdict,
        reason_code=reason.value,
        reason=_REASON_TEXT[reason],
        trading_date=local.date().isoformat(),
        local_time=local.isoformat(),
        timezone=calendar.timezone if calendar is not None else "",
        session_state=verdict.value,
        session_open="",
        session_close="",
        coverage_year=local.year,
        calendar_verified=False,
        calendar_source=calendar.holiday_source if calendar is not None else "",
        next_open="",
    )


def evaluate_session(exchange: str, moment: datetime) -> SessionDecision:
    """Evaluate one exchange at ``moment``; fail closed when unverifiable.

    ``moment`` must be timezone-aware.  All comparisons use the exchange's own
    IANA timezone, never the host machine's local time.
    """

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("session evaluation needs a timezone-aware datetime")
    mic = str(exchange or "").strip().upper()
    calendar = calendar_by_mic(mic)
    if calendar is None:
        return _blank_decision(
            mic, moment, SessionVerdict.CALENDAR_UNAVAILABLE,
            SessionReason.CALENDAR_UNAVAILABLE,
        )
    if not calendar.coverage(moment):
        return _blank_decision(
            mic,
            moment,
            SessionVerdict.CALENDAR_UNVERIFIED,
            SessionReason.CALENDAR_UNVERIFIED,
            calendar=calendar,
        )

    local = calendar.local(moment)
    state = calendar.state(moment)
    verdict = _VERDICT_FOR_STATE.get(state, SessionVerdict.UNKNOWN)
    if verdict is SessionVerdict.UNKNOWN:
        return _blank_decision(
            mic,
            moment,
            SessionVerdict.UNKNOWN,
            SessionReason.CALENDAR_UNAVAILABLE,
            calendar=calendar,
        )
    reason = _VERDICT_REASON[verdict]
    bounds = calendar.session_bounds(local.date())
    next_open = calendar.next_session_open(moment)
    return SessionDecision(
        exchange=mic,
        eligible=verdict in _ELIGIBLE,
        verdict=verdict,
        reason_code=reason.value,
        reason=_REASON_TEXT[reason],
        trading_date=local.date().isoformat(),
        local_time=local.isoformat(),
        timezone=calendar.timezone,
        session_state=state.value,
        session_open=bounds[0].isoformat() if bounds else "",
        session_close=bounds[1].isoformat() if bounds else "",
        coverage_year=local.year,
        calendar_verified=True,
        calendar_source=calendar.holiday_source,
        next_open=next_open.isoformat() if next_open else "",
    )


_VERDICT_FOR_STATE: dict[SessionState, SessionVerdict] = {
    SessionState.OPEN: SessionVerdict.TRADING_SESSION,
    SessionState.CONTINUOUS: SessionVerdict.CONTINUOUS,
    SessionState.PRE_OPEN: SessionVerdict.PRE_OPEN,
    SessionState.CLOSED: SessionVerdict.CLOSED,
    SessionState.WEEKEND: SessionVerdict.WEEKEND,
    SessionState.HOLIDAY: SessionVerdict.HOLIDAY,
    SessionState.UNKNOWN: SessionVerdict.UNKNOWN,
}


def session_contract() -> dict[str, Any]:
    """The static session rules the autonomy layer enforces."""

    return {
        "milestone": "AT-02",
        "evaluator": "novacontrol.novamarket.autonomy.session",
        "calendar_source": "marketdata/calendar.py",
        "verified_years": [2026],
        "coverage_is_explicit": True,
        "fail_closed_outside_coverage": True,
        "assumes_weekdays_are_trading_days": False,
        "uses_host_local_timezone": False,
        "verdicts": [item.value for item in SessionVerdict],
        "reason_codes": [item.value for item in SessionReason],
    }


__all__ = [
    "SessionDecision",
    "SessionReason",
    "SessionVerdict",
    "evaluate_session",
    "session_contract",
]
