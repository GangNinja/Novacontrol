"""NM-08 risk types: categories, policies, decisions, alerts and overrides.

The risk engine is deliberately independent of portfolio construction. It reads
NM-02…NM-07 state and returns enforceable decisions; it never proposes a trade
and never trusts another subsystem's recommendation as evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

PAISE = Decimal("0.01")
HUNDRED = Decimal("100")
POLICY_VERSION = "nm08-risk-1.0"
DEFAULT_MIN_OBSERVATIONS = 20
DECISION_TTL_HOURS = 2.0


class RiskCategory(StrEnum):
    MARKET = "market"
    CONCENTRATION = "concentration"
    LIQUIDITY = "liquidity"
    FORECAST = "forecast"
    EVENT = "event"
    CAPITAL = "capital"
    DATA = "data"
    OPERATIONAL = "operational"


class RiskOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    RESTRICTED = "restricted"
    REQUIRES_REVIEW = "requires_review"
    INSUFFICIENT_DATA = "insufficient_data"


class RiskAction(StrEnum):
    APPLY_PROPOSAL = "apply_proposal"
    REBALANCE = "rebalance"
    ADD_POSITION = "add_position"
    INCREASE_POSITION = "increase_position"
    REDUCE_POSITION = "reduce_position"
    REMOVE_POSITION = "remove_position"
    ALLOCATE_CAPITAL = "allocate_capital"
    RESERVE_CAPITAL = "reserve_capital"
    PAPER_ORDER = "paper_order"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class ConstraintSeverity(StrEnum):
    HARD = "hard"
    REVIEW = "review"
    SOFT = "soft"
    WARNING = "warning"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def risk_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def digest_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def dec(value: object, *, field_name: str = "value") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a decimal number") from exc


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One measured metric, or an explicit statement that it is unavailable."""

    name: str
    value: Decimal | None
    unit: str = "ratio"
    observations: int = 0
    sufficient: bool = False
    reason: str = ""
    assumptions: tuple[str, ...] = ()

    @classmethod
    def insufficient(
        cls, name: str, reason: str, *, observations: int = 0, unit: str = "ratio"
    ) -> "MetricResult":
        return cls(
            name=name,
            value=None,
            unit=unit,
            observations=observations,
            sufficient=False,
            reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": None if self.value is None else str(self.value),
            "unit": self.unit,
            "observations": self.observations,
            "sufficient": self.sufficient,
            "reason": self.reason,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """One versioned policy. Every limit is a stated simulation default."""

    policy_id: str
    version: str
    label: str
    max_position_weight: Decimal
    max_sector_weight: Decimal
    max_invested_fraction: Decimal
    min_cash_fraction: Decimal
    max_drawdown_pct: Decimal
    max_volatility_pct: Decimal
    min_liquidity_value: Decimal
    max_turnover: Decimal
    max_forecast_age_hours: float
    allowed_model_states: tuple[str, ...]
    max_uncertainty: str
    max_data_age_hours: float
    max_state_age_hours: float
    min_observations: int
    max_aggregate_sector_weight: Decimal
    restricted_instruments: tuple[str, ...] = ()
    aggregate: bool = False

    def __post_init__(self) -> None:
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise ValueError("max_position_weight must be within (0, 1]")
        if not Decimal("0") < self.max_sector_weight <= Decimal("1"):
            raise ValueError("max_sector_weight must be within (0, 1]")
        if not Decimal("0") < self.max_invested_fraction <= Decimal("1"):
            raise ValueError("max_invested_fraction must be within (0, 1]")
        if not Decimal("0") <= self.min_cash_fraction < Decimal("1"):
            raise ValueError("min_cash_fraction must be within [0, 1)")
        if self.max_drawdown_pct < 0 or self.max_volatility_pct < 0:
            raise ValueError("drawdown and volatility limits cannot be negative")
        if self.min_liquidity_value < 0:
            raise ValueError("min_liquidity_value cannot be negative")
        if not Decimal("0") < self.max_turnover <= Decimal("2"):
            raise ValueError("max_turnover must be within (0, 2]")
        if self.max_forecast_age_hours <= 0 or self.max_data_age_hours <= 0:
            raise ValueError("freshness limits must be positive")
        if self.max_state_age_hours <= 0:
            raise ValueError("max_state_age_hours must be positive")
        if self.min_observations < 2:
            raise ValueError("min_observations must be at least 2")
        if self.max_uncertainty not in {"low", "medium", "high"}:
            raise ValueError("max_uncertainty must be low, medium or high")
        if not Decimal("0") < self.max_aggregate_sector_weight <= Decimal("1"):
            raise ValueError("max_aggregate_sector_weight must be within (0, 1]")
        if not self.allowed_model_states:
            raise ValueError("at least one forecast model state must be allowed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "label": self.label,
            "aggregate": self.aggregate,
            "max_position_weight": str(self.max_position_weight),
            "max_sector_weight": str(self.max_sector_weight),
            "max_invested_fraction": str(self.max_invested_fraction),
            "min_cash_fraction": str(self.min_cash_fraction),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "max_volatility_pct": str(self.max_volatility_pct),
            "min_liquidity_value": str(self.min_liquidity_value),
            "max_turnover": str(self.max_turnover),
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "allowed_model_states": list(self.allowed_model_states),
            "max_uncertainty": self.max_uncertainty,
            "max_data_age_hours": self.max_data_age_hours,
            "max_state_age_hours": self.max_state_age_hours,
            "min_observations": self.min_observations,
            "max_aggregate_sector_weight": str(self.max_aggregate_sector_weight),
            "restricted_instruments": list(self.restricted_instruments),
        }


def _policy(
    policy_id: str,
    label: str,
    *,
    position: str,
    sector: str,
    invested: str,
    cash: str,
    drawdown: str,
    volatility: str,
    liquidity: str,
    turnover: str,
    forecast_age: float,
    states: tuple[str, ...],
    uncertainty: str,
    data_age: float,
    state_age: float,
    aggregate_sector: str,
    aggregate: bool = False,
) -> RiskPolicy:
    return RiskPolicy(
        policy_id=policy_id,
        version=POLICY_VERSION,
        label=label,
        max_position_weight=Decimal(position),
        max_sector_weight=Decimal(sector),
        max_invested_fraction=Decimal(invested),
        min_cash_fraction=Decimal(cash),
        max_drawdown_pct=Decimal(drawdown),
        max_volatility_pct=Decimal(volatility),
        min_liquidity_value=Decimal(liquidity),
        max_turnover=Decimal(turnover),
        max_forecast_age_hours=forecast_age,
        allowed_model_states=states,
        max_uncertainty=uncertainty,
        max_data_age_hours=data_age,
        max_state_age_hours=state_age,
        min_observations=DEFAULT_MIN_OBSERVATIONS,
        max_aggregate_sector_weight=Decimal(aggregate_sector),
        aggregate=aggregate,
    )


#: Simulation defaults, not proven investment limits. Each is versioned and can
#: be replaced by an audited policy update; a decision records the version used.
POLICY_DEFAULTS: dict[str, RiskPolicy] = {
    "short_term": _policy(
        "short_term",
        "Short-term risk policy",
        position="0.25",
        sector="0.40",
        invested="0.90",
        cash="0.10",
        drawdown="12",
        volatility="35",
        liquidity="25000",
        turnover="0.50",
        forecast_age=12,
        states=("active", "validated"),
        uncertainty="medium",
        data_age=24,
        state_age=1,
        aggregate_sector="0.60",
    ),
    "medium_term": _policy(
        "medium_term",
        "Medium-term risk policy",
        position="0.18",
        sector="0.30",
        invested="0.92",
        cash="0.08",
        drawdown="20",
        volatility="28",
        liquidity="50000",
        turnover="0.35",
        forecast_age=72,
        states=("active", "validated", "degraded"),
        uncertainty="high",
        data_age=72,
        state_age=6,
        aggregate_sector="0.55",
    ),
    "long_term": _policy(
        "long_term",
        "Long-term risk policy",
        position="0.15",
        sector="0.35",
        invested="0.95",
        cash="0.05",
        drawdown="30",
        volatility="25",
        liquidity="75000",
        turnover="0.20",
        forecast_age=168,
        states=("active", "validated", "degraded"),
        uncertainty="high",
        data_age=168,
        state_age=24,
        aggregate_sector="0.55",
    ),
    "aggregate": _policy(
        "aggregate",
        "Combined three-portfolio risk policy",
        position="0.20",
        sector="0.45",
        invested="0.92",
        cash="0.08",
        drawdown="15",
        volatility="30",
        liquidity="100000",
        turnover="0.30",
        forecast_age=168,
        states=("active", "validated", "degraded"),
        uncertainty="high",
        data_age=168,
        state_age=24,
        aggregate_sector="0.45",
        aggregate=True,
    ),
}


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    action: str
    outcome: RiskOutcome
    created_at: datetime
    expires_at: datetime
    policy_id: str
    policy_version: str
    state_version: str
    portfolio_id: str = ""
    proposal_id: str = ""
    instrument_id: str = ""
    data_quality: str = "measured"
    explanation: str = ""
    metrics: dict[str, MetricResult] = None  # type: ignore[assignment]
    violations: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    restrictions: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    actor: str = ""

    def __post_init__(self) -> None:
        if self.metrics is None:
            object.__setattr__(self, "metrics", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "action": self.action,
            "outcome": self.outcome.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "state_version": self.state_version,
            "portfolio_id": self.portfolio_id,
            "proposal_id": self.proposal_id,
            "instrument_id": self.instrument_id,
            "data_quality": self.data_quality,
            "explanation": self.explanation,
            "metrics": {name: metric.to_dict() for name, metric in self.metrics.items()},
            "violations": [dict(item) for item in self.violations],
            "warnings": [dict(item) for item in self.warnings],
            "restrictions": list(self.restrictions),
            "references": list(self.references),
            "actor": self.actor,
            "is_risk_decision": True,
        }


@dataclass(frozen=True, slots=True)
class RiskAlert:
    alert_id: str
    dedupe_key: str
    category: RiskCategory
    severity: Severity
    created_at: datetime
    updated_at: datetime
    status: AlertStatus = AlertStatus.OPEN
    portfolio_id: str = ""
    instrument_id: str = ""
    metric: str = ""
    threshold: str = ""
    observed: str = ""
    explanation: str = ""
    recommended_action: str = ""
    decision_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "portfolio_id": self.portfolio_id,
            "instrument_id": self.instrument_id,
            "metric": self.metric,
            "threshold": self.threshold,
            "observed": self.observed,
            "explanation": self.explanation,
            "recommended_action": self.recommended_action,
            "decision_id": self.decision_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RiskOverride:
    override_id: str
    decision_id: str
    actor: str
    reason: str
    created_at: datetime
    expires_at: datetime
    constraint_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "override_id": self.override_id,
            "decision_id": self.decision_id,
            "actor": self.actor,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "constraint_codes": list(self.constraint_codes),
            "active": self.expires_at > utcnow(),
        }


@dataclass(frozen=True, slots=True)
class StressScenario:
    scenario_id: str
    name: str
    kind: str
    shock_pct: Decimal
    description: str
    sector: str = ""
    instrument_id: str = ""
    hypothetical: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "kind": self.kind,
            "shock_pct": str(self.shock_pct),
            "sector": self.sector,
            "instrument_id": self.instrument_id,
            "description": self.description,
            "hypothetical": True,
            "is_forecast": False,
        }


SCENARIOS: dict[str, StressScenario] = {
    "market_decline": StressScenario(
        "market_decline",
        "Broad market decline",
        "market",
        Decimal("-10"),
        "A hypothetical 10% fall across every equity holding.",
    ),
    "sector_shock": StressScenario(
        "sector_shock",
        "Sector shock",
        "sector",
        Decimal("-15"),
        "A hypothetical 15% fall in the portfolio's largest sector.",
    ),
    "volatility_shock": StressScenario(
        "volatility_shock",
        "Volatility shock",
        "volatility",
        Decimal("50"),
        "A hypothetical 50% increase in measured volatility; values are unchanged.",
    ),
    "liquidity_shock": StressScenario(
        "liquidity_shock",
        "Liquidity deterioration",
        "liquidity",
        Decimal("-50"),
        "A hypothetical 50% fall in traded value; values are unchanged.",
    ),
    "currency_shock": StressScenario(
        "currency_shock",
        "Currency shock",
        "currency",
        Decimal("-5"),
        "A hypothetical 5% currency move. The book is single-currency (INR), so "
        "the modelled impact is zero and that assumption is stated.",
    ),
    "company_shock": StressScenario(
        "company_shock",
        "Company-specific shock",
        "instrument",
        Decimal("-20"),
        "A hypothetical 20% fall in the largest single holding.",
    ),
    "interest_rate_shock": StressScenario(
        "interest_rate_shock",
        "Interest-rate shock",
        "rate",
        Decimal("-5"),
        "A hypothetical 100bp yield rise. No duration or rate-sensitivity "
        "model exists in this build, so the modelled market-value impact is "
        "zero; this assumption is stated rather than hidden.",
    ),
    "commodity_shock": StressScenario(
        "commodity_shock",
        "Commodity-price shock",
        "sector",
        Decimal("-20"),
        "A hypothetical 20% fall in Energy-sector holdings.",
        sector="Energy",
    ),
    "geopolitical_shock": StressScenario(
        "geopolitical_shock",
        "Geopolitical disruption",
        "market",
        Decimal("-7"),
        "A hypothetical 7% broad-market decline following a geopolitical "
        "disruption.",
    ),
}
