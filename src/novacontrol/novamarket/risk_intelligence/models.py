"""PI-22 risk-intelligence vocabulary, thresholds and authority contract.

PI-22 is an analytical layer.  It does not create a risk decision, approve an
order or write accounting state.  NM-08 remains the sole risk authority and
NM-10 the sole paper-execution authority.
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

RI_PHASE = "PI-22"
RI_CONTRACT_VERSION = "pi22-risk-intelligence-1.0"
CONFIG_VERSION = "pi22-risk-config-1.0"
MAX_HISTORY = 500
MAX_ALERTS = 500
MAX_SCENARIOS = 200
MAX_SERIES_POINTS = 1000

RISK_AUTHORITY = "NM-08 RiskEngine (sole authoritative risk engine)"
EXECUTION_AUTHORITY = "NM-10 paper-execution and reconciliation engine"
MANAGEMENT_AUTHORITY = "PI-21 portfolio-management coordinator"
OPTIMIZATION_AUTHORITY = "PI-18 portfolio optimization"
ACCOUNTING_AUTHORITY = "NM-07 portfolio, valuation and ledger services"


class RiskIntelligenceError(RuntimeError):
    code = "RISK_INTELLIGENCE_ERROR"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        if code:
            self.code = code


class RiskIntelligenceValidationError(RiskIntelligenceError):
    code = "RISK_INTELLIGENCE_VALIDATION"
    http_status = 422


class RiskIntelligenceNotFound(RiskIntelligenceError):
    code = "RISK_INTELLIGENCE_NOT_FOUND"
    http_status = 404


class RiskIntelligenceStateError(RiskIntelligenceError):
    code = "RISK_INTELLIGENCE_STATE"
    http_status = 409


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    INSUFFICIENT_DATA = "insufficient_data"
    STALE_DATA = "stale_data"
    UNSUPPORTED_SCENARIO = "unsupported_scenario"
    CALCULATION_FAILED = "calculation_failed"
    REQUIRES_REVIEW = "requires_review"


class RiskLevel(StrEnum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"
    INSUFFICIENT_DATA = "insufficient_data"


class LiquidityClass(StrEnum):
    HIGH_LIQUIDITY = "high_liquidity"
    NORMAL_LIQUIDITY = "normal_liquidity"
    LOW_LIQUIDITY = "low_liquidity"
    VERY_LOW_LIQUIDITY = "very_low_liquidity"
    INSUFFICIENT_DATA = "insufficient_data"


class RiskTrend(StrEnum):
    IMPROVING = "improving"
    STABLE = "stable"
    DETERIORATING = "deteriorating"
    UNKNOWN = "unknown"


class AlertSeverity(StrEnum):
    INFORMATIONAL = "informational"
    WATCH = "watch"
    WARNING = "warning"
    HIGH_ALERT = "high_alert"
    CRITICAL_ALERT = "critical_alert"


class ProtectiveRecommendation(StrEnum):
    MAINTAIN_POSITION = "maintain_position"
    REVIEW_POSITION = "review_position"
    REDUCE_EXPOSURE = "reduce_exposure"
    EXIT_REVIEW = "exit_review"
    RETAIN_CASH = "retain_cash"
    REDUCE_NEW_ENTRIES = "reduce_new_entries"
    AVOID_NEW_EXPOSURE = "avoid_new_exposure"
    REVIEW_SECTOR_CONCENTRATION = "review_sector_concentration"
    REVIEW_CORRELATED_HOLDINGS = "review_correlated_holdings"
    WAIT_FOR_BETTER_LIQUIDITY = "wait_for_better_liquidity"
    INCREASE_MONITORING = "increase_monitoring"


RISK_LEVEL_RANK = {
    RiskLevel.INSUFFICIENT_DATA.value: -1,
    RiskLevel.NORMAL.value: 0,
    RiskLevel.ELEVATED.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}
ALERT_RANK = {
    AlertSeverity.INFORMATIONAL.value: 0,
    AlertSeverity.WATCH.value: 1,
    AlertSeverity.WARNING.value: 2,
    AlertSeverity.HIGH_ALERT.value: 3,
    AlertSeverity.CRITICAL_ALERT.value: 4,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif value in (None, ""):
        return None
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def number(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def decimal(value: object, *, default: str = "0") -> Decimal:
    if isinstance(value, bool):
        raise RiskIntelligenceValidationError("a boolean is not a decimal value")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RiskIntelligenceValidationError(f"invalid decimal value {value!r}") from exc


def stable_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def money_text(value: Decimal | float | int | str | None) -> str:
    parsed = decimal(0 if value is None else value)
    return format(parsed.quantize(Decimal("0.01")), "f")


@dataclass(frozen=True, slots=True)
class RiskThresholds:
    """Transparent research thresholds; none of these is an NM-08 limit."""

    max_position_weight: Decimal = Decimal("0.20")
    max_sector_weight: Decimal = Decimal("0.35")
    max_industry_weight: Decimal = Decimal("0.30")
    hhi_warning: Decimal = Decimal("0.25")
    hhi_high: Decimal = Decimal("0.40")
    top_three_warning: Decimal = Decimal("0.55")
    top_three_high: Decimal = Decimal("0.70")
    correlation_warning: Decimal = Decimal("0.70")
    correlation_high: Decimal = Decimal("0.85")
    correlation_change_warning: Decimal = Decimal("0.25")
    volatility_warning_pct: Decimal = Decimal("25")
    volatility_high_pct: Decimal = Decimal("40")
    drawdown_warning_pct: Decimal = Decimal("10")
    drawdown_high_pct: Decimal = Decimal("20")
    drawdown_critical_pct: Decimal = Decimal("30")
    liquidity_low_ratio: Decimal = Decimal("0.05")
    liquidity_high_ratio: Decimal = Decimal("0.15")
    liquidity_critical_ratio: Decimal = Decimal("0.30")
    liquidity_low_days: Decimal = Decimal("5")
    liquidity_high_days: Decimal = Decimal("15")
    liquidity_very_low_days: Decimal = Decimal("30")
    var_confidence: Decimal = Decimal("0.95")
    min_observations: int = 20
    min_correlation_observations: int = 20
    rolling_window: int = 20
    max_series_points: int = 252
    max_correlation_pairs: int = 100
    max_positions: int = 100
    annualization_periods: int = 252
    data_stale_hours: float = 72.0
    max_forecast_age_hours: float = 168.0
    alert_cooldown_hours: int = 12
    max_alerts: int = 200
    max_history: int = 200
    version: str = CONFIG_VERSION

    def __post_init__(self) -> None:
        decimal_fields = (
            "max_position_weight",
            "max_sector_weight",
            "max_industry_weight",
            "hhi_warning",
            "hhi_high",
            "top_three_warning",
            "top_three_high",
            "correlation_warning",
            "correlation_high",
            "correlation_change_warning",
            "volatility_warning_pct",
            "volatility_high_pct",
            "drawdown_warning_pct",
            "drawdown_high_pct",
            "drawdown_critical_pct",
            "liquidity_low_ratio",
            "liquidity_high_ratio",
            "liquidity_critical_ratio",
            "liquidity_low_days",
            "liquidity_high_days",
            "liquidity_very_low_days",
            "var_confidence",
        )
        for name in decimal_fields:
            object.__setattr__(
                self, name, decimal(getattr(self, name), default=str(getattr(self, name)))
            )
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise RiskIntelligenceValidationError("max_position_weight must be within (0, 1]")
        if not Decimal("0") < self.max_sector_weight <= Decimal("1"):
            raise RiskIntelligenceValidationError("max_sector_weight must be within (0, 1]")
        if not Decimal("0") < self.max_industry_weight <= Decimal("1"):
            raise RiskIntelligenceValidationError("max_industry_weight must be within (0, 1]")
        if not Decimal("0") <= self.correlation_warning < Decimal("1"):
            raise RiskIntelligenceValidationError("correlation_warning must be within [0, 1)")
        if not self.correlation_warning < self.correlation_high <= Decimal("1"):
            raise RiskIntelligenceValidationError(
                "correlation_high must be above correlation_warning and at most 1"
            )
        if not Decimal("0") < self.var_confidence < Decimal("1"):
            raise RiskIntelligenceValidationError("var_confidence must be within (0, 1)")
        if not (
            Decimal("0")
            < self.liquidity_low_days
            < self.liquidity_high_days
            < self.liquidity_very_low_days
        ):
            raise RiskIntelligenceValidationError(
                "liquidity day thresholds must be positive and increasing"
            )
        if not 2 <= self.min_observations <= 500:
            raise RiskIntelligenceValidationError("min_observations must be within 2..500")
        if not 2 <= self.min_correlation_observations <= 500:
            raise RiskIntelligenceValidationError(
                "min_correlation_observations must be within 2..500"
            )
        if not 2 <= self.rolling_window <= 500:
            raise RiskIntelligenceValidationError("rolling_window must be within 2..500")
        if not 10 <= self.max_series_points <= MAX_SERIES_POINTS:
            raise RiskIntelligenceValidationError(
                f"max_series_points must be within 10..{MAX_SERIES_POINTS}"
            )
        if not 1 <= self.max_correlation_pairs <= 500:
            raise RiskIntelligenceValidationError("max_correlation_pairs must be within 1..500")
        if not 1 <= self.max_positions <= 500:
            raise RiskIntelligenceValidationError("max_positions must be within 1..500")
        if not 1 <= self.annualization_periods <= 525600:
            raise RiskIntelligenceValidationError("annualization_periods must be positive")
        if not 1 <= self.data_stale_hours <= 8760:
            raise RiskIntelligenceValidationError("data_stale_hours must be within 1..8760")
        if not 1 <= self.max_forecast_age_hours <= 8760:
            raise RiskIntelligenceValidationError("max_forecast_age_hours must be within 1..8760")
        if not 0 <= self.alert_cooldown_hours <= 720:
            raise RiskIntelligenceValidationError("alert_cooldown_hours must be within 0..720")
        if not 1 <= self.max_alerts <= MAX_ALERTS:
            raise RiskIntelligenceValidationError(f"max_alerts must be within 1..{MAX_ALERTS}")
        if not 1 <= self.max_history <= MAX_HISTORY:
            raise RiskIntelligenceValidationError(f"max_history must be within 1..{MAX_HISTORY}")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> RiskThresholds:
        values = dict(payload or {})
        values.pop("digest", None)
        values.pop("version", None)
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise RiskIntelligenceValidationError(
                "unknown risk configuration field(s): " + ", ".join(unknown)
            )
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise RiskIntelligenceValidationError(str(exc)) from exc

    @property
    def digest(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            body[name] = str(value) if isinstance(value, Decimal) else value
        body["digest"] = digest({key: value for key, value in body.items() if key != "digest"})
        return body


def authority_contract() -> dict[str, Any]:
    return {
        "risk_authority": RISK_AUTHORITY,
        "paper_execution_authority": EXECUTION_AUTHORITY,
        "portfolio_management": MANAGEMENT_AUTHORITY,
        "optimization": OPTIMIZATION_AUTHORITY,
        "accounting": ACCOUNTING_AUTHORITY,
        "risk_override": False,
        "risk_decision_creation": False,
        "order_submission": False,
        "portfolio_mutation": False,
        "cash_mutation": False,
        "ledger_write": False,
        "fill_mutation": False,
        "broker_connected": False,
        "live_trading": False,
        "paper_only": True,
        "currency": "NC",
        "inr_equivalent": "1 NC = 1 INR",
    }


__all__ = [
    "ACCOUNTING_AUTHORITY",
    "ALERT_RANK",
    "AlertSeverity",
    "AnalysisStatus",
    "CONFIG_VERSION",
    "EXECUTION_AUTHORITY",
    "LiquidityClass",
    "MANAGEMENT_AUTHORITY",
    "MAX_ALERTS",
    "MAX_HISTORY",
    "MAX_SCENARIOS",
    "MAX_SERIES_POINTS",
    "OPTIMIZATION_AUTHORITY",
    "ProtectiveRecommendation",
    "RI_CONTRACT_VERSION",
    "RI_PHASE",
    "RISK_AUTHORITY",
    "RISK_LEVEL_RANK",
    "RiskIntelligenceError",
    "RiskIntelligenceNotFound",
    "RiskIntelligenceStateError",
    "RiskIntelligenceValidationError",
    "RiskLevel",
    "RiskThresholds",
    "RiskTrend",
    "authority_contract",
    "decimal",
    "digest",
    "money_text",
    "number",
    "parse_time",
    "stable_id",
    "utcnow",
]
