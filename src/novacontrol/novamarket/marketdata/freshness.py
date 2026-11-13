"""Freshness: is this observation current, expected, stale, or unknowable?

One universal freshness window is wrong in both directions. It calls Friday's
close invalid on Saturday, and it presents a holiday's last price as a live
quote. This module answers with the exchange's own session state plus the
interval and asset class:

* **FRESH** — a live quote, or a bar whose period has completed and is current.
* **DELAYED** — older than a live quote but expected: a provider's documented
  delay, the previous completed session during today's trading, or the last
  completed daily bar while a new session is open.
* **CLOSED_MARKET** — the exchange is shut and this is its last completed
  session's observation. It is usable for valuation and is NOT a live quote.
* **STALE** — old enough that using it as current would be misleading.
* **MISSING / PROVIDER_UNAVAILABLE** — no observation exists; nothing is
  invented (the engine sets these).

The result carries the reasons and the calendar coverage flag, so a caller can
tell "the market is closed" from "I do not have a verified calendar for this
exchange". A price is never relabelled FRESH because the cache returned it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from novacontrol.novamarket.contracts import ConfigurationError
from novacontrol.novamarket.marketdata.calendar import (
    SessionState,
    calendar_for,
)
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    Freshness,
    Instrument,
    OhlcvBar,
    PriceObservation,
)


@dataclass(frozen=True, slots=True)
class FreshnessSettings:
    """The configurable lines between live, delayed and stale.

    The defaults are deliberately conservative for equities and looser for
    daily/weekly data: a daily bar is not stale because it is a day old, and a
    spot quote is not fresh because it is only an hour old. All values are
    validated, so a hand-edited configuration cannot ask for a negative window.
    """

    spot_fresh_seconds: int = 120
    spot_delayed_seconds: int = 900
    intraday_fresh_bars: int = 1
    intraday_delayed_bars: int = 3
    intraday_stale_bars: int = 10
    daily_delayed_sessions: int = 2
    daily_stale_sessions: int = 5
    weekly_stale_days: int = 21
    monthly_stale_days: int = 90
    closed_market_max_age_days: int = 7
    provider_delay_grace_seconds: int = 60

    LIMITS: Mapping[str, tuple[int, int]] = field(
        default_factory=lambda: {
            "spot_fresh_seconds": (1, 86_400),
            "spot_delayed_seconds": (1, 604_800),
            "intraday_fresh_bars": (1, 100),
            "intraday_delayed_bars": (1, 1_000),
            "intraday_stale_bars": (1, 10_000),
            "daily_delayed_sessions": (1, 100),
            "daily_stale_sessions": (1, 1_000),
            "weekly_stale_days": (1, 3_650),
            "monthly_stale_days": (1, 3_650),
            "closed_market_max_age_days": (1, 365),
            "provider_delay_grace_seconds": (0, 3_600),
        },
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name, (low, high) in self.LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"freshness.{name} must be an integer")
            if not low <= value <= high:
                raise ConfigurationError(
                    f"freshness.{name} must be between {low} and {high}, got {value}"
                )
        if self.intraday_delayed_bars < self.intraday_fresh_bars:
            raise ConfigurationError(
                "freshness.intraday_delayed_bars must be >= intraday_fresh_bars"
            )
        if self.intraday_stale_bars < self.intraday_delayed_bars:
            raise ConfigurationError(
                "freshness.intraday_stale_bars must be >= intraday_delayed_bars"
            )
        if self.daily_stale_sessions < self.daily_delayed_sessions:
            raise ConfigurationError(
                "freshness.daily_stale_sessions must be >= daily_delayed_sessions"
            )
        if self.spot_delayed_seconds < self.spot_fresh_seconds:
            raise ConfigurationError(
                "freshness.spot_delayed_seconds must be >= spot_fresh_seconds"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> FreshnessSettings:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigurationError("freshness must be an object")
        defaults = cls()
        allowed = set(defaults.LIMITS)
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigurationError(
                f"unknown freshness field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        return cls(
            **{
                name: data.get(name, getattr(defaults, name))
                for name in allowed
            }
        )

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.LIMITS}

    @staticmethod
    def schema() -> dict[str, Any]:
        defaults = FreshnessSettings()
        return {
            "fields": {
                name: {"min": low, "max": high}
                for name, (low, high) in defaults.LIMITS.items()
            },
            "rules": (
                "Spot quotes are FRESH within spot_fresh_seconds, DELAYED within "
                "spot_delayed_seconds, STALE after. Intraday bars use bar counts; "
                "daily bars use completed sessions; weekly/monthly use age. A "
                "closed market classifies its last completed session as "
                "CLOSED_MARKET, not STALE, while it is younger than "
                "closed_market_max_age_days."
            ),
        }


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """A freshness verdict with the evidence behind it."""

    freshness: Freshness
    reasons: tuple[str, ...]
    age_seconds: float | None
    market_state: SessionState
    calendar_coverage: bool | None
    expected_delay_seconds: int | None = None
    last_completed_session: date | None = None

    @property
    def live(self) -> bool:
        return self.freshness is Freshness.FRESH

    def to_dict(self) -> dict[str, Any]:
        return {
            "freshness": self.freshness.value,
            "live": self.live,
            "reasons": list(self.reasons),
            "age_seconds": (
                round(self.age_seconds, 3) if self.age_seconds is not None else None
            ),
            "market_state": self.market_state.value,
            "calendar_coverage": self.calendar_coverage,
            "expected_delay_seconds": self.expected_delay_seconds,
            "last_completed_session": (
                self.last_completed_session.isoformat()
                if self.last_completed_session
                else None
            ),
        }


def _check_now(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("freshness needs a timezone-aware 'now'")
    return now


def _with_reasons(
    freshness: Freshness,
    reasons: list[str],
    *,
    age_seconds: float | None,
    market_state: SessionState,
    coverage: bool | None,
    delay: int | None,
    last_session: date | None,
) -> FreshnessAssessment:
    return FreshnessAssessment(
        freshness=freshness,
        reasons=tuple(reasons),
        age_seconds=age_seconds,
        market_state=market_state,
        calendar_coverage=coverage,
        expected_delay_seconds=delay,
        last_completed_session=last_session,
    )


def _age_based(
    age_seconds: float,
    settings: FreshnessSettings,
    delay_seconds: int,
    reasons: list[str],
    *,
    market_state: SessionState,
    coverage: bool | None,
    last_session: date | None,
) -> FreshnessAssessment:
    grace = delay_seconds + settings.provider_delay_grace_seconds
    if age_seconds <= settings.spot_fresh_seconds + grace:
        reasons.append("within the fresh window")
        return _with_reasons(
            Freshness.FRESH,
            reasons,
            age_seconds=age_seconds,
            market_state=market_state,
            coverage=coverage,
            delay=delay_seconds,
            last_session=last_session,
        )
    if age_seconds <= settings.spot_delayed_seconds + grace:
        reasons.append("older than a live quote but within the delayed window")
        return _with_reasons(
            Freshness.DELAYED,
            reasons,
            age_seconds=age_seconds,
            market_state=market_state,
            coverage=coverage,
            delay=delay_seconds,
            last_session=last_session,
        )
    reasons.append("older than the stale threshold")
    return _with_reasons(
        Freshness.STALE,
        reasons,
        age_seconds=age_seconds,
        market_state=market_state,
        coverage=coverage,
        delay=delay_seconds,
        last_session=last_session,
    )


def assess_quote(
    instrument: Instrument,
    observation: PriceObservation,
    *,
    now: datetime,
    settings: FreshnessSettings | None = None,
    provider_delay_seconds: int | None = None,
) -> FreshnessAssessment:
    """Classify a spot quote against its exchange's real session state."""
    moment = _check_now(now)
    policy = settings or FreshnessSettings()
    age_seconds = max((moment - observation.observed_at).total_seconds(), 0.0)
    calendar = calendar_for(instrument)
    reasons: list[str] = []
    delay = provider_delay_seconds
    if delay is None:
        delay = 0

    if calendar is None:
        reasons.append(
            f"no verified calendar for {instrument.exchange!r}; freshness uses "
            "age only"
        )
        return _age_based(
            age_seconds,
            policy,
            delay,
            reasons,
            market_state=SessionState.UNKNOWN,
            coverage=None,
            last_session=None,
        )

    state = calendar.state(moment)
    coverage = calendar.coverage(moment)
    last_close = calendar.last_session_close(moment)
    last_session = last_close.date() if last_close else None
    if not coverage:
        reasons.append(
            f"holiday coverage for {moment.year} is not verified; session times "
            "and weekdays were used"
        )

    if state in (SessionState.OPEN, SessionState.CONTINUOUS):
        if observation.observed_at > moment + timedelta(seconds=5):
            reasons.append("observation timestamp is ahead of the clock")
        return _age_based(
            age_seconds,
            policy,
            delay,
            reasons,
            market_state=state,
            coverage=coverage,
            last_session=last_session,
        )

    # The exchange is shut. Its last completed session's close is the expected
    # observation; a print from an even older session is stale, not "closed".
    if last_close is not None and last_session is not None:
        observed_day = calendar.local(observation.observed_at).date()
        closed_age = (moment - last_close).total_seconds()
        within_grace = (
            closed_age
            <= policy.closed_market_max_age_days * 86_400
        )
        if observed_day == last_session and within_grace:
            reasons.append(
                f"{state.value}: this is the last completed session's price, "
                "not a live quote"
            )
            return _with_reasons(
                Freshness.CLOSED_MARKET,
                reasons,
                age_seconds=age_seconds,
                market_state=state,
                coverage=coverage,
                delay=delay,
                last_session=last_session,
            )
        if observed_day == last_session and not within_grace:
            reasons.append(
                f"last completed session is older than "
                f"{policy.closed_market_max_age_days} days"
            )
            return _with_reasons(
                Freshness.STALE,
                reasons,
                age_seconds=age_seconds,
                market_state=state,
                coverage=coverage,
                delay=delay,
                last_session=last_session,
            )

    return _age_based(
        age_seconds,
        policy,
        delay,
        reasons,
        market_state=state,
        coverage=coverage,
        last_session=last_session,
    )


def assess_bar(
    instrument: Instrument,
    bar: OhlcvBar,
    *,
    now: datetime,
    settings: FreshnessSettings | None = None,
    provider_delay_seconds: int | None = None,
) -> FreshnessAssessment:
    """Classify an OHLCV bar by interval, session state and completion."""
    moment = _check_now(now)
    policy = settings or FreshnessSettings()
    age_seconds = max((moment - bar.observed_at).total_seconds(), 0.0)
    calendar = calendar_for(instrument)
    reasons: list[str] = []
    delay = provider_delay_seconds or 0

    if bar.partial:
        reasons.append("bar is partial; its period has not completed")

    if calendar is None:
        reasons.append(
            f"no verified calendar for {instrument.exchange!r}; freshness uses "
            "age only"
        )
        freshness = _bar_age_freshness(bar.interval, age_seconds, policy, reasons)
        return _with_reasons(
            freshness,
            reasons,
            age_seconds=age_seconds,
            market_state=SessionState.UNKNOWN,
            coverage=None,
            delay=delay,
            last_session=None,
        )

    state = calendar.state(moment)
    coverage = calendar.coverage(moment)
    last_close = calendar.last_session_close(moment)
    last_session = last_close.date() if last_close else None
    if not coverage:
        reasons.append(
            f"holiday coverage for {moment.year} is not verified; session times "
            "and weekdays were used"
        )

    if bar.interval is DataInterval.DAY_1 and last_close is not None:
        observed_day = calendar.local(bar.observed_at).date()
        if observed_day == last_session:
            if state in (SessionState.OPEN,):
                reasons.append(
                    "the latest completed daily bar; today's session is still open"
                )
                freshness = Freshness.DELAYED
            elif state in (
                SessionState.CLOSED,
                SessionState.HOLIDAY,
                SessionState.WEEKEND,
            ):
                reasons.append(
                    "the latest completed session's daily bar (market is closed)"
                )
                freshness = Freshness.CLOSED_MARKET
            else:
                reasons.append("the previous completed session's daily bar")
                freshness = Freshness.DELAYED
            return _with_reasons(
                freshness,
                reasons,
                age_seconds=age_seconds,
                market_state=state,
                coverage=coverage,
                delay=delay,
                last_session=last_session,
            )
        if last_session is not None and observed_day < last_session:
            missed = calendar.sessions_between(
                bar.observed_at, last_close
            )
            if missed <= policy.daily_delayed_sessions:
                reasons.append(
                    f"{missed} completed session(s) newer than this daily bar"
                )
                freshness = Freshness.DELAYED
            else:
                reasons.append(
                    f"{missed} completed sessions newer than this daily bar"
                )
                freshness = Freshness.STALE
            return _with_reasons(
                freshness,
                reasons,
                age_seconds=age_seconds,
                market_state=state,
                coverage=coverage,
                delay=delay,
                last_session=last_session,
            )

    if bar.interval.is_intraday:
        if state in (
            SessionState.CLOSED,
            SessionState.HOLIDAY,
            SessionState.WEEKEND,
        ) and last_close is not None:
            observed_day = calendar.local(bar.observed_at).date()
            if observed_day == last_session and (
                moment - last_close
            ).total_seconds() <= policy.closed_market_max_age_days * 86_400:
                reasons.append(
                    "the last completed session's bar; the market is closed"
                )
                return _with_reasons(
                    Freshness.CLOSED_MARKET,
                    reasons,
                    age_seconds=age_seconds,
                    market_state=state,
                    coverage=coverage,
                    delay=delay,
                    last_session=last_session,
                )
        interval_seconds = _interval_seconds(bar.interval)
        bars_behind = age_seconds / interval_seconds if interval_seconds else 0.0
        if bars_behind <= policy.intraday_fresh_bars:
            reasons.append("within the fresh intraday bar window")
            freshness = Freshness.FRESH
        elif bars_behind <= policy.intraday_delayed_bars:
            reasons.append(
                f"{bars_behind:.1f} bars behind; within the delayed window"
            )
            freshness = Freshness.DELAYED
        else:
            reasons.append(
                f"{bars_behind:.1f} bars behind; beyond the stale threshold"
            )
            freshness = Freshness.STALE
        return _with_reasons(
            freshness,
            reasons,
            age_seconds=age_seconds,
            market_state=state,
            coverage=coverage,
            delay=delay,
            last_session=last_session,
        )

    freshness = _bar_age_freshness(bar.interval, age_seconds, policy, reasons)
    return _with_reasons(
        freshness,
        reasons,
        age_seconds=age_seconds,
        market_state=state,
        coverage=coverage,
        delay=delay,
        last_session=last_session,
    )


def _interval_seconds(interval: DataInterval) -> int:
    return {
        DataInterval.MINUTE_1: 60,
        DataInterval.MINUTE_5: 300,
        DataInterval.MINUTE_15: 900,
        DataInterval.MINUTE_30: 1_800,
        DataInterval.HOUR_1: 3_600,
        DataInterval.DAY_1: 86_400,
        DataInterval.WEEK_1: 604_800,
        DataInterval.MONTH_1: 2_592_000,
    }.get(interval, 0)


def _bar_age_freshness(
    interval: DataInterval,
    age_seconds: float,
    policy: FreshnessSettings,
    reasons: list[str],
) -> Freshness:
    if interval is DataInterval.WEEK_1:
        stale = policy.weekly_stale_days * 86_400
        if age_seconds <= stale / 3:
            reasons.append("within the fresh weekly window")
            return Freshness.FRESH
        if age_seconds <= stale:
            reasons.append("within the delayed weekly window")
            return Freshness.DELAYED
        reasons.append("older than the weekly stale threshold")
        return Freshness.STALE
    if interval is DataInterval.MONTH_1:
        stale = policy.monthly_stale_days * 86_400
        if age_seconds <= stale / 3:
            reasons.append("within the fresh monthly window")
            return Freshness.FRESH
        if age_seconds <= stale:
            reasons.append("within the delayed monthly window")
            return Freshness.DELAYED
        reasons.append("older than the monthly stale threshold")
        return Freshness.STALE
    interval_seconds = _interval_seconds(interval) or 86_400
    bars_behind = age_seconds / interval_seconds
    if bars_behind <= policy.intraday_fresh_bars:
        reasons.append("within the fresh bar window")
        return Freshness.FRESH
    if bars_behind <= policy.intraday_delayed_bars:
        reasons.append("within the delayed bar window")
        return Freshness.DELAYED
    reasons.append("beyond the stale bar threshold")
    return Freshness.STALE


def missing_assessment(
    instrument: Instrument,
    *,
    now: datetime,
    settings: FreshnessSettings | None = None,
    provider_unavailable: bool = False,
) -> FreshnessAssessment:
    """The verdict when there is no observation at all — never a fake price."""
    moment = _check_now(now)
    policy = settings or FreshnessSettings()
    calendar = calendar_for(instrument)
    state = calendar.state(moment) if calendar else SessionState.UNKNOWN
    coverage = calendar.coverage(moment) if calendar else None
    last_close = calendar.last_session_close(moment) if calendar else None
    if provider_unavailable:
        reasons = ["no provider could supply validated data"]
    else:
        reasons = ["no observation is stored for this instrument"]
        if state not in (SessionState.OPEN, SessionState.CONTINUOUS):
            # "Nothing stored" and "the exchange is shut, so nothing live could
            # arrive" are different answers: the second one is expected, and a
            # consumer must be able to tell it from a provider outage.
            exchange = instrument.exchange or instrument.mic
            reasons.append(
                f"the {exchange} market is {state.value.replace('_', ' ')}; "
                "no live quote is collected while it is shut"
            )
    return _with_reasons(
        Freshness.PROVIDER_UNAVAILABLE
        if provider_unavailable
        else Freshness.MISSING,
        reasons,
        age_seconds=None,
        market_state=state,
        coverage=coverage,
        delay=None,
        last_session=last_close.date() if last_close else None,
    )


__all__ = [
    "FreshnessAssessment",
    "FreshnessSettings",
    "assess_bar",
    "assess_quote",
    "missing_assessment",
]
