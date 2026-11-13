"""Declared trading sessions for discovery scheduling (NM-05).

A session is **declared**, never inferred: one row per exchange MIC carrying its
IANA timezone and its local open/close window. Markets do not share a schedule,
so the table is the only place a schedule is stated.

Two honest limits are built in:

* only weekdays are known; no holiday calendar is claimed, so a public holiday
  still reads as ``OPEN`` inside its window;
* an exchange with no declared session is ``UNKNOWN`` and never blocks a cycle.

Scheduled discovery skips a cycle only when a declared session exists **and**
every covered exchange is ``CLOSED``. A manual or forced cycle always runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Iterable
from zoneinfo import ZoneInfo


class SessionState(StrEnum):
    """What is known about an exchange at one instant."""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


#: exchange MIC -> (IANA timezone, local open, local close).
SESSION_TABLE: dict[str, tuple[str, time, time]] = {
    "XNSE": ("Asia/Kolkata", time(9, 15), time(15, 30)),
    "XBOM": ("Asia/Kolkata", time(9, 15), time(15, 30)),
    "XNYS": ("America/New_York", time(9, 30), time(16, 0)),
    "XNAS": ("America/New_York", time(9, 30), time(16, 0)),
    "XASE": ("America/New_York", time(9, 30), time(16, 0)),
    "XLON": ("Europe/London", time(8, 0), time(16, 30)),
    "XTKS": ("Asia/Tokyo", time(9, 0), time(15, 0)),
}


def session_state(exchange: str, at: datetime) -> SessionState:
    """The declared state of one exchange at ``at`` (weekdays only)."""
    row = SESSION_TABLE.get(str(exchange or "").strip().upper())
    if row is None:
        return SessionState.UNKNOWN
    zone, opens_at, closes_at = row
    moment = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    local = moment.astimezone(ZoneInfo(zone))
    if local.weekday() >= 5:
        return SessionState.CLOSED
    if opens_at <= local.time() <= closes_at:
        return SessionState.OPEN
    return SessionState.CLOSED


def sessions_for(exchanges: Iterable[str], at: datetime) -> dict[str, str]:
    """State per exchange, keyed by upper-cased MIC, skipping blank names."""
    states: dict[str, str] = {}
    for exchange in exchanges:
        label = str(exchange or "").strip().upper()
        if label:
            states[label] = session_state(label, at).value
    return states


__all__ = ["SESSION_TABLE", "SessionState", "session_state", "sessions_for"]
