"""NM-09 strategy types: definitions, signals, proposals and conflicts.

A signal is an observation. A proposed action is a request. Neither is an
execution: NM-08 must decide on every proposal, and NM-10 is the only phase
allowed to create an order or mark anything executed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

STRATEGY_CONTRACT_VERSION = "nm09-strategies-1.0"


class StrategyCategory(StrEnum):
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    EVENT = "event"
    FORECAST = "forecast"


class SignalType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    EXIT = "exit"
    NO_SIGNAL = "no_signal"


class StrategyStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class ProposalStatus(StrEnum):
    CREATED = "created"
    PENDING_RISK = "pending_risk"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESTRICTED = "restricted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FORWARDED_TO_EXECUTION = "forwarded_to_execution"


class ConflictType(StrEnum):
    OPPOSITE_SIDES = "opposite_sides"
    DUPLICATE_ENTRY = "duplicate_entry"
    HORIZON_MISMATCH = "horizon_mismatch"


class ConflictOutcome(StrEnum):
    BLOCKED = "blocked"
    PRIORITY_SELECTED = "priority_selected"
    CONSOLIDATED = "consolidated"
    REVIEW_REQUIRED = "review_required"
    NO_ACTION = "no_action"


#: Explicit lifecycle. An unlisted transition is an error, never a silent reset.
STATUS_TRANSITIONS: dict[StrategyStatus, frozenset[StrategyStatus]] = {
    StrategyStatus.DRAFT: frozenset({StrategyStatus.ACTIVE, StrategyStatus.RETIRED}),
    StrategyStatus.ACTIVE: frozenset(
        {
            StrategyStatus.PAUSED,
            StrategyStatus.DEGRADED,
            StrategyStatus.SUSPENDED,
            StrategyStatus.RETIRED,
        }
    ),
    StrategyStatus.PAUSED: frozenset({StrategyStatus.ACTIVE, StrategyStatus.RETIRED}),
    StrategyStatus.DEGRADED: frozenset(
        {StrategyStatus.ACTIVE, StrategyStatus.PAUSED, StrategyStatus.SUSPENDED, StrategyStatus.RETIRED}
    ),
    StrategyStatus.SUSPENDED: frozenset({StrategyStatus.PAUSED, StrategyStatus.RETIRED}),
    StrategyStatus.RETIRED: frozenset(),
}


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def strategy_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def config_digest(payload: Any) -> str:
    import json

    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    """The reusable, immutable description of one strategy family."""

    catalog_id: str
    name: str
    category: StrategyCategory
    description: str
    asset_classes: tuple[str, ...]
    horizons: tuple[str, ...]
    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    position_rules: tuple[str, ...]
    required_data: tuple[str, ...]
    required_forecasts: tuple[str, ...]
    risk_requirements: tuple[str, ...]
    default_config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "asset_classes": list(self.asset_classes),
            "horizons": list(self.horizons),
            "entry_rules": list(self.entry_rules),
            "exit_rules": list(self.exit_rules),
            "position_rules": list(self.position_rules),
            "required_data": list(self.required_data),
            "required_forecasts": list(self.required_forecasts),
            "risk_requirements": list(self.risk_requirements),
            "default_config": dict(self.default_config),
        }


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    strategy_id: str
    catalog_id: str
    name: str
    category: StrategyCategory
    status: StrategyStatus
    version: str
    config: dict[str, Any]
    horizons: tuple[str, ...]
    asset_classes: tuple[str, ...]
    priority: int
    created_at: datetime
    updated_at: datetime
    last_evaluated_at: datetime | None = None
    status_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "catalog_id": self.catalog_id,
            "name": self.name,
            "category": self.category.value,
            "status": self.status.value,
            "version": self.version,
            "config": dict(self.config),
            "horizons": list(self.horizons),
            "asset_classes": list(self.asset_classes),
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_evaluated_at": (
                self.last_evaluated_at.isoformat() if self.last_evaluated_at else None
            ),
            "status_reason": self.status_reason,
            "is_strategy": True,
        }


@dataclass(frozen=True, slots=True)
class StrategySignal:
    signal_id: str
    strategy_id: str
    strategy_version: str
    instrument_id: str
    portfolio_id: str
    signal_type: SignalType
    created_at: datetime
    data_cutoff: datetime
    evaluation_horizon: str
    indicators: dict[str, Any]
    forecast_refs: tuple[str, ...]
    event_refs: tuple[str, ...]
    conditions: tuple[str, ...]
    confidence: Decimal
    explanation: str
    expires_at: datetime
    reference_price: Decimal | None
    data_quality: str
    horizon_days: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "instrument_id": self.instrument_id,
            "portfolio_id": self.portfolio_id,
            "signal_type": self.signal_type.value,
            "created_at": self.created_at.isoformat(),
            "market_data_cutoff": self.data_cutoff.isoformat(),
            "evaluation_horizon": self.evaluation_horizon,
            "indicators": dict(self.indicators),
            "forecast_references": list(self.forecast_refs),
            "event_references": list(self.event_refs),
            "triggered_conditions": list(self.conditions),
            "confidence": str(self.confidence),
            "explanation": self.explanation,
            "expires_at": self.expires_at.isoformat(),
            "reference_price": None if self.reference_price is None else str(self.reference_price),
            "data_quality": self.data_quality,
            "horizon_days": self.horizon_days,
            "is_signal": True,
        }


@dataclass(frozen=True, slots=True)
class ProposedAction:
    proposal_id: str
    signal_id: str
    strategy_id: str
    strategy_version: str
    instrument_id: str
    portfolio_id: str
    action: str
    side: str
    notional: Decimal
    reference_price: Decimal | None
    price_at: datetime | None
    created_at: datetime
    expires_at: datetime
    reason: str
    forecast_refs: tuple[str, ...]
    event_refs: tuple[str, ...]
    status: ProposalStatus
    risk_decision_id: str = ""
    risk_outcome: str = ""
    risk_policy_version: str = ""
    risk_state_version: str = ""
    risk_restrictions: tuple[str, ...] = ()
    risk_error: str = ""
    conflict_id: str = ""
    updated_at: datetime | None = None
    execution_order_id: str = ""
    execution_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "instrument_id": self.instrument_id,
            "portfolio_id": self.portfolio_id,
            "action": self.action,
            "side": self.side,
            "notional": str(self.notional),
            "reference_price": None if self.reference_price is None else str(self.reference_price),
            "price_at": self.price_at.isoformat() if self.price_at else None,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "reason": self.reason,
            "forecast_references": list(self.forecast_refs),
            "event_references": list(self.event_refs),
            "risk_decision_id": self.risk_decision_id,
            "risk_outcome": self.risk_outcome,
            "risk_policy_version": self.risk_policy_version,
            "risk_state_version": self.risk_state_version,
            "risk_restrictions": list(self.risk_restrictions),
            "risk_error": self.risk_error,
            "status": self.status.value,
            "conflict_id": self.conflict_id,
            "updated_at": (self.updated_at or self.created_at).isoformat(),
            "execution_order_id": self.execution_order_id,
            "execution_status": self.execution_status,
            "is_executed": False,
        }


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    conflict_id: str
    portfolio_id: str
    instrument_id: str
    conflict_type: ConflictType
    strategy_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]
    outcome: ConflictOutcome
    rationale: str
    created_at: datetime
    selected_signal_id: str = ""
    selected_strategy_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "portfolio_id": self.portfolio_id,
            "instrument_id": self.instrument_id,
            "conflict_type": self.conflict_type.value,
            "strategy_ids": list(self.strategy_ids),
            "signal_ids": list(self.signal_ids),
            "outcome": self.outcome.value,
            "rationale": self.rationale,
            "created_at": self.created_at.isoformat(),
            "selected_signal_id": self.selected_signal_id,
            "selected_strategy_id": self.selected_strategy_id,
        }


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    strategy_id: str
    at: datetime
    from_status: str
    to_status: str
    reason: str
    actor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "at": self.at.isoformat(),
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "actor": self.actor,
        }


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    strategy_id: str
    computed_at: datetime
    observations: int
    wins: int
    win_rate: Decimal | None
    average_return: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal | None
    sharpe: Decimal | None
    total_return: Decimal | None
    sufficient: bool
    reason: str
    degradation: str
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "computed_at": self.computed_at.isoformat(),
            "observations": self.observations,
            "wins": self.wins,
            "win_rate": None if self.win_rate is None else str(self.win_rate),
            "average_return": (
                None if self.average_return is None else str(self.average_return)
            ),
            "profit_factor": (
                None if self.profit_factor is None else str(self.profit_factor)
            ),
            "max_drawdown": (
                None if self.max_drawdown is None else str(self.max_drawdown)
            ),
            "sharpe": None if self.sharpe is None else str(self.sharpe),
            "total_return": (
                None if self.total_return is None else str(self.total_return)
            ),
            "sufficient": self.sufficient,
            "reason": self.reason,
            "degradation": self.degradation,
            "basis": self.basis,
            "is_executed_performance": False,
        }


__all__ = [
    "STRATEGY_CONTRACT_VERSION",
    "STATUS_TRANSITIONS",
    "ConflictOutcome",
    "ConflictRecord",
    "ConflictType",
    "LifecycleRecord",
    "PerformanceReport",
    "ProposalStatus",
    "ProposedAction",
    "SignalType",
    "StrategyCategory",
    "StrategyDefinition",
    "StrategyRecord",
    "StrategySignal",
    "StrategyStatus",
    "config_digest",
    "strategy_id",
    "utcnow",
]
