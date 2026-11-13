"""Exchange calendars: sessions, weekends, holidays and early closes.

Freshness is meaningless without knowing whether an exchange is supposed to be
trading. A universal "stale after 15 minutes" rule would call a Friday close
invalid on Saturday and would present a holiday's last price as if it were live.

The calendars here are deliberately NARROW and dated:

* NSE and BSE: 09:15–15:30 Asia/Kolkata, using the National Stock Exchange's
  published 2026 capital-market holiday list (NSE circular NSE/CMTR/71775 plus
  the January 15, 2026 municipal-election closure, circular 03/2026).
* NYSE, Nasdaq, NYSE Arca and NYSE American: 09:30–16:00 America/New_York,
  using the NYSE Group's published 2026 holiday and early-close calendar.
* Crypto: continuous, because the market really is.
* Every other exchange: NO calendar. The oracle returns ``None`` and freshness
  says it cannot tell, rather than pretending a generic weekday schedule is the
  exchange's own.

Coverage is a year set, not an open-ended promise. Outside a covered year the
calendar still knows the session times and weekdays but marks ``coverage=False``
so a caller can say "weekday schedule; holiday coverage not verified". That is
the honest answer, and it is what the freshness reasons carry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from novacontrol.novamarket.marketdata.models import Instrument, AssetClass


class SessionState(StrEnum):
    """What an exchange is doing at a moment, and why."""

    OPEN = "open"
    PRE_OPEN = "pre_open"
    CLOSED = "closed"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    CONTINUOUS = "continuous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TradingSession:
    """One regular session in the exchange's local time."""

    open_time: time
    close_time: time

    def __post_init__(self) -> None:
        if self.open_time >= self.close_time:
            raise ValueError("a trading session must open before it closes")


def _holidays(year: int, rows: Mapping[date, str]) -> dict[date, str]:
    return {day: reason for day, reason in rows.items() if day.year == year}


#: NSE capital-market holidays, 2026. Weekend holidays are listed by NSE but are
#: already weekends; they are omitted here so the holiday count means "weekday
#: closures". Source: NSE circular NSE/CMTR/71775 (2026 list) and circular
#: 03/2026 (15 January municipal-election closure).
_NSE_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 15): "Municipal Corporation Election - Maharashtra",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

#: NYSE Group holidays, 2026. Source: NYSE "Holidays & Trading Hours" page
#: (https://www.nyse.com/markets/hours-calendars), which lists 2026–2028.
_US_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth National Independence Day",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
}

#: 1:00 p.m. early closes published by NYSE for 2026.
_US_EARLY_CLOSES_2026: dict[date, time] = {
    date(2026, 11, 27): time(13, 0),
    date(2026, 12, 24): time(13, 0),
}

_NSE_SESSIONS = (TradingSession(time(9, 15), time(15, 30)),)
_US_SESSIONS = (TradingSession(time(9, 30), time(16, 0)),)
_CRYPTO_SESSIONS = (TradingSession(time(0, 0), time(23, 59, 59)),)


@dataclass(frozen=True, slots=True)
class ExchangeCalendar:
    """A dated session calendar for one exchange identity."""

    mic: str
    name: str
    timezone: str
    sessions: tuple[TradingSession, ...]
    holidays: Mapping[date, str]
    early_closes: Mapping[date, time]
    coverage_years: frozenset[int]
    holiday_source: str
    continuous: bool = False

    def __post_init__(self) -> None:
        if not self.sessions:
            raise ValueError("a calendar needs at least one trading session")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001 - both error types are refusals
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
        object.__setattr__(self, "holidays", dict(self.holidays))
        object.__setattr__(self, "early_closes", dict(self.early_closes))
        object.__setattr__(self, "coverage_years", frozenset(self.coverage_years))

    # -- day-level facts ------------------------------------------------------

    def local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("a calendar comparison needs a timezone-aware datetime")
        return moment.astimezone(ZoneInfo(self.timezone))

    def coverage(self, moment: datetime) -> bool:
        return self.local(moment).year in self.coverage_years

    def is_trading_day(self, day: date) -> bool:
        if self.continuous:
            return True
        if day.weekday() >= 5:
            return False
        return day not in self.holidays

    def holiday_name(self, day: date) -> str:
        return self.holidays.get(day, "")

    def session_close(self, day: date) -> time:
        """The regular close for a day, honouring a published early close."""
        if self.continuous:
            return time(23, 59, 59)
        return self.early_closes.get(day, self.sessions[-1].close_time)

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        """The day's first open and last close in the exchange timezone."""
        if not self.is_trading_day(day):
            return None
        zone = ZoneInfo(self.timezone)
        start = datetime.combine(day, self.sessions[0].open_time, tzinfo=zone)
        end = datetime.combine(day, self.session_close(day), tzinfo=zone)
        return start, end

    def state(self, moment: datetime) -> SessionState:
        """What the exchange is doing now; never guesses at an unknown calendar."""
        if self.continuous:
            return SessionState.CONTINUOUS
        if not self.coverage(moment):
            # Session times are still known, but the holiday list is not, so the
            # answer says which part is unverified rather than calling a holiday
            # a trading day.
            return self._state_from_clock(moment, trusted_holidays=False)
        local = self.local(moment)
        if local.weekday() >= 5:
            return SessionState.WEEKEND
        if local.date() in self.holidays:
            return SessionState.HOLIDAY
        return self._state_from_clock(moment, trusted_holidays=True)

    def _state_from_clock(
        self, moment: datetime, *, trusted_holidays: bool
    ) -> SessionState:
        local = self.local(moment)
        day = local.date()
        if not trusted_holidays and day in self.holidays:
            return SessionState.HOLIDAY
        if local.time() < self.sessions[0].open_time:
            return SessionState.PRE_OPEN
        close_time = self.session_close(day)
        if local.time() >= close_time:
            return SessionState.CLOSED
        for session in self.sessions:
            if session.open_time <= local.time() < self.session_close(day):
                return SessionState.OPEN
        return SessionState.CLOSED

    def is_open(self, moment: datetime) -> bool:
        return self.state(moment) in (SessionState.OPEN, SessionState.CONTINUOUS)

    # -- neighbouring sessions ------------------------------------------------

    def last_session_close(self, moment: datetime) -> datetime | None:
        """The most recent completed regular close at or before ``moment``."""
        local = self.local(moment)
        for offset in range(0, 15):
            day = local.date() - timedelta(days=offset)
            bounds = self.session_bounds(day)
            if bounds is None:
                continue
            close = bounds[1]
            if close <= local:
                return close
        return None

    def last_completed_session(self, moment: datetime) -> date | None:
        close = self.last_session_close(moment)
        return close.date() if close is not None else None

    def next_session_open(self, moment: datetime) -> datetime | None:
        local = self.local(moment)
        for offset in range(0, 15):
            day = local.date() + timedelta(days=offset)
            bounds = self.session_bounds(day)
            if bounds is None:
                continue
            opens = bounds[0]
            if opens > local:
                return opens
        return None

    def sessions_between(self, start: datetime, end: datetime) -> int:
        """Completed sessions in ``[start, end]``, bounded to 366 days."""
        if end < start:
            return 0
        local_start = self.local(start).date()
        local_end = self.local(end).date()
        if (local_end - local_start).days > 366:
            raise ValueError("sessions_between is limited to one year")
        count = 0
        day = local_start
        while day <= local_end:
            if self.is_trading_day(day):
                count += 1
            day += timedelta(days=1)
        return count

    def to_dict(self) -> dict[str, Any]:
        return {
            "mic": self.mic,
            "name": self.name,
            "timezone": self.timezone,
            "sessions": [
                {
                    "open": session.open_time.isoformat(timespec="minutes"),
                    "close": session.close_time.isoformat(timespec="minutes"),
                }
                for session in self.sessions
            ],
            "continuous": self.continuous,
            "coverage_years": sorted(self.coverage_years),
            "verified": bool(self.coverage_years),
            "holiday_count": len(self.holidays),
            "early_close_count": len(self.early_closes),
            "holiday_source": self.holiday_source,
            "maintenance": (
                "Annual source review; coverage is explicit per year and is "
                "never extrapolated to an unlisted year."
            ),
        }


_NSE = ExchangeCalendar(
    mic="XNSE",
    name="National Stock Exchange of India",
    timezone="Asia/Kolkata",
    sessions=_NSE_SESSIONS,
    holidays=_NSE_HOLIDAYS_2026,
    early_closes={},
    coverage_years=frozenset({2026}),
    holiday_source=(
        "NSE circular NSE/CMTR/71775 (2026 holidays) and circular 03/2026 "
        "(15-Jan-2026 municipal-election closure)"
    ),
)
_BSE = ExchangeCalendar(
    mic="XBOM",
    name="BSE Ltd.",
    timezone="Asia/Kolkata",
    sessions=_NSE_SESSIONS,
    holidays=_NSE_HOLIDAYS_2026,
    early_closes={},
    coverage_years=frozenset({2026}),
    holiday_source="Same 2026 equity-market holiday list as NSE",
)
_US: dict[str, Any] = dict(
    timezone="America/New_York",
    sessions=_US_SESSIONS,
    holidays=_US_HOLIDAYS_2026,
    early_closes=_US_EARLY_CLOSES_2026,
    coverage_years=frozenset({2026}),
    holiday_source="NYSE Group 2026 Holidays & Trading Hours calendar",
)
_CALENDARS: dict[str, ExchangeCalendar] = {
    "XNSE": _NSE,
    "XBOM": _BSE,
    "XNAS": ExchangeCalendar(mic="XNAS", name="Nasdaq", **_US),
    "XNYS": ExchangeCalendar(mic="XNYS", name="New York Stock Exchange", **_US),
    "ARCX": ExchangeCalendar(mic="ARCX", name="NYSE Arca", **_US),
    "XASE": ExchangeCalendar(mic="XASE", name="NYSE American", **_US),
    "CRYPTO": ExchangeCalendar(
        mic="CRYPTO",
        name="Crypto (continuous)",
        timezone="UTC",
        sessions=_CRYPTO_SESSIONS,
        holidays={},
        early_closes={},
        coverage_years=frozenset({2026}),
        holiday_source="Crypto markets trade continuously; no holiday calendar",
        continuous=True,
    ),
}


def calendar_for(instrument: Instrument) -> ExchangeCalendar | None:
    """The verified calendar for an instrument, or ``None`` if none exists.

    Crypto uses the ``CRYPTO`` calendar even if an instrument's MIC is a venue
    code; the asset class is the stronger fact for session purposes.
    """
    if instrument.asset_class is AssetClass.CRYPTO:
        return _CALENDARS["CRYPTO"]
    if instrument.asset_class is AssetClass.CURRENCY:
        # FX sessions are venue- and product-specific; NM-02 does not claim a
        # verified FX calendar, so it does not fabricate one.
        return None
    return _CALENDARS.get(instrument.mic)


def calendar_by_mic(mic: str) -> ExchangeCalendar | None:
    """The verified calendar for a market identifier, or ``None``.

    This is the read-only door for the runtime monitor, which needs the NSE/BSE
    session without inventing a temporary ``Instrument``.
    """
    return _CALENDARS.get(str(mic or "").strip().upper())


def calendars() -> tuple[ExchangeCalendar, ...]:
    seen: set[str] = set()
    unique: list[ExchangeCalendar] = []
    for calendar in _CALENDARS.values():
        if calendar.mic in seen:
            continue
        seen.add(calendar.mic)
        unique.append(calendar)
    return tuple(unique)


def calendar_contract() -> dict[str, Any]:
    """The calendar coverage as data, for ``/market/contracts``."""
    return {
        "calendars": [calendar.to_dict() for calendar in calendars()],
        "coverage_note": (
            "Holiday coverage is verified for 2026 only. Outside a covered year "
            "session times and weekdays still apply, but freshness reports "
            "calendar_coverage=false and does not claim a holiday closure."
        ),
    }


__all__ = [
    "ExchangeCalendar",
    "SessionState",
    "TradingSession",
    "calendar_by_mic",
    "calendar_contract",
    "calendar_for",
    "calendars",
]
