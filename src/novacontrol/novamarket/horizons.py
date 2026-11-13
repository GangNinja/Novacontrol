"""The three investment horizons, kept distinguishable by construction.

The mandate requires day trading, swing trading and long-term investing to
remain separable in their strategies, risk constraints, execution assumptions
and accounting. NM-01 implements that requirement as three *profiles* rather
than three branches in later code:

* each horizon has its own holding window, overnight rule, position limit,
  stop-loss fraction and rebalance cadence — so a later engine asks the profile
  instead of hard-coding "swing means a few days";
* each horizon has its own ``ledger_namespace``, so the three can never share a
  ledger by accident (NM-03's wallet and NM-07's portfolios key on it);
* the profiles are validated together at import, so a "distinction" that is
  only a different label fails the tests rather than shipping.

Nothing here trades, forecasts or sizes a position. It states the constraints a
future phase must honour and gives NM-01's contracts something real to validate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum


class InvestmentHorizon(StrEnum):
    """The three horizons the portfolio manager will keep separate."""

    DAY_TRADING = "day_trading"
    SWING_TRADING = "swing_trading"
    LONG_TERM = "long_term"


#: Owner decision 1: portfolio storage keys stay NM-07-compatible while every
#: consumer names the trading style on the right.  Do not add a second mapping.
PORTFOLIO_HORIZON_TO_TRADING_STYLE: dict[str, str] = {
    "short_term": InvestmentHorizon.DAY_TRADING.value,
    "medium_term": InvestmentHorizon.SWING_TRADING.value,
    "long_term": InvestmentHorizon.LONG_TERM.value,
}

PORTFOLIO_HORIZON_BY_TRADING_STYLE: dict[str, str] = {
    trading_style: portfolio_horizon
    for portfolio_horizon, trading_style in PORTFOLIO_HORIZON_TO_TRADING_STYLE.items()
}


def trading_style_for_portfolio_horizon(value: str) -> str:
    """Translate a persisted portfolio horizon to its approved trading style."""

    try:
        return PORTFOLIO_HORIZON_TO_TRADING_STYLE[str(value)]
    except KeyError as exc:
        known = ", ".join(PORTFOLIO_HORIZON_TO_TRADING_STYLE)
        raise ValueError(
            f"unknown portfolio horizon {value!r}; known: {known}"
        ) from exc


def portfolio_horizon_for_trading_style(value: InvestmentHorizon | str) -> str:
    """Translate an approved trading style back to the portfolio storage key."""

    try:
        style = InvestmentHorizon(value).value
    except ValueError as exc:
        known = ", ".join(item.value for item in InvestmentHorizon)
        raise ValueError(f"unknown trading style {value!r}; known: {known}") from exc
    return PORTFOLIO_HORIZON_BY_TRADING_STYLE[style]


@dataclass(frozen=True, slots=True)
class HorizonProfile:
    """What makes one horizon materially different from the other two."""

    horizon: InvestmentHorizon
    label: str
    description: str
    max_holding_period: timedelta
    allows_overnight: bool
    max_position_fraction: Decimal
    stop_loss_fraction: Decimal
    rebalance_cadence: str
    ledger_namespace: str

    def __post_init__(self) -> None:
        if self.max_holding_period <= timedelta(0):
            raise ValueError(f"{self.horizon.value}: holding period must be positive")
        for name, fraction in (
            ("max_position_fraction", self.max_position_fraction),
            ("stop_loss_fraction", self.stop_loss_fraction),
        ):
            if not isinstance(fraction, Decimal):
                raise TypeError(f"{self.horizon.value}: {name} must be a Decimal")
            if not Decimal("0") < fraction <= Decimal("1"):
                raise ValueError(
                    f"{self.horizon.value}: {name} must be in (0, 1], got {fraction}"
                )
        if not self.ledger_namespace.strip():
            raise ValueError(f"{self.horizon.value}: a ledger namespace is required")
        if not self.rebalance_cadence.strip():
            raise ValueError(f"{self.horizon.value}: a rebalance cadence is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "horizon": self.horizon.value,
            "portfolio_horizon": portfolio_horizon_for_trading_style(self.horizon),
            "label": self.label,
            "description": self.description,
            "max_holding_seconds": int(self.max_holding_period.total_seconds()),
            "allows_overnight": self.allows_overnight,
            "max_position_fraction": format(self.max_position_fraction, "f"),
            "stop_loss_fraction": format(self.stop_loss_fraction, "f"),
            "rebalance_cadence": self.rebalance_cadence,
            "ledger_namespace": self.ledger_namespace,
        }


#: The profiles, in shortest-to-longest order. The numbers are starting
#: constraints, not a strategy: NM-08's independent risk engine may tighten them,
#: and NM-09's strategies must read them rather than restate them.
_PROFILES: tuple[HorizonProfile, ...] = (
    HorizonProfile(
        horizon=InvestmentHorizon.DAY_TRADING,
        label="Day trading",
        description=(
            "Intraday positions closed before the session ends; no overnight "
            "exposure, the tightest position and stop limits, intraday cadence."
        ),
        max_holding_period=timedelta(hours=23),
        allows_overnight=False,
        max_position_fraction=Decimal("0.10"),
        stop_loss_fraction=Decimal("0.01"),
        rebalance_cadence="intraday",
        ledger_namespace="portfolio.day",
    ),
    HorizonProfile(
        horizon=InvestmentHorizon.SWING_TRADING,
        label="Swing trading",
        description=(
            "Multi-day to multi-week positions that may be held overnight, with "
            "a wider stop and a daily review cadence."
        ),
        max_holding_period=timedelta(days=30),
        allows_overnight=True,
        max_position_fraction=Decimal("0.20"),
        stop_loss_fraction=Decimal("0.05"),
        rebalance_cadence="daily",
        ledger_namespace="portfolio.swing",
    ),
    HorizonProfile(
        horizon=InvestmentHorizon.LONG_TERM,
        label="Long-term investing",
        description=(
            "Positions held for months to years with the widest holding window, "
            "a quarterly review cadence and its own ledger."
        ),
        max_holding_period=timedelta(days=3650),
        allows_overnight=True,
        max_position_fraction=Decimal("0.35"),
        stop_loss_fraction=Decimal("0.15"),
        rebalance_cadence="quarterly",
        ledger_namespace="portfolio.long_term",
    ),
)

HORIZON_PROFILES: Mapping[InvestmentHorizon, HorizonProfile] = {
    profile.horizon: profile for profile in _PROFILES
}


def profile_for(horizon: InvestmentHorizon | str) -> HorizonProfile:
    """The profile for a horizon, refusing an unknown one by name."""
    try:
        wanted = InvestmentHorizon(horizon)
    except ValueError as exc:
        known = ", ".join(item.value for item in InvestmentHorizon)
        raise ValueError(f"unknown investment horizon {horizon!r}; known: {known}") from exc
    return HORIZON_PROFILES[wanted]


def horizon_profiles() -> tuple[HorizonProfile, ...]:
    """Every profile in roadmap order (day → swing → long-term)."""
    return _PROFILES


def validate_horizon_profiles() -> tuple[str, ...]:
    """Structural problems with the three profiles, or ``()`` when distinct.

    Distinctness is checked as data: all three horizons exist, no ledger
    namespace is shared, the holding windows strictly increase, the day horizon
    forbids overnight exposure, and the review cadences differ. A future edit
    that turns one horizon into a copy of another fails these checks.
    """
    problems: list[str] = []
    declared = [profile.horizon for profile in _PROFILES]
    if len(_PROFILES) != len(InvestmentHorizon):
        problems.append("a profile is missing for one of the three horizons")
    if set(declared) != set(InvestmentHorizon) or len(declared) != len(set(declared)):
        # A duplicate plus a missing horizon can keep the COUNT at three while
        # silently dropping one, so the composition is checked, not the length.
        problems.append("exactly one profile is required for each of the three horizons")

    namespaces: dict[str, str] = {}
    for profile in _PROFILES:
        if profile.ledger_namespace in namespaces:
            problems.append(
                f"{profile.horizon.value} shares ledger namespace "
                f"{profile.ledger_namespace!r} with {namespaces[profile.ledger_namespace]}"
            )
        namespaces[profile.ledger_namespace] = profile.horizon.value

    ordered = [profile.max_holding_period for profile in _PROFILES]
    if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
        problems.append("holding windows must strictly increase day → swing → long term")

    day = HORIZON_PROFILES[InvestmentHorizon.DAY_TRADING]
    if day.allows_overnight:
        problems.append("day trading must not allow overnight exposure")
    for horizon in (InvestmentHorizon.SWING_TRADING, InvestmentHorizon.LONG_TERM):
        if not HORIZON_PROFILES[horizon].allows_overnight:
            problems.append(f"{horizon.value} must permit overnight positions")

    cadences = {profile.rebalance_cadence for profile in _PROFILES}
    if len(cadences) != len(_PROFILES):
        problems.append("each horizon needs its own rebalance cadence")

    return tuple(problems)


__all__ = [
    "HORIZON_PROFILES",
    "HorizonProfile",
    "InvestmentHorizon",
    "PORTFOLIO_HORIZON_BY_TRADING_STYLE",
    "PORTFOLIO_HORIZON_TO_TRADING_STYLE",
    "horizon_profiles",
    "portfolio_horizon_for_trading_style",
    "trading_style_for_portfolio_horizon",
    "profile_for",
    "validate_horizon_profiles",
]
