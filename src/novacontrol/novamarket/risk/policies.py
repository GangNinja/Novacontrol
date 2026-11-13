"""NM-08 policy evaluation: fixed order, explicit severity, enforceable outcome.

The evaluator is a pure function over a :class:`RiskSnapshot`. It never reads a
database and never decides what action to take; it reports breaches and the
engine turns them into one outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from novacontrol.novamarket.risk.models import (
    ConstraintSeverity,
    RiskCategory,
    RiskOutcome,
    RiskPolicy,
)

UNCERTAINTY_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True, slots=True)
class ConstraintBreach:
    code: str
    category: RiskCategory
    severity: ConstraintSeverity
    message: str
    actual: str = ""
    limit: str = ""
    instrument_id: str = ""
    sector: str = ""
    overridable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "actual": self.actual,
            "limit": self.limit,
            "instrument_id": self.instrument_id,
            "sector": self.sector,
            "overridable": self.overridable,
        }


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    positions: tuple[dict[str, Any], ...]
    projected: tuple[dict[str, Any], ...]
    cash_weight: Decimal
    invested_fraction: Decimal
    drawdown_pct: Decimal
    volatility_pct: Decimal | None
    turnover: Decimal
    data_age_hours: float | None
    state_age_hours: float | None
    aggregate_sector_weights: Mapping[str, Decimal] = field(default_factory=dict)
    liquidity: Decimal | None = None
    forecast_failures: tuple[str, ...] = ()
    event_pressure: tuple[str, ...] = ()
    insufficient: tuple[str, ...] = ()
    capital_request: Decimal | None = None
    available_cash: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    breaches: tuple[ConstraintBreach, ...]
    warnings: tuple[ConstraintBreach, ...]
    restrictions: tuple[str, ...]
    insufficient: tuple[str, ...]

    @property
    def outcome(self) -> RiskOutcome:
        if any(item.severity is ConstraintSeverity.HARD for item in self.breaches):
            return RiskOutcome.REJECTED
        if self.insufficient:
            return RiskOutcome.INSUFFICIENT_DATA
        if any(item.severity is ConstraintSeverity.REVIEW for item in self.breaches):
            return RiskOutcome.REQUIRES_REVIEW
        if any(item.severity is ConstraintSeverity.SOFT for item in self.breaches):
            return RiskOutcome.RESTRICTED
        return RiskOutcome.APPROVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "breaches": [item.to_dict() for item in self.breaches],
            "warnings": [item.to_dict() for item in self.warnings],
            "restrictions": list(self.restrictions),
            "insufficient": list(self.insufficient),
        }


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def evaluate_policy(
    policy: RiskPolicy,
    snapshot: RiskSnapshot,
    *,
    action: str,
    adds_exposure: bool,
) -> PolicyEvaluation:
    """Evaluate every limit in one documented order.

    Data availability is reported as ``insufficient`` (never an approval), hard
    limits reject, review-level conditions escalate, and soft limits restrict.
    """
    breaches: list[ConstraintBreach] = []
    warnings: list[ConstraintBreach] = []
    restrictions: list[str] = []
    insufficient: list[str] = list(snapshot.insufficient)

    # 1. Data / state freshness.
    if snapshot.data_age_hours is None:
        insufficient.append("no timestamped price observation is available")
    elif snapshot.data_age_hours > policy.max_data_age_hours:
        breaches.append(
            ConstraintBreach(
                code="stale_market_data",
                category=RiskCategory.DATA,
                severity=ConstraintSeverity.HARD,
                message=(
                    f"the newest price is {snapshot.data_age_hours:.1f}h old; the "
                    f"policy allows {policy.max_data_age_hours:.0f}h"
                ),
                actual=f"{snapshot.data_age_hours:.1f}h",
                limit=f"{policy.max_data_age_hours:.0f}h",
            )
        )
    if snapshot.state_age_hours is None:
        insufficient.append("the portfolio has no valuation timestamp")
    elif snapshot.state_age_hours > policy.max_state_age_hours:
        insufficient.append(
            f"portfolio state is {snapshot.state_age_hours:.1f}h old; the policy "
            f"requires {policy.max_state_age_hours:.0f}h or newer"
        )

    # 2. Restricted instruments are a hard, non-overridable rule.
    restricted = set(policy.restricted_instruments)
    for position in snapshot.projected:
        instrument_id = str(position.get("instrument_id") or "")
        if instrument_id in restricted and Decimal(str(position.get("weight") or "0")) > 0:
            breaches.append(
                ConstraintBreach(
                    code="restricted_instrument",
                    category=RiskCategory.CONCENTRATION,
                    severity=ConstraintSeverity.HARD,
                    message=f"{instrument_id} is restricted by this policy",
                    instrument_id=instrument_id,
                )
            )

    # 3. Position concentration.
    for position in snapshot.projected:
        weight = Decimal(str(position.get("weight") or "0"))
        if weight > policy.max_position_weight:
            breaches.append(
                ConstraintBreach(
                    code="max_position_weight",
                    category=RiskCategory.CONCENTRATION,
                    severity=ConstraintSeverity.HARD,
                    message=(
                        f"{position.get('instrument_id')} would be "
                        f"{_money(weight)} of the book; the limit is "
                        f"{_money(policy.max_position_weight)}"
                    ),
                    actual=_money(weight),
                    limit=_money(policy.max_position_weight),
                    instrument_id=str(position.get("instrument_id") or ""),
                )
            )

    # 4. Sector concentration, computed from the projected book.
    sectors: dict[str, Decimal] = {}
    for position in snapshot.projected:
        weight = Decimal(str(position.get("weight") or "0"))
        sector = str(position.get("sector") or "unknown")
        sectors[sector] = sectors.get(sector, Decimal("0")) + weight
    for sector, weight in sectors.items():
        if weight > policy.max_sector_weight:
            breaches.append(
                ConstraintBreach(
                    code="max_sector_weight",
                    category=RiskCategory.CONCENTRATION,
                    severity=ConstraintSeverity.HARD,
                    message=(
                        f"sector {sector} would be {_money(weight)} of the book; "
                        f"the limit is {_money(policy.max_sector_weight)}"
                    ),
                    actual=_money(weight),
                    limit=_money(policy.max_sector_weight),
                    sector=sector,
                )
            )

    # 5. Invested fraction and cash reserve.
    if snapshot.invested_fraction > policy.max_invested_fraction:
        breaches.append(
            ConstraintBreach(
                code="max_invested_fraction",
                category=RiskCategory.CAPITAL,
                severity=ConstraintSeverity.HARD,
                message=(
                    f"invested fraction {_money(snapshot.invested_fraction)} exceeds "
                    f"{_money(policy.max_invested_fraction)}"
                ),
                actual=_money(snapshot.invested_fraction),
                limit=_money(policy.max_invested_fraction),
            )
        )
    if snapshot.cash_weight < policy.min_cash_fraction:
        breaches.append(
            ConstraintBreach(
                code="min_cash_fraction",
                category=RiskCategory.CAPITAL,
                severity=ConstraintSeverity.HARD,
                message=(
                    f"cash would be {_money(snapshot.cash_weight)} of the book; the "
                    f"minimum is {_money(policy.min_cash_fraction)}"
                ),
                actual=_money(snapshot.cash_weight),
                limit=_money(policy.min_cash_fraction),
            )
        )

    # 5b. Capital allocation/reservation must be backed by actual cash.
    if snapshot.capital_request is not None:
        if snapshot.available_cash is None:
            insufficient.append("available cash could not be determined")
        elif snapshot.capital_request > snapshot.available_cash:
            breaches.append(
                ConstraintBreach(
                    code="insufficient_cash",
                    category=RiskCategory.CAPITAL,
                    severity=ConstraintSeverity.HARD,
                    message=(
                        f"requested capital {_money(snapshot.capital_request)} "
                        f"exceeds available cash {_money(snapshot.available_cash)}"
                    ),
                    actual=_money(snapshot.capital_request),
                    limit=_money(snapshot.available_cash),
                )
            )

    # 6. Drawdown is a review condition, not an automatic liquidation.
    if snapshot.drawdown_pct > policy.max_drawdown_pct:
        breaches.append(
            ConstraintBreach(
                code="max_drawdown",
                category=RiskCategory.MARKET,
                severity=ConstraintSeverity.REVIEW,
                message=(
                    f"drawdown {_money(snapshot.drawdown_pct)}% exceeds the "
                    f"{_money(policy.max_drawdown_pct)}% review threshold"
                ),
                actual=f"{_money(snapshot.drawdown_pct)}%",
                limit=f"{_money(policy.max_drawdown_pct)}%",
            )
        )

    # 7. Volatility restricts size rather than banning the book.
    if snapshot.volatility_pct is None:
        insufficient.append("annualised volatility could not be measured")
    elif snapshot.volatility_pct > policy.max_volatility_pct:
        breaches.append(
            ConstraintBreach(
                code="max_volatility",
                category=RiskCategory.MARKET,
                severity=ConstraintSeverity.SOFT,
                message=(
                    f"annualised volatility {_money(snapshot.volatility_pct)}% exceeds "
                    f"the {_money(policy.max_volatility_pct)}% limit"
                ),
                actual=f"{_money(snapshot.volatility_pct)}%",
                limit=f"{_money(policy.max_volatility_pct)}%",
            )
        )
        restrictions.append(
            "reduce the requested notional until measured volatility is within the policy"
        )

    # 8. Liquidity.
    if snapshot.liquidity is None:
        if adds_exposure:
            insufficient.append("average daily traded value is unavailable")
    elif snapshot.liquidity < policy.min_liquidity_value:
        breaches.append(
            ConstraintBreach(
                code="min_liquidity",
                category=RiskCategory.LIQUIDITY,
                severity=ConstraintSeverity.REVIEW,
                message=(
                    f"average daily value {_money(snapshot.liquidity)} is below the "
                    f"{_money(policy.min_liquidity_value)} minimum"
                ),
                actual=_money(snapshot.liquidity),
                limit=_money(policy.min_liquidity_value),
            )
        )

    # 9. Forecast reliability (only when the action adds exposure).
    if adds_exposure and snapshot.forecast_failures:
        for failure in snapshot.forecast_failures:
            breaches.append(
                ConstraintBreach(
                    code="forecast_unreliable",
                    category=RiskCategory.FORECAST,
                    severity=ConstraintSeverity.REVIEW,
                    message=failure,
                )
            )

    # 10. Aggregate sector exposure across all portfolios.
    for sector, weight in snapshot.aggregate_sector_weights.items():
        if weight > policy.max_aggregate_sector_weight:
            breaches.append(
                ConstraintBreach(
                    code="max_aggregate_sector_weight",
                    category=RiskCategory.CONCENTRATION,
                    severity=ConstraintSeverity.REVIEW,
                    message=(
                        f"combined sector {sector} exposure would be "
                        f"{_money(weight)} of all portfolios; the limit is "
                        f"{_money(policy.max_aggregate_sector_weight)}"
                    ),
                    actual=_money(weight),
                    limit=_money(policy.max_aggregate_sector_weight),
                    sector=sector,
                )
            )

    # 11. Turnover.
    if snapshot.turnover > policy.max_turnover:
        breaches.append(
            ConstraintBreach(
                code="max_turnover",
                category=RiskCategory.CAPITAL,
                severity=ConstraintSeverity.SOFT,
                message=(
                    f"turnover {_money(snapshot.turnover)} exceeds "
                    f"{_money(policy.max_turnover)}"
                ),
                actual=_money(snapshot.turnover),
                limit=_money(policy.max_turnover),
            )
        )
        restrictions.append("split the rebalance across more than one review period")

    # A confirmed high-importance event is a review condition: unverified
    # reports are not consumed here, and a verified event never silently passes.
    for instrument_id in snapshot.event_pressure:
        breaches.append(
            ConstraintBreach(
                code="verified_event_pressure",
                category=RiskCategory.EVENT,
                severity=ConstraintSeverity.REVIEW,
                message=(
                    f"a confirmed high-importance event is open on {instrument_id}; "
                    "an authorized review is required"
                ),
                instrument_id=instrument_id,
            )
        )

    return PolicyEvaluation(
        breaches=tuple(breaches),
        warnings=tuple(warnings),
        restrictions=tuple(dict.fromkeys(restrictions)),
        insufficient=tuple(dict.fromkeys(insufficient)),
    )


__all__ = [
    "ConstraintBreach",
    "PolicyEvaluation",
    "RiskSnapshot",
    "UNCERTAINTY_RANK",
    "evaluate_policy",
]
