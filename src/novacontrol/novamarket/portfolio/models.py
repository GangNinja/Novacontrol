"""NM-07 portfolio value types, horizons, policies and lifecycle.

A portfolio is a *simulated book*: the NovaCoin it holds is real ledger balance
moved out of a funding wallet by an NM-03 transfer, and its positions are
recorded lots valued from timestamped NM-02 prices. Nothing here places an order
or talks to a broker; NM-10 owns execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

#: One paisa. Every NC amount is quantized to this unit.
PAISE = Decimal("0.01")

#: The smallest position quantity this build records.
SHARE = Decimal("0.0001")

#: A ceiling that catches typos: one lakh crore NC.
MAX_CAPITAL = Decimal("1000000000000")

POLICY_VERSION = "nm07-portfolio-1.0"


def utcnow() -> datetime:
    """One clock convention for every portfolio timestamp."""
    return datetime.now(tz=timezone.utc)


class PortfolioHorizon(StrEnum):
    SHORT = "short_term"
    MEDIUM = "medium_term"
    LONG = "long_term"


class PortfolioStatus(StrEnum):
    INITIALIZING = "initializing"
    ACTIVE = "active"
    UNDER_REVIEW = "under_review"
    REBALANCING = "rebalancing"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    CLOSED = "closed"


#: An illegal move raises; a portfolio's history is never overwritten.
PORTFOLIO_TRANSITIONS: dict[PortfolioStatus, frozenset[PortfolioStatus]] = {
    PortfolioStatus.INITIALIZING: frozenset(
        {PortfolioStatus.ACTIVE, PortfolioStatus.SUSPENDED, PortfolioStatus.CLOSED}
    ),
    PortfolioStatus.ACTIVE: frozenset(
        {
            PortfolioStatus.UNDER_REVIEW,
            PortfolioStatus.REBALANCING,
            PortfolioStatus.PAUSED,
            PortfolioStatus.SUSPENDED,
            PortfolioStatus.CLOSED,
        }
    ),
    PortfolioStatus.UNDER_REVIEW: frozenset(
        {
            PortfolioStatus.ACTIVE,
            PortfolioStatus.REBALANCING,
            PortfolioStatus.PAUSED,
            PortfolioStatus.SUSPENDED,
            PortfolioStatus.CLOSED,
        }
    ),
    PortfolioStatus.REBALANCING: frozenset(
        {
            PortfolioStatus.ACTIVE,
            PortfolioStatus.UNDER_REVIEW,
            PortfolioStatus.SUSPENDED,
            PortfolioStatus.CLOSED,
        }
    ),
    PortfolioStatus.PAUSED: frozenset(
        {
            PortfolioStatus.ACTIVE,
            PortfolioStatus.UNDER_REVIEW,
            PortfolioStatus.SUSPENDED,
            PortfolioStatus.CLOSED,
        }
    ),
    PortfolioStatus.SUSPENDED: frozenset({PortfolioStatus.PAUSED, PortfolioStatus.CLOSED}),
    PortfolioStatus.CLOSED: frozenset(),
}


def ensure_portfolio_transition(
    current: PortfolioStatus | str, target: PortfolioStatus | str
) -> tuple[PortfolioStatus, PortfolioStatus]:
    source = PortfolioStatus(current)
    destination = PortfolioStatus(target)
    if source is destination:
        return source, destination
    if destination not in PORTFOLIO_TRANSITIONS[source]:
        raise ValueError(
            f"portfolio cannot move from {source.value} to {destination.value}"
        )
    return source, destination


class ProposalStatus(StrEnum):
    VALIDATED = "validated"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


def _decimal(value: object, *, field_name: str, allow_float: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a decimal value, not bool")
    if isinstance(value, float):
        if not allow_float:
            raise TypeError(
                f"{field_name} must not be a binary float; use Decimal, int or a "
                "decimal string"
            )
        value = Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must not be empty")
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name} is not decimal: {value!r}") from exc
    raise TypeError(f"{field_name} must be Decimal, int or a decimal string")


def q2(value: object, *, field_name: str = "amount") -> Decimal:
    """A money amount quantized to paise; floats are refused."""
    amount = _decimal(value, field_name=field_name).quantize(
        PAISE, rounding=ROUND_HALF_EVEN
    )
    return amount


def parse_capital(value: object, *, field_name: str = "capital") -> Decimal:
    amount = _decimal(value, field_name=field_name)
    if amount <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    if amount > MAX_CAPITAL:
        raise ValueError(f"{field_name} exceeds the supported maximum")
    if amount != amount.quantize(PAISE):
        raise ValueError(
            f"{field_name} is finer than one paisa; an unrepresentable amount is "
            "refused rather than silently rounded"
        )
    return amount.quantize(PAISE)


def quantity(value: object, *, field_name: str = "quantity") -> Decimal:
    amount = _decimal(value, field_name=field_name)
    if amount < 0:
        raise ValueError(f"{field_name} must not be negative")
    return amount.quantize(SHARE)


def dec(value: object, *, field_name: str = "amount") -> Decimal:
    """Read a stored decimal string (never a float)."""
    return _decimal(value, field_name=field_name)


def money_text(amount: Decimal) -> str:
    return format(amount.quantize(PAISE, rounding=ROUND_HALF_EVEN), "f")


def quantity_text(amount: Decimal) -> str:
    return format(amount.quantize(SHARE), "f")


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    """One horizon's decision policy. Every limit is a stated decision."""

    horizon: PortfolioHorizon
    label: str
    forecast_horizons: tuple[str, ...]
    weighting: str
    max_positions: int
    max_position_weight: Decimal
    min_position_weight: Decimal
    max_sector_weight: Decimal
    min_cash_fraction: Decimal
    min_candidate_score: float
    max_forecast_age_hours: float
    max_uncertainty: str
    model_states: tuple[str, ...]
    drift_threshold: Decimal
    cooldown_hours: int
    max_turnover: Decimal
    exclude_event_pressure: bool
    review_interval_hours: int
    min_performance_observations: int
    fee_bps: int = 0

    def __post_init__(self) -> None:
        if self.weighting not in {
            "score",
            "inverse_volatility",
            "risk_budget",
            "equal",
        }:
            raise ValueError(f"unknown weighting {self.weighting!r}")
        if not 1 <= self.max_positions <= 50:
            raise ValueError("max_positions must be within 1..50")
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise ValueError("max_position_weight must be within (0, 1]")
        if not Decimal("0") <= self.min_position_weight < self.max_position_weight:
            raise ValueError("min_position_weight must be below the maximum")
        if not Decimal("0") < self.max_sector_weight <= Decimal("1"):
            raise ValueError("max_sector_weight must be within (0, 1]")
        if not Decimal("0") <= self.min_cash_fraction < Decimal("1"):
            raise ValueError("min_cash_fraction must be within [0, 1)")
        if not 0.0 <= self.min_candidate_score <= 100.0:
            raise ValueError("min_candidate_score must be within [0, 100]")
        if self.max_forecast_age_hours <= 0:
            raise ValueError("max_forecast_age_hours must be positive")
        if self.max_uncertainty not in {"low", "medium", "high"}:
            raise ValueError("max_uncertainty must be low, medium or high")
        if not self.forecast_horizons:
            raise ValueError("a policy needs at least one forecast horizon")
        if not Decimal("0") <= self.drift_threshold <= Decimal("1"):
            raise ValueError("drift_threshold must be within [0, 1]")
        if not Decimal("0") < self.max_turnover <= Decimal("2"):
            raise ValueError("max_turnover must be within (0, 2]")
        if self.cooldown_hours < 0 or self.review_interval_hours < 1:
            raise ValueError("cooldown/review intervals must be non-negative")
        if self.min_performance_observations < 2:
            raise ValueError("min_performance_observations must be at least 2")
        if not 0 <= self.fee_bps <= 500:
            raise ValueError("fee_bps must be within 0..500")

    @property
    def investable_fraction(self) -> Decimal:
        return Decimal("1") - self.min_cash_fraction

    @property
    def version(self) -> str:
        return POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "label": self.label,
            "forecast_horizons": list(self.forecast_horizons),
            "weighting": self.weighting,
            "max_positions": self.max_positions,
            "max_position_weight": money_text(self.max_position_weight),
            "min_position_weight": money_text(self.min_position_weight),
            "max_sector_weight": money_text(self.max_sector_weight),
            "min_cash_fraction": money_text(self.min_cash_fraction),
            "min_candidate_score": self.min_candidate_score,
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "max_uncertainty": self.max_uncertainty,
            "model_states": list(self.model_states),
            "drift_threshold": money_text(self.drift_threshold),
            "cooldown_hours": self.cooldown_hours,
            "max_turnover": money_text(self.max_turnover),
            "exclude_event_pressure": self.exclude_event_pressure,
            "review_interval_hours": self.review_interval_hours,
            "min_performance_observations": self.min_performance_observations,
            "fee_bps": self.fee_bps,
            "version": POLICY_VERSION,
        }


#: Three genuinely different configurations, not one formula with a new name.
HORIZON_POLICIES: dict[PortfolioHorizon, PortfolioPolicy] = {
    PortfolioHorizon.SHORT: PortfolioPolicy(
        horizon=PortfolioHorizon.SHORT,
        label="Short-term opportunities",
        forecast_horizons=("next_session", "few_sessions"),
        weighting="score",
        max_positions=6,
        max_position_weight=Decimal("0.25"),
        min_position_weight=Decimal("0.05"),
        max_sector_weight=Decimal("0.40"),
        min_cash_fraction=Decimal("0.10"),
        min_candidate_score=55.0,
        max_forecast_age_hours=12.0,
        max_uncertainty="medium",
        model_states=("active", "validated"),
        drift_threshold=Decimal("0.02"),
        cooldown_hours=6,
        max_turnover=Decimal("0.50"),
        exclude_event_pressure=True,
        review_interval_hours=6,
        min_performance_observations=20,
    ),
    PortfolioHorizon.MEDIUM: PortfolioPolicy(
        horizon=PortfolioHorizon.MEDIUM,
        label="Medium-term balance",
        forecast_horizons=("one_week", "one_month"),
        weighting="risk_budget",
        max_positions=10,
        max_position_weight=Decimal("0.18"),
        min_position_weight=Decimal("0.04"),
        max_sector_weight=Decimal("0.30"),
        min_cash_fraction=Decimal("0.08"),
        min_candidate_score=50.0,
        max_forecast_age_hours=72.0,
        max_uncertainty="high",
        model_states=("active", "validated", "degraded"),
        drift_threshold=Decimal("0.04"),
        cooldown_hours=24,
        max_turnover=Decimal("0.35"),
        exclude_event_pressure=False,
        review_interval_hours=24,
        min_performance_observations=20,
    ),
    PortfolioHorizon.LONG: PortfolioPolicy(
        horizon=PortfolioHorizon.LONG,
        label="Long-term compounding",
        forecast_horizons=("three_months", "one_month"),
        weighting="inverse_volatility",
        max_positions=12,
        max_position_weight=Decimal("0.15"),
        min_position_weight=Decimal("0.03"),
        max_sector_weight=Decimal("0.35"),
        min_cash_fraction=Decimal("0.05"),
        min_candidate_score=45.0,
        max_forecast_age_hours=168.0,
        max_uncertainty="high",
        model_states=("active", "validated", "degraded"),
        drift_threshold=Decimal("0.06"),
        cooldown_hours=72,
        max_turnover=Decimal("0.20"),
        exclude_event_pressure=False,
        review_interval_hours=72,
        min_performance_observations=20,
    ),
}


def policy_for(horizon: PortfolioHorizon | str) -> PortfolioPolicy:
    return HORIZON_POLICIES[PortfolioHorizon(horizon)]


def portfolio_id_for(
    horizon: PortfolioHorizon | str, funding_wallet_id: str, name: str
) -> str:
    seed = f"{PortfolioHorizon(horizon).value}|{funding_wallet_id}|{name.strip()}"
    return "pf_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def digest_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PortfolioRecord:
    portfolio_id: str
    name: str
    horizon: PortfolioHorizon
    funding_wallet_id: str
    wallet_id: str
    initial_capital: Decimal
    status: PortfolioStatus
    created_at: datetime
    updated_at: datetime
    realized_pnl: Decimal = Decimal("0.00")
    last_evaluated_at: datetime | None = None
    last_rebalanced_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "name": self.name,
            "horizon": self.horizon.value,
            "base_currency": "INR",
            "nova_coin": True,
            "funding_wallet_id": self.funding_wallet_id,
            "wallet_id": self.wallet_id,
            "initial_capital": money_text(self.initial_capital),
            "realized_pnl": money_text(self.realized_pnl),
            "status": self.status.value,
            "strategy_version": POLICY_VERSION,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_evaluated_at": (
                self.last_evaluated_at.isoformat() if self.last_evaluated_at else None
            ),
            "last_rebalanced_at": (
                self.last_rebalanced_at.isoformat() if self.last_rebalanced_at else None
            ),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "close_reason": self.close_reason,
            "is_simulated": True,
            "executes_orders": False,
        }


@dataclass(frozen=True, slots=True)
class Position:
    portfolio_id: str
    instrument_id: str
    quantity: Decimal
    average_cost: Decimal
    realized_pnl: Decimal
    sector: str
    asset_class: str
    opened_at: datetime
    updated_at: datetime
    last_price: Decimal | None = None
    priced_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def cost_basis(self) -> Decimal:
        return (self.quantity * self.average_cost).quantize(PAISE)

    def market_value(self) -> Decimal | None:
        if self.last_price is None:
            return None
        return (self.quantity * self.last_price).quantize(PAISE)

    def unrealized_pnl(self) -> Decimal | None:
        value = self.market_value()
        if value is None:
            return None
        return (value - self.cost_basis).quantize(PAISE)

    def to_dict(self) -> dict[str, Any]:
        value = self.market_value()
        unrealized = self.unrealized_pnl()
        return {
            "instrument_id": self.instrument_id,
            "quantity": quantity_text(self.quantity),
            "average_cost": money_text(self.average_cost),
            "cost_basis": money_text(self.cost_basis),
            "last_price": money_text(self.last_price) if self.last_price else None,
            "priced_at": self.priced_at.isoformat() if self.priced_at else None,
            "market_value": money_text(value) if value is not None else None,
            "unrealized_pnl": money_text(unrealized) if unrealized is not None else None,
            "realized_pnl": money_text(self.realized_pnl),
            "sector": self.sector,
            "asset_class": self.asset_class,
            "stale": self.last_price is None,
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


@dataclass(frozen=True, slots=True)
class TradeIntent:
    instrument_id: str
    side: Side
    target_weight: Decimal
    current_weight: Decimal
    notional: Decimal
    quantity_delta: Decimal
    price: Decimal
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "target_weight": money_text(self.target_weight),
            "current_weight": money_text(self.current_weight),
            "notional": money_text(self.notional),
            "quantity_delta": quantity_text(self.quantity_delta),
            "price": money_text(self.price),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RebalanceProposal:
    proposal_id: str
    portfolio_id: str
    created_at: datetime
    status: ProposalStatus
    drift: Decimal
    turnover: Decimal
    intents: tuple[TradeIntent, ...]
    rationale: str
    policy_version: str
    expires_at: datetime
    cash_after: Decimal
    digest: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "portfolio_id": self.portfolio_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "drift": money_text(self.drift),
            "turnover": money_text(self.turnover),
            "intents": [intent.to_dict() for intent in self.intents],
            "rationale": self.rationale,
            "policy_version": self.policy_version,
            "expires_at": self.expires_at.isoformat(),
            "cash_after": money_text(self.cash_after),
            "digest": self.digest,
            "notes": list(self.notes),
            "executes_orders": False,
            "requires_explicit_apply": True,
        }


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    portfolio_id: str
    kind: str
    at: datetime
    horizon: str
    payload: dict[str, Any]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "portfolio_id": self.portfolio_id,
            "kind": self.kind,
            "at": self.at.isoformat(),
            "horizon": self.horizon,
            "strategy_version": POLICY_VERSION,
            **self.payload,
            "digest": self.digest,
            "is_chain_of_thought": False,
        }


@dataclass(frozen=True, slots=True)
class ValuationSnapshot:
    snapshot_id: str
    portfolio_id: str
    at: datetime
    cash: Decimal
    invested: Decimal
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    return_pct: Decimal
    drawdown_pct: Decimal
    cash_weight: Decimal
    priced_positions: int
    stale_positions: int
    holdings: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "portfolio_id": self.portfolio_id,
            "at": self.at.isoformat(),
            "cash": money_text(self.cash),
            "invested": money_text(self.invested),
            "total_value": money_text(self.total_value),
            "realized_pnl": money_text(self.realized_pnl),
            "unrealized_pnl": money_text(self.unrealized_pnl),
            "return_pct": money_text(self.return_pct),
            "drawdown_pct": money_text(self.drawdown_pct),
            "cash_weight": money_text(self.cash_weight),
            "priced_positions": self.priced_positions,
            "stale_positions": self.stale_positions,
            "holdings": self.holdings,
        }
