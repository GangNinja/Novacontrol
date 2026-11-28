"""Deterministic one-close-per-session policy for daily bars.

The store's unique key includes the provider, so two providers can hold the
same instrument/session without a database conflict.  A daily SMA window must
still see exactly one close per exchange session.  The policy here is:

* a session is the bar's *local* trading date, derived from its recorded
  ``source_timezone`` (never from the raw UTC stamp);
* an official exchange bhavcopy row wins over a quote provider;
* otherwise the provider name, then the bar id, decides — never row order;
* conflicting values are left in the store and reported by the importer; this
  helper only chooses which one a consumer reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

#: Official end-of-day exchange files, most authoritative first.
OFFICIAL_DAILY_PROVIDERS: tuple[str, ...] = ("bse_bhavcopy", "nse_bhavcopy")


def _value(bar: Any, name: str, default: Any = None) -> Any:
    if isinstance(bar, Mapping):
        return bar.get(name, default)
    return getattr(bar, name, default)


def bar_session_date(bar: Any) -> date | None:
    """The exchange-local session date of one bar, or ``None`` if unknown."""

    observed = _value(bar, "observed_at")
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        return None
    zone_name = str(_value(bar, "source_timezone", "") or "UTC")
    try:
        zone = ZoneInfo(zone_name)
    except Exception:  # noqa: BLE001 - an unknown zone is read as UTC
        zone = ZoneInfo("UTC")
    return observed.astimezone(zone).date()


def _rank(bar: Any) -> tuple[int, str, str]:
    provider = str(_value(bar, "provider", "") or "").lower()
    if provider in OFFICIAL_DAILY_PROVIDERS:
        return (OFFICIAL_DAILY_PROVIDERS.index(provider), provider, "")
    return (len(OFFICIAL_DAILY_PROVIDERS), provider, str(_value(bar, "bar_id", "") or ""))


def _epoch(bar: Any) -> float:
    observed = _value(bar, "observed_at")
    return observed.timestamp() if isinstance(observed, datetime) else float("-inf")


def one_close_per_session(bars: Iterable[Any]) -> list[Any]:
    """One effective daily bar per session, chronological, deterministic."""

    chosen: dict[date, Any] = {}
    for bar in bars:
        day = bar_session_date(bar)
        if day is None:
            continue
        current = chosen.get(day)
        if current is None or _rank(bar) < _rank(current):
            chosen[day] = bar
    return sorted(chosen.values(), key=_epoch)


__all__ = ["OFFICIAL_DAILY_PROVIDERS", "bar_session_date", "one_close_per_session"]
