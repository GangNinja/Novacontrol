"""PI-21 portfolio-management vocabulary and validation.

This package composes the existing NM-07 portfolio, NM-08 risk, NM-10 paper
execution and PI-12 autonomy workflows.  It never owns a ledger, position,
cash balance, risk approval or order record.  The types here describe advisory
portfolio-management records and the one versioned configuration that controls
their thresholds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

PM_PHASE = "PI-21"
PM_CONTRACT_VERSION = "pi21-portfolio-management-1.0"
CONFIG_VERSION = "pm-config-1.0"

RISK_AUTHORITY = "NM-08"
EXECUTION_AUTHORITY = "NM-10"
PORTFOLIO_AUTHORITY = "NM-07 / existing portfolio, valuation and ledger services"
AUTONOMY_AUTHORITY = "PI-12 / AT-01..AT-06"
RESEARCH_AUTHORITY = "PI-19"
OPTIMIZATION_AUTHORITY = "PI-18"
AGENT_AUTHORITY = "PI-17"
POLICY_AUTHORITY = "PI-15 / PI-16 (advisory only)"

PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
FINANCIAL_MUTATION = False
ONE_CYCLE_AT_A_TIME = True
MAX_CYCLES_PER_QUERY = 100
MAX_OPPORTUNITIES = 25
MAX_DECISIONS = 100
MAX_REBALANCE_CHANGES = 20
MAX_AGENT_REFERENCES = 20
DEFAULT_STALE_AFTER_SECONDS = 900
DEFAULT_RESERVED_UNKNOWN = False

HORIZONS = ("5m", "15m", "20m", "60m", "1d", "3d", "5d", "30d", "90d")
HORIZON_FAMILIES: dict[str, str] = {
    "5m": "intraday",
    "15m": "intraday",
    "20m": "intraday",
    "60m": "intraday",
    "1h": "intraday",
    "1d": "swing",
    "3d": "swing",
    "5d": "swing",
    "30d": "long_term",
    "90d": "long_term",
}
HORIZON_ALIASES = {"1h": "60m", "day": "1d", "week": "5d", "month": "30d"}

#: NM-07 stores one of three investment horizons; a review analyses ONE window.
#: Each row is the representative horizon of that policy's own declared
#: ``forecast_horizons`` (``next_session`` → 1d, ``one_week`` → 5d,
#: ``three_months`` → 90d), so a scheduled review reads the window the portfolio
#: itself targets instead of the 5d default.
PORTFOLIO_HORIZON_REVIEW_HORIZONS: dict[str, str] = {
    "short_term": "1d",
    "medium_term": "5d",
    "long_term": "90d",
}


class PortfolioManagementError(RuntimeError):
    """Base class for an explicit PI-21 refusal."""

    code = "PORTFOLIO_MANAGEMENT_ERROR"
    http_status = 409

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = code or self.code

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


class PortfolioManagementValidationError(PortfolioManagementError):
    code = "PORTFOLIO_MANAGEMENT_VALIDATION"
    http_status = 422


class PortfolioManagementNotFound(PortfolioManagementError):
    code = "PORTFOLIO_MANAGEMENT_NOT_FOUND"
    http_status = 404


class PortfolioManagementStateError(PortfolioManagementError):
    code = "PORTFOLIO_MANAGEMENT_STATE"
    http_status = 409


class PortfolioManagementConflict(PortfolioManagementError):
    code = "PORTFOLIO_MANAGEMENT_CONFLICT"
    http_status = 409


class PortfolioManagementResourceError(PortfolioManagementError):
    code = "PORTFOLIO_MANAGEMENT_RESOURCE"
    http_status = 429


class CycleStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    MONITORING_ONLY = "monitoring_only"
    WAITING_FOR_EVIDENCE = "waiting_for_evidence"
    WAITING_FOR_RISK_ASSESSMENT = "waiting_for_risk_assessment"
    RISK_REJECTED = "risk_rejected"
    ORDER_PENDING = "order_pending"
    PARTIALLY_EXECUTED = "partially_executed"
    EXECUTED = "executed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class PositionAssessment(StrEnum):
    STRONG_POSITION = "strong_position"
    HOLD_POSITION = "hold_position"
    REVIEW_POSITION = "review_position"
    REDUCE_POSITION = "reduce_position"
    EXIT_CANDIDATE = "exit_candidate"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ManagementAction(StrEnum):
    OPEN = "open"
    ADD = "add"
    HOLD = "hold"
    REDUCE = "reduce"
    EXIT = "exit"
    RETAIN_CASH = "retain_cash"
    WAIT = "wait"
    NO_ACTION = "no_action"


class EntryRecommendation(StrEnum):
    NEW_POSITION = "new_position"
    ADD_TO_EXISTING_POSITION = "add_to_existing_position"
    WAIT = "wait"
    NO_ENTRY = "no_entry"


class ExitRecommendation(StrEnum):
    MAINTAIN = "maintain"
    REDUCE = "reduce"
    EXIT = "exit"
    WAIT = "wait"
    REVIEW_REQUIRED = "review_required"


class DeploymentRecommendation(StrEnum):
    DEPLOY_CAPITAL = "deploy_capital"
    PARTIAL_DEPLOYMENT = "partial_deployment"
    RETAIN_CASH = "retain_cash"
    WAIT_FOR_CONFIRMATION = "wait_for_confirmation"
    NO_DEPLOYMENT = "no_deployment"


class CashRecommendation(StrEnum):
    RETAIN_CASH = "retain_cash"
    DEPLOY_CASH = "deploy_cash"
    PRESERVE_RESERVES = "preserve_reserves"
    REDUCE_DEPLOYMENT = "reduce_deployment"
    WAIT = "wait"


class RebalanceAction(StrEnum):
    OPEN_POSITION = "open_position"
    INCREASE_POSITION = "increase_position"
    REDUCE_POSITION = "reduce_position"
    CLOSE_POSITION = "close_position"
    MAINTAIN_POSITION = "maintain_position"
    HOLD_CASH = "hold_cash"
    NO_REBALANCING_REQUIRED = "no_rebalancing_required"


class DataStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL_DATA = "partial_data"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: object, *, field_name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        result = value
    elif value in (None, ""):
        raise ValueError(f"{field_name} is required")
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} is not an ISO timestamp") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def decimal(value: object, *, field_name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be bool")
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a decimal value") from exc


def number(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def canonical_horizon(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "5d"
    text = HORIZON_ALIASES.get(text, text)
    if text not in HORIZONS:
        raise PortfolioManagementValidationError(
            f"unknown portfolio-management horizon {value!r}; "
            f"supported horizons: {', '.join(HORIZONS)}"
        )
    return text


def review_horizon(declared: object) -> str:
    """The analysis horizon a review runs for a portfolio's declared horizon.

    A declared canonical horizon (or its alias) wins, an NM-07 investment
    horizon is translated by :data:`PORTFOLIO_HORIZON_REVIEW_HORIZONS`, and
    anything missing or unrecognised keeps the documented 5d default rather
    than guessing.
    """

    text = str(declared or "").strip().lower()
    if not text:
        return "5d"
    normalised = HORIZON_ALIASES.get(text, text)
    if normalised in HORIZONS:
        return normalised
    return PORTFOLIO_HORIZON_REVIEW_HORIZONS.get(text, "5d")


def evidence_horizon(value: str) -> str:
    """Translate PI-21's 60m label to PI-20/PI-09's 1h contract."""

    return "1h" if value == "60m" else value


def stable_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class ManagementConfig:
    """One versioned configuration; thresholds never become risk limits."""

    cash_reserve_target: Decimal = Decimal("0.10")
    min_deployable_cash: Decimal = Decimal("500")
    max_position_weight: Decimal = Decimal("0.20")
    max_sector_weight: Decimal = Decimal("0.35")
    max_turnover: Decimal = Decimal("0.35")
    rebalance_threshold: Decimal = Decimal("0.02")
    min_trade_notional: Decimal = Decimal("100")
    min_expected_improvement: Decimal = Decimal("0.001")
    transaction_cost_bps: Decimal = Decimal("10")
    cooldown_hours: int = 6
    max_changes_per_cycle: int = 10
    max_opportunities: int = 10
    max_positions: int = 20
    max_forecast_age_hours: float = 168.0
    min_evidence_score: float = 50.0
    correlation_proxy_limit: Decimal = Decimal("0.75")
    version: str = CONFIG_VERSION

    def __post_init__(self) -> None:
        for name in (
            "cash_reserve_target",
            "max_position_weight",
            "max_sector_weight",
            "max_turnover",
            "rebalance_threshold",
            "min_deployable_cash",
            "min_trade_notional",
            "min_expected_improvement",
            "transaction_cost_bps",
            "correlation_proxy_limit",
        ):
            value = decimal(getattr(self, name), field_name=name)
            object.__setattr__(self, name, value)
        if not Decimal("0") <= self.cash_reserve_target < Decimal("1"):
            raise PortfolioManagementValidationError("cash_reserve_target must be within [0, 1)")
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise PortfolioManagementValidationError("max_position_weight must be within (0, 1]")
        if not Decimal("0") < self.max_sector_weight <= Decimal("1"):
            raise PortfolioManagementValidationError("max_sector_weight must be within (0, 1]")
        if not Decimal("0") < self.max_turnover <= Decimal("2"):
            raise PortfolioManagementValidationError("max_turnover must be within (0, 2]")
        if not Decimal("0") <= self.rebalance_threshold <= Decimal("0.25"):
            raise PortfolioManagementValidationError("rebalance_threshold must be within [0, 0.25]")
        if self.min_deployable_cash < 0 or self.min_trade_notional < 0:
            raise PortfolioManagementValidationError("cash and trade minimums must not be negative")
        if not Decimal("0") <= self.min_expected_improvement < Decimal("1"):
            raise PortfolioManagementValidationError(
                "min_expected_improvement must be within [0, 1)"
            )
        if not Decimal("0") <= self.transaction_cost_bps <= Decimal("500"):
            raise PortfolioManagementValidationError("transaction_cost_bps must be within 0..500")
        if not 0 <= self.cooldown_hours <= 720:
            raise PortfolioManagementValidationError("cooldown_hours must be within 0..720")
        if not 1 <= self.max_changes_per_cycle <= MAX_REBALANCE_CHANGES:
            raise PortfolioManagementValidationError(
                f"max_changes_per_cycle must be within 1..{MAX_REBALANCE_CHANGES}"
            )
        if not 1 <= self.max_opportunities <= MAX_OPPORTUNITIES:
            raise PortfolioManagementValidationError(
                f"max_opportunities must be within 1..{MAX_OPPORTUNITIES}"
            )
        if not 1 <= self.max_positions <= 50:
            raise PortfolioManagementValidationError("max_positions must be within 1..50")
        if not 0.1 <= self.max_forecast_age_hours <= 8760:
            raise PortfolioManagementValidationError(
                "max_forecast_age_hours must be within 0.1..8760"
            )
        if not 0 <= self.min_evidence_score <= 100:
            raise PortfolioManagementValidationError("min_evidence_score must be within 0..100")
        if not Decimal("0") <= self.correlation_proxy_limit <= Decimal("1"):
            raise PortfolioManagementValidationError(
                "correlation_proxy_limit must be within [0, 1]"
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> ManagementConfig:
        values = dict(payload or {})
        values.pop("digest", None)
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise PortfolioManagementValidationError(
                "unknown portfolio-management configuration field(s): " + ", ".join(unknown)
            )
        values.pop("version", None)
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise PortfolioManagementValidationError(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            body[name] = str(value) if isinstance(value, Decimal) else value
        body["digest"] = self.digest
        return body

    @property
    def digest(self) -> str:
        return digest(
            {
                name: str(getattr(self, name))
                if isinstance(getattr(self, name), Decimal)
                else getattr(self, name)
                for name in self.__dataclass_fields__
            }
        )


def authority_contract() -> dict[str, Any]:
    return {
        "portfolio_authority": PORTFOLIO_AUTHORITY,
        "risk_authority": RISK_AUTHORITY,
        "execution_authority": EXECUTION_AUTHORITY,
        "autonomy_authority": AUTONOMY_AUTHORITY,
        "research_authority": RESEARCH_AUTHORITY,
        "optimization_authority": OPTIMIZATION_AUTHORITY,
        "agent_authority": AGENT_AUTHORITY,
        "policy_authority": POLICY_AUTHORITY,
        "paper_only": PAPER_ONLY,
        "live_trading": LIVE_TRADING,
        "broker_connected": BROKER_CONNECTED,
        "financial_mutation": FINANCIAL_MUTATION,
        "portfolio_mutation": False,
        "ledger_write": False,
        "order_submission": False,
        "risk_override": False,
        "policy_activation": False,
        "nc_inr_rate": "1.0",
        "currency": "INR",
    }


__all__ = [
    "AGENT_AUTHORITY",
    "AUTONOMY_AUTHORITY",
    "BROKER_CONNECTED",
    "CashRecommendation",
    "CONFIG_VERSION",
    "CycleStatus",
    "DataStatus",
    "DeploymentRecommendation",
    "DEFAULT_STALE_AFTER_SECONDS",
    "EXECUTION_AUTHORITY",
    "EntryRecommendation",
    "ExitRecommendation",
    "FINANCIAL_MUTATION",
    "HORIZON_ALIASES",
    "HORIZON_FAMILIES",
    "HORIZONS",
    "LIVE_TRADING",
    "MAX_AGENT_REFERENCES",
    "MAX_CYCLES_PER_QUERY",
    "MAX_DECISIONS",
    "MAX_OPPORTUNITIES",
    "MAX_REBALANCE_CHANGES",
    "ManagementAction",
    "ManagementConfig",
    "ONE_CYCLE_AT_A_TIME",
    "OPTIMIZATION_AUTHORITY",
    "PAPER_ONLY",
    "PM_CONTRACT_VERSION",
    "PM_PHASE",
    "POLICY_AUTHORITY",
    "PORTFOLIO_AUTHORITY",
    "PORTFOLIO_HORIZON_REVIEW_HORIZONS",
    "PortfolioManagementConflict",
    "PortfolioManagementError",
    "PortfolioManagementNotFound",
    "PortfolioManagementResourceError",
    "PortfolioManagementStateError",
    "PortfolioManagementValidationError",
    "PositionAssessment",
    "RISK_AUTHORITY",
    "RESEARCH_AUTHORITY",
    "RebalanceAction",
    "authority_contract",
    "canonical_horizon",
    "decimal",
    "digest",
    "evidence_horizon",
    "money_text",
    "number",
    "parse_time",
    "review_horizon",
    "stable_id",
    "utcnow",
]
