"""Portfolio valuation and performance arithmetic (NM-07).

Money is :class:`Decimal` throughout; a valuation is a statement about stored
positions and one timestamped price per instrument. Nothing here extrapolates a
price it does not have: a position without a price is valued at cost and reported
as stale.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from novacontrol.novamarket.portfolio.models import (
    PAISE,
    PortfolioRecord,
    Position,
    money_text,
)

ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class Valuation:
    cash: Decimal
    invested: Decimal
    total: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    cost_basis: Decimal
    return_pct: Decimal
    drawdown_pct: Decimal
    cash_weight: Decimal
    priced_positions: int
    stale_positions: int
    holdings: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash": money_text(self.cash),
            "invested": money_text(self.invested),
            "cost_basis": money_text(self.cost_basis),
            "total_value": money_text(self.total),
            "realized_pnl": money_text(self.realized_pnl),
            "unrealized_pnl": money_text(self.unrealized_pnl),
            "total_pnl": money_text(self.realized_pnl + self.unrealized_pnl),
            "return_pct": money_text(self.return_pct),
            "drawdown_pct": money_text(self.drawdown_pct),
            "cash_weight": money_text(self.cash_weight),
            "cash_weight_pct": money_text(self.cash_weight * HUNDRED),
            "priced_positions": self.priced_positions,
            "stale_positions": self.stale_positions,
            "holdings": self.holdings,
        }


def value_portfolio(
    record: PortfolioRecord,
    positions: Sequence[Position],
    cash: Decimal,
    *,
    history_totals: Sequence[Decimal] = (),
    stale_after_hours: float | None = None,
    at: datetime | None = None,
) -> Valuation:
    """Value open positions at their stored price; cost basis when unpriced."""
    invested = ZERO
    cost_basis = ZERO
    unrealized = ZERO
    priced = 0
    stale = 0
    for position in positions:
        cost_basis += position.cost_basis
        value = position.market_value()
        if value is None:
            stale += 1
            invested += position.cost_basis
            continue
        priced += 1
        invested += value
        unrealized += value - position.cost_basis
        if (
            stale_after_hours is not None
            and at is not None
            and position.priced_at is not None
            and (at - position.priced_at).total_seconds() / 3600.0 > stale_after_hours
        ):
            stale += 1
    invested = invested.quantize(PAISE)
    unrealized = unrealized.quantize(PAISE)
    cash = cash.quantize(PAISE)
    total = (cash + invested).quantize(PAISE)
    initial = record.initial_capital
    return_pct = (
        ((total - initial) / initial * HUNDRED).quantize(PAISE) if initial > 0 else ZERO
    )
    peak = max([initial, total, *[value for value in history_totals]])
    drawdown = (
        ((total - peak) / peak * HUNDRED).quantize(PAISE) if peak > 0 else ZERO
    )
    cash_weight = (cash / total).quantize(Decimal("0.0001")) if total > 0 else ZERO
    return Valuation(
        cash=cash,
        invested=invested,
        total=total,
        realized_pnl=record.realized_pnl,
        unrealized_pnl=unrealized,
        cost_basis=cost_basis.quantize(PAISE),
        return_pct=return_pct,
        drawdown_pct=drawdown,
        cash_weight=cash_weight,
        priced_positions=priced,
        stale_positions=stale,
        holdings=len(positions),
    )


def spacing_hours(timestamps: Sequence[datetime]) -> float | None:
    """Median spacing between valuation timestamps, or None for one snapshot."""
    if len(timestamps) < 2:
        return None
    ordered = sorted(timestamps)
    gaps = [
        (later - earlier).total_seconds() / 3600.0
        for earlier, later in zip(ordered, ordered[1:])
        if later > earlier
    ]
    if not gaps:
        return None
    return statistics.median(gaps)


def performance(
    record: PortfolioRecord,
    *,
    valuation: Valuation,
    totals: Sequence[Decimal],
    timestamps: Sequence[datetime],
    turnover_notional: Decimal,
    mean_total: Decimal,
    policy_minimum: int,
) -> dict[str, Any]:
    """Measured performance, with every ratio gated on enough observations.

    Assumptions, stated once and repeated in the payload: the risk-free rate is
    zero (this build has no risk-free series), snapshots are equally spaced at
    their median interval, and annualisation uses the resulting period count.
    """
    observations = len(totals)
    absolute = (valuation.total - record.initial_capital).quantize(PAISE)
    percentage = valuation.return_pct
    ordered = sorted(zip(timestamps, totals), key=lambda pair: pair[0])
    values = [float(total) for _at, total in ordered]
    returns = [
        (values[index] / values[index - 1]) - 1.0
        for index in range(1, len(values))
        if values[index - 1] > 0
    ]
    peak = float(record.initial_capital)
    maximum_drawdown = 0.0
    for value in values or [float(valuation.total)]:
        peak = max(peak, value)
        if peak > 0:
            maximum_drawdown = min(maximum_drawdown, (value - peak) / peak)

    periods_per_year: float | None = None
    hours = spacing_hours(timestamps)
    if hours and hours > 0:
        periods_per_year = min(24.0 * 365.0 / hours, 252.0)

    metrics: dict[str, Any] = {
        "observations": observations,
        "absolute_return": money_text(absolute),
        "return_pct": money_text(percentage),
        "maximum_drawdown_pct": money_text(
            (Decimal(str(maximum_drawdown)) * HUNDRED).quantize(PAISE)
        ),
        "volatility_pct": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "risk_adjusted_reason": "",
        "annualisation_periods": (
            round(periods_per_year, 2) if periods_per_year else None
        ),
        "assumptions": [
            "risk_free_rate=0 (no risk-free series exists in this build)",
            "snapshots treated as equally spaced at their median interval",
            "ratios require at least the policy's minimum observations",
        ],
    }
    if observations < policy_minimum:
        metrics["risk_adjusted_reason"] = (
            f"{observations} valuation(s); at least {policy_minimum} are required "
            "before a risk-adjusted ratio is reported"
        )
        return metrics
    if len(returns) < 2 or periods_per_year is None:
        metrics["risk_adjusted_reason"] = (
            "not enough spaced return observations for a risk-adjusted ratio"
        )
        return metrics
    mean = statistics.fmean(returns)
    deviation = statistics.pstdev(returns)
    annualiser = math.sqrt(periods_per_year)
    metrics["volatility_pct"] = round(
        deviation * annualiser * 100.0, 6
    )
    if deviation > 0:
        metrics["sharpe_ratio"] = round(mean / deviation * annualiser, 6)
    downside = [value for value in returns if value < 0]
    if downside:
        downside_deviation = math.sqrt(
            statistics.fmean(value**2 for value in downside)
        )
        if downside_deviation > 0:
            metrics["sortino_ratio"] = round(
                mean / downside_deviation * annualiser, 6
            )
    if metrics["sharpe_ratio"] is None:
        metrics["risk_adjusted_reason"] = "zero return deviation; no ratio is defined"

    turnover = Decimal("0.00")
    if mean_total > 0:
        turnover = (turnover_notional / mean_total).quantize(Decimal("0.0001"))
    metrics["turnover"] = money_text(turnover)
    metrics["turnover_notional"] = money_text(turnover_notional)
    metrics["turnover_basis"] = (
        "cumulative traded notional (buys and sells) divided by mean portfolio value"
    )
    # Cash earns nothing in this simulation: the drag is an opportunity-cost
    # estimate at the portfolio's own return, not a claim about a money market.
    metrics["cash_drag_estimate_pct"] = money_text(
        (valuation.cash_weight * percentage).quantize(PAISE)
    )
    metrics["cash_drag_basis"] = (
        "cash_weight × portfolio return; cash has no simulated yield"
    )
    return metrics


__all__ = ["Valuation", "performance", "spacing_hours", "value_portfolio"]
