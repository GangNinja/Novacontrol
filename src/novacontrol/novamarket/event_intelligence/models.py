"""PI-24 event-intelligence contracts.

The layer consumes NM-04's verified event radar and PI-19's research evidence.
It never fetches its own news, invents a direction, changes a forecast, promotes
a model, approves risk or submits an order.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

PI24_PHASE = "PI-24"
PI24_VERSION = "1.0.0"
PI24_CONTRACT_VERSION = "pi24-event-intelligence.1.0"
PI24_SCHEMA_VERSION = 1
FEATURE_VERSION = "pi24-event-features-1.0"
RELEVANCE_POLICY_VERSION = "pi24.event-relevance.1.0"
WARNING_POLICY_VERSION = "pi24.event-warning.1.0"
OUTCOME_POLICY_VERSION = "pi24.event-outcome.1.0"
EVALUATION_POLICY_VERSION = "pi24.event-aware-evaluation.1.0"

RISK_AUTHORITY = "NM-08"
EXECUTION_AUTHORITY = "NM-10"
MAX_TEXT = 4_000
MAX_EVENTS = 500
MAX_RECORDS = 20_000
MAX_OUTCOMES = 20_000
MAX_INSTRUMENTS = 100
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_UPCOMING_DAYS = 30
DEFAULT_MIN_OUTCOME_SAMPLES = 10

HORIZONS: dict[str, dict[str, str]] = {
    "5m": {"group": "intraday", "label": "5 minutes"},
    "15m": {"group": "intraday", "label": "15 minutes"},
    "20m": {"group": "intraday", "label": "20 minutes"},
    "1h": {"group": "intraday", "label": "1 hour"},
    "1d": {"group": "swing", "label": "1 trading day"},
    "3d": {"group": "swing", "label": "3 trading days"},
    "5d": {"group": "swing", "label": "5 trading days"},
    "30d": {"group": "long_term", "label": "30 calendar days"},
    "90d": {"group": "long_term", "label": "90 calendar days"},
}
HORIZON_DAYS = {
    "5m": 0.0,
    "15m": 0.0,
    "20m": 0.0,
    "1h": 0.0,
    "1d": 1.0,
    "3d": 3.0,
    "5d": 5.0,
    "30d": 30.0,
    "90d": 90.0,
}

_REQUIRED_HORIZON = re.compile(r"^[A-Za-z0-9]+$")
_INSTRUMENT = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,63}$")
_INSTRUCTION_RISK = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+"
    r"(?:instructions?|prompts?)|(?:system|developer)\s+(?:prompt|message)|"
    r"reveal\s+(?:the\s+)?(?:api\s*key|secret|password|token)|"
    r"execute\s+(?:this|the following)\s+(?:command|tool)",
    re.IGNORECASE,
)


class EventIntelligenceError(ValueError):
    """A PI-24 request is invalid."""

    code = "EVENT_INTELLIGENCE_ERROR"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        if code:
            self.code = code


class EventIntelligenceValidationError(EventIntelligenceError):
    code = "EVENT_INTELLIGENCE_VALIDATION"
    http_status = 422


class EventIntelligenceNotFoundError(EventIntelligenceError):
    code = "EVENT_INTELLIGENCE_NOT_FOUND"
    http_status = 404


class EventIntelligenceStateError(EventIntelligenceError):
    code = "EVENT_INTELLIGENCE_STATE"
    http_status = 409


class EventIntelligenceUnavailableError(EventIntelligenceError):
    code = "EVENT_INTELLIGENCE_UNAVAILABLE"
    http_status = 503


class EventGroup(StrEnum):
    CORPORATE = "corporate"
    ECONOMIC = "economic"
    MARKET = "market"
    COMPANY_SPECIFIC = "company_specific"
    UNCLASSIFIED = "unclassified"


class EventStatus(StrEnum):
    DETECTED = "detected"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    DUPLICATE = "duplicate"
    CONFLICTED = "conflicted"
    IRRELEVANT = "irrelevant"
    EXPIRED = "expired"
    COMPLETED = "completed"


class RelevanceClass(StrEnum):
    DIRECT = "direct"
    SECTOR_RELATED = "sector_related"
    MARKET_WIDE = "market_wide"
    INDIRECT = "indirect"
    NOT_RELEVANT = "not_relevant"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class WarningStatus(StrEnum):
    NORMAL = "normal"
    EVENT_APPROACHING = "event_approaching"
    EVENT_UNCERTAINTY = "event_uncertainty"
    EVENT_VOLATILITY_RISK = "event_volatility_risk"
    UNVERIFIED_EVENT = "unverified_event"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ImpactStatus(StrEnum):
    OBSERVED_HISTORICAL = "observed_historical"
    DIRECTION_UNKNOWN = "direction_unknown"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_APPLICABLE = "not_applicable"


class OutcomeStatus(StrEnum):
    RECORDED = "recorded"
    INSUFFICIENT_DATA = "insufficient_data"
    STALE_DATA = "stale_data"
    NOT_APPLICABLE = "not_applicable"


#: Maps NM-04's declared subcategories into the four PI-24 families. The radar
#: remains the classifier; this table only groups its existing labels.
EVENT_GROUPS: dict[str, str] = {
    "earnings": EventGroup.CORPORATE.value,
    "guidance": EventGroup.CORPORATE.value,
    "management_change": EventGroup.CORPORATE.value,
    "merger_acquisition": EventGroup.CORPORATE.value,
    "restructuring": EventGroup.CORPORATE.value,
    "buyback": EventGroup.CORPORATE.value,
    "dividend": EventGroup.CORPORATE.value,
    "stock_split": EventGroup.CORPORATE.value,
    "fundraising": EventGroup.CORPORATE.value,
    "product_launch": EventGroup.COMPANY_SPECIFIC.value,
    "regulatory_investigation": EventGroup.COMPANY_SPECIFIC.value,
    "litigation": EventGroup.COMPANY_SPECIFIC.value,
    "bankruptcy": EventGroup.COMPANY_SPECIFIC.value,
    "contract": EventGroup.COMPANY_SPECIFIC.value,
    "partnership": EventGroup.COMPANY_SPECIFIC.value,
    "inflation": EventGroup.ECONOMIC.value,
    "interest_rate": EventGroup.ECONOMIC.value,
    "central_bank": EventGroup.ECONOMIC.value,
    "gdp": EventGroup.ECONOMIC.value,
    "employment": EventGroup.ECONOMIC.value,
    "fiscal_policy": EventGroup.ECONOMIC.value,
    "budget": EventGroup.ECONOMIC.value,
    "currency": EventGroup.ECONOMIC.value,
    "sovereign_debt": EventGroup.ECONOMIC.value,
    "trade_policy": EventGroup.ECONOMIC.value,
    "geopolitics": EventGroup.MARKET.value,
    "sector_regulation": EventGroup.COMPANY_SPECIFIC.value,
    "demand": EventGroup.COMPANY_SPECIFIC.value,
    "supply_chain": EventGroup.COMPANY_SPECIFIC.value,
    "commodity_prices": EventGroup.MARKET.value,
    "technology": EventGroup.COMPANY_SPECIFIC.value,
    "earnings_trend": EventGroup.COMPANY_SPECIFIC.value,
    "sector_investment": EventGroup.COMPANY_SPECIFIC.value,
    "competition": EventGroup.COMPANY_SPECIFIC.value,
    "exchange_announcement": EventGroup.MARKET.value,
    "trading_halt": EventGroup.MARKET.value,
    "listing": EventGroup.CORPORATE.value,
    "delisting": EventGroup.CORPORATE.value,
    "index_change": EventGroup.MARKET.value,
    "liquidity": EventGroup.MARKET.value,
    "circuit_breaker": EventGroup.MARKET.value,
    "infrastructure_incident": EventGroup.MARKET.value,
}

IMPORTANCE_VALUE = {"low": 0.25, "moderate": 0.5, "high": 0.75, "critical": 1.0}
CORE_EVENT_FLAGS = {
    "earnings": "event_earnings_flag",
    "guidance": "event_guidance_flag",
    "dividend": "event_dividend_flag",
    "stock_split": "event_corporate_action_flag",
    "merger_acquisition": "event_corporate_action_flag",
    "buyback": "event_corporate_action_flag",
    "fundraising": "event_corporate_action_flag",
    "regulatory_investigation": "event_regulatory_flag",
    "litigation": "event_litigation_flag",
    "trading_halt": "event_trading_halt_flag",
    "interest_rate": "event_rate_decision_flag",
    "inflation": "event_inflation_flag",
    "gdp": "event_gdp_flag",
}

RELEVANCE_ORDER = {
    RelevanceClass.DIRECT: 6,
    RelevanceClass.SECTOR_RELATED: 5,
    RelevanceClass.MARKET_WIDE: 4,
    RelevanceClass.INDIRECT: 3,
    RelevanceClass.NOT_RELEVANT: 2,
    RelevanceClass.INSUFFICIENT_EVIDENCE: 1,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: object, name: str = "timestamp") -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise EventIntelligenceValidationError(f"{name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clean(value: object, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()[:limit]


def number(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def canonical_instrument(value: object) -> str:
    cleaned = clean(value, 80).upper()
    if not _INSTRUMENT.match(cleaned):
        raise EventIntelligenceValidationError(f"instrument_id {value!r} is not canonical")
    return cleaned


def canonical_horizon(value: object) -> str:
    cleaned = clean(value, 16).lower()
    aliases = {"60m": "1h", "1hour": "1h", "daily": "1d", "weekly": "5d"}
    resolved = aliases.get(cleaned, cleaned)
    if resolved not in HORIZONS:
        raise EventIntelligenceValidationError(
            f"unsupported horizon {value!r}; expected one of {', '.join(HORIZONS)}"
        )
    return resolved


def event_group(subcategory: str) -> str:
    return EVENT_GROUPS.get(clean(subcategory, 80).lower(), EventGroup.UNCLASSIFIED.value)


def importance_value(value: object) -> float | None:
    return IMPORTANCE_VALUE.get(clean(value, 20).lower())


def stable_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def instruction_risk(text_value: str) -> bool:
    return bool(_INSTRUCTION_RISK.search(text_value))


@dataclass(frozen=True, slots=True)
class EventIntelligenceConfig:
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    upcoming_days: int = DEFAULT_UPCOMING_DAYS
    min_relevance_events: int = 1
    min_outcome_samples: int = DEFAULT_MIN_OUTCOME_SAMPLES
    min_evaluation_samples: int = 20
    max_events: int = 200
    max_records: int = MAX_RECORDS
    max_outcomes: int = MAX_OUTCOMES
    max_instruments: int = 50
    stale_hours: float = 168.0
    approaching_hours: float = 72.0
    volatility_importance_threshold: float = 0.75
    version: str = "pi24-event-config-1.0"

    def __post_init__(self) -> None:
        for name, value, low, high in (
            ("lookback_days", self.lookback_days, 1, 3650),
            ("upcoming_days", self.upcoming_days, 1, 365),
            ("min_relevance_events", self.min_relevance_events, 0, 100),
            ("min_outcome_samples", self.min_outcome_samples, 2, 1000),
            ("min_evaluation_samples", self.min_evaluation_samples, 2, 5000),
            ("max_events", self.max_events, 1, MAX_EVENTS),
            ("max_records", self.max_records, 100, MAX_RECORDS),
            ("max_outcomes", self.max_outcomes, 100, MAX_OUTCOMES),
            ("max_instruments", self.max_instruments, 1, MAX_INSTRUMENTS),
            ("stale_hours", self.stale_hours, 1.0, 8760.0),
            ("approaching_hours", self.approaching_hours, 1.0, 720.0),
        ):
            if not low <= value <= high:
                raise EventIntelligenceValidationError(f"{name} must be between {low} and {high}")
        if not 0.0 < self.volatility_importance_threshold <= 1.0:
            raise EventIntelligenceValidationError(
                "volatility_importance_threshold must be in (0, 1]"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lookback_days": self.lookback_days,
            "upcoming_days": self.upcoming_days,
            "min_relevance_events": self.min_relevance_events,
            "min_outcome_samples": self.min_outcome_samples,
            "min_evaluation_samples": self.min_evaluation_samples,
            "max_events": self.max_events,
            "max_records": self.max_records,
            "max_outcomes": self.max_outcomes,
            "max_instruments": self.max_instruments,
            "stale_hours": self.stale_hours,
            "approaching_hours": self.approaching_hours,
            "volatility_importance_threshold": self.volatility_importance_threshold,
            "version": self.version,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> EventIntelligenceConfig:
        known = set(cls().to_dict())
        unknown = sorted(set(payload) - known)
        if unknown:
            raise EventIntelligenceValidationError(
                f"unknown configuration key(s): {', '.join(unknown)}"
            )
        base = cls().to_dict()
        base.update({key: value for key, value in payload.items() if key != "version"})
        return cls(
            lookback_days=int(base["lookback_days"]),
            upcoming_days=int(base["upcoming_days"]),
            min_relevance_events=int(base["min_relevance_events"]),
            min_outcome_samples=int(base["min_outcome_samples"]),
            min_evaluation_samples=int(base["min_evaluation_samples"]),
            max_events=int(base["max_events"]),
            max_records=int(base["max_records"]),
            max_outcomes=int(base["max_outcomes"]),
            max_instruments=int(base["max_instruments"]),
            stale_hours=float(base["stale_hours"]),
            approaching_hours=float(base["approaching_hours"]),
            volatility_importance_threshold=float(base["volatility_importance_threshold"]),
        )


@dataclass(frozen=True, slots=True)
class EventIntelligenceSources:
    """Read-only adapters into existing NovaMarket services."""

    events: Callable[..., Sequence[Mapping[str, Any]]] = lambda **_: ()
    event: Callable[[str], Mapping[str, Any]] = lambda _event_id: {}
    event_history: Callable[[str], Mapping[str, Any]] = lambda _event_id: {}
    instrument: Callable[[str], Mapping[str, Any]] = lambda _instrument_id: {}
    regime: Callable[[str], Mapping[str, Any] | None] = lambda _instrument_id: None
    bars: Callable[..., Sequence[Mapping[str, Any]]] = lambda **_: ()
    forecasts: Callable[..., Sequence[Mapping[str, Any]]] = lambda **_: ()
    evaluations: Callable[..., Sequence[Mapping[str, Any]]] = lambda **_: ()
    evaluation_records: Callable[..., Mapping[str, Any]] = lambda **_: {}
    portfolio: Callable[[str], Mapping[str, Any]] = lambda _portfolio_id: {}
    agents: Callable[..., Mapping[str, Any]] = lambda **_: {}


def event_rows(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in ("events", "items", "data", "results"):
            rows = value.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                return [row for row in rows if isinstance(row, Mapping)]
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def feature_value(value: object) -> tuple[float | None, str]:
    if value is None:
        return None, "missing"
    parsed = number(value)
    if parsed is None:
        return None, "invalid"
    return parsed, "value"


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def days_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86_400.0


__all__ = [
    "CORE_EVENT_FLAGS",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MIN_OUTCOME_SAMPLES",
    "DEFAULT_UPCOMING_DAYS",
    "EVENT_GROUPS",
    "EXECUTION_AUTHORITY",
    "EventGroup",
    "EventIntelligenceConfig",
    "EventIntelligenceError",
    "EventIntelligenceNotFoundError",
    "EventIntelligenceSources",
    "EventIntelligenceStateError",
    "EventIntelligenceUnavailableError",
    "EventIntelligenceValidationError",
    "EventStatus",
    "FEATURE_VERSION",
    "HORIZON_DAYS",
    "HORIZONS",
    "IMPORTANCE_VALUE",
    "ImpactStatus",
    "MAX_EVENTS",
    "MAX_INSTRUMENTS",
    "MAX_OUTCOMES",
    "MAX_RECORDS",
    "MAX_TEXT",
    "OUTCOME_POLICY_VERSION",
    "EVALUATION_POLICY_VERSION",
    "OutcomeStatus",
    "PI24_CONTRACT_VERSION",
    "PI24_PHASE",
    "PI24_SCHEMA_VERSION",
    "PI24_VERSION",
    "RELEVANCE_ORDER",
    "RELEVANCE_POLICY_VERSION",
    "RISK_AUTHORITY",
    "RelevanceClass",
    "WARNING_POLICY_VERSION",
    "WarningStatus",
    "canonical_horizon",
    "canonical_instrument",
    "clamp",
    "clean",
    "days_between",
    "digest",
    "event_group",
    "event_rows",
    "feature_value",
    "importance_value",
    "instruction_risk",
    "number",
    "parse_time",
    "stable_id",
    "utcnow",
]
