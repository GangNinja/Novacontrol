"""PI-12 forecast-driven decisions over the accepted AT-03 pipeline.

This module interprets *stored* PI-08/PI-10 forecasts and produces an auditable
BUY, SELL, HOLD, WAIT or NO_ACTION decision.  It is deliberately not a second
forecast, risk, execution, portfolio or autonomy authority:

* forecasts are read from the existing prediction stores;
* an actionable decision is represented as an ordinary AT-03 signal/proposal;
* NM-08 remains the only risk authority;
* NM-10 remains the only paper-execution authority.

The evaluator is pure and deterministic: the only clock it uses is injected.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

FORECAST_DECISION_CONTRACT_VERSION = "pi12-forecast-decision-1.0"
FORECAST_DECISION_PHASE = "PI-12"

HORIZONS: tuple[str, ...] = (
    "1m",
    "5m",
    "15m",
    "20m",
    "1h",
    "1d",
    "3d",
    "5d",
    "30d",
    "90d",
)
_INTRADAY = frozenset({"1m", "5m", "15m", "20m", "1h"})
_SWING = frozenset({"1d", "3d", "5d"})
_LONG = frozenset({"30d", "90d"})

#: Risk-engine forecast labels are NM-06's.  PI-12 maps a forecast *for the
#: policy horizon* to the corresponding label; it never asks NM-08 to accept an
#: incompatible intraday forecast as a swing forecast.
RISK_HORIZON_MAP: dict[str, str] = {
    "3d": "few_sessions",
    "5d": "one_week",
    "30d": "one_month",
    "90d": "three_months",
}

#: Canonical NM-06 horizon labels.  PI-12 asks in interval form (``5d``) while
#: NM-06 registers models under label form (``one_week``).  Both sides resolve
#: through this one mapping, so a model is found by translation, never by a
#: prefix guess or by reinterpreting a different horizon.  Unknown values stay
#: as given and therefore match nothing.
HORIZON_CANONICAL: dict[str, str] = {
    "1d": "next_session",
    "3d": "few_sessions",
    "5d": "one_week",
    "30d": "one_month",
    "90d": "three_months",
    "next_session": "next_session",
    "few_sessions": "few_sessions",
    "one_week": "one_week",
    "one_month": "one_month",
    "three_months": "three_months",
}


def canonical_horizon(value: Any) -> str:
    """The NM-06 label for a PI-12 interval label (or itself when unknown)."""

    text = str(value or "").strip().lower()
    return HORIZON_CANONICAL.get(text, text)

UNCERTAINTY_RANK = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
_DECISION_TYPES = frozenset({"BUY", "SELL", "HOLD", "WAIT", "NO_ACTION"})
_VALID_PORTFOLIO_HORIZONS = frozenset({"short_term", "medium_term", "long_term"})
_ALLOWED_VALIDITY = frozenset({"valid", "confirmed"})
_ALLOWED_QUALITY = frozenset({"valid", "warning"})
_ALLOWED_FRESHNESS = frozenset({"fresh", "recent"})
_VERIFIED_EVENTS = frozenset(
    {
        "confirmed",
        "officially_confirmed",
        "verified",
        "validated",
        "corroborated",
        "multi_source_corroborated",
    }
)

_DEFAULT_HORIZONS: dict[str, tuple[str, ...]] = {
    "short_term": ("5m", "15m", "20m", "1h", "1d", "3d"),
    "medium_term": ("1d", "3d", "5d", "30d"),
    "long_term": ("30d", "90d"),
}
_DEFAULT_PRIORITY: dict[str, tuple[str, ...]] = {
    "short_term": ("1h", "5m", "15m", "20m", "1d", "3d"),
    "medium_term": ("5d", "3d", "1d", "30d"),
    "long_term": ("90d", "30d"),
}
_DEFAULT_AGE_HOURS: dict[str, float] = {
    "1m": 1.0,
    "5m": 2.0,
    "15m": 2.0,
    "20m": 2.0,
    "1h": 4.0,
    "1d": 24.0,
    "3d": 36.0,
    "5d": 48.0,
    "30d": 120.0,
    "90d": 240.0,
}
_DEFAULT_UNCERTAINTY: dict[str, str] = {
    "1m": "low",
    "5m": "low",
    "15m": "medium",
    "20m": "medium",
    "1h": "medium",
    "1d": "medium",
    "3d": "medium",
    "5d": "medium",
    "30d": "high",
    "90d": "high",
}


class ForecastDecisionType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"
    NO_ACTION = "NO_ACTION"


class ForecastDecisionStatus(StrEnum):
    DECISION_GENERATED = "decision_generated"
    RISK_PENDING = "risk_pending"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    RISK_RESTRICTED = "risk_restricted"
    RISK_REVIEW = "risk_review"
    PROPOSAL_SKIPPED = "proposal_skipped"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_ACCEPTED = "order_accepted"
    EXECUTION_PREFLIGHT_FAILED = "execution_preflight_failed"
    EXECUTION_SUBMISSION_FAILED = "execution_submission_failed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    RECONCILIATION_PENDING = "reconciliation_pending"
    EXPIRED = "expired"
    OUTCOME_RECORDED = "outcome_recorded"
    DECISION_ERROR = "decision_error"


class ForecastDecisionConfigError(ValueError):
    """A PI-12 policy cannot be interpreted safely."""


class ForecastEvidenceUnavailable(RuntimeError):
    """Required PI-08 forecast evidence could not be read; no trade is allowed."""


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ForecastDecisionConfigError(f"{name} must be a decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ForecastDecisionConfigError(f"{name} must be a decimal number") from exc
    if parsed.is_nan() or parsed.is_infinite():
        raise ForecastDecisionConfigError(f"{name} must be finite")
    return parsed


def _bounded_float(value: Any, name: str, low: float, high: float) -> float:
    parsed = float(_decimal(value, name))
    if parsed < low or parsed > high:
        raise ForecastDecisionConfigError(
            f"{name} must be between {low} and {high}"
        )
    return parsed


def _horizon_tuple(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, Sequence):
        raw = [str(item) for item in value]
    else:
        raise ForecastDecisionConfigError(f"{name} must be a horizon list")
    if not raw:
        raise ForecastDecisionConfigError(f"{name} must not be empty")
    unknown = [item for item in raw if item not in HORIZONS]
    if unknown:
        raise ForecastDecisionConfigError(f"{name} has unknown horizons {unknown}")
    return tuple(dict.fromkeys(raw))


@dataclass(frozen=True, slots=True)
class ForecastDecisionConfig:
    """Versioned selection and reliability policy for forecast decisions."""

    horizons: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(_DEFAULT_HORIZONS)
    )
    priority: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(_DEFAULT_PRIORITY)
    )
    min_expected_return: float = 0.015
    min_probability: float = 0.55
    max_disagreement: float = 0.35
    max_uncertainty_by_horizon: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_UNCERTAINTY)
    )
    max_age_hours_by_horizon: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_AGE_HOURS)
    )
    require_calibrated_probability: bool = True
    require_final_stage: bool = True
    conflict_policy: str = "wait"
    allow_add_to_existing: bool = False
    require_technical_confirmation: bool = False
    block_on_verified_event: bool = True
    decision_ttl_minutes: int = 120
    version: str = "pi12-policy-1.0"

    def __post_init__(self) -> None:
        unknown_horizons = set(self.horizons) - _VALID_PORTFOLIO_HORIZONS
        if unknown_horizons:
            raise ForecastDecisionConfigError(
                f"unknown portfolio horizons: {sorted(unknown_horizons)}"
            )
        unknown_priority = set(self.priority) - _VALID_PORTFOLIO_HORIZONS
        if unknown_priority:
            raise ForecastDecisionConfigError(
                f"unknown priority horizons: {sorted(unknown_priority)}"
            )
        policies: dict[str, tuple[str, ...]] = {}
        for key in _VALID_PORTFOLIO_HORIZONS:
            if key not in self.horizons:
                raise ForecastDecisionConfigError(f"horizons.{key} is required")
            selected = _horizon_tuple(self.horizons[key], f"horizons.{key}")
            policies[key] = selected
        object.__setattr__(self, "horizons", policies)
        priorities: dict[str, tuple[str, ...]] = {}
        for key, selected in policies.items():
            configured = _horizon_tuple(
                self.priority.get(key, selected), f"priority.{key}"
            )
            if set(configured) != set(selected):
                raise ForecastDecisionConfigError(
                    f"priority.{key} must contain exactly the selected horizons"
                )
            priorities[key] = configured
        object.__setattr__(self, "priority", priorities)
        for name in ("min_expected_return",):
            value = _bounded_float(getattr(self, name), name, 0.0, 1.0)
            object.__setattr__(self, name, value)
        probability = _bounded_float(self.min_probability, "min_probability", 0.5, 1.0)
        object.__setattr__(self, "min_probability", probability)
        disagreement = _bounded_float(
            self.max_disagreement, "max_disagreement", 0.0, 1.0
        )
        object.__setattr__(self, "max_disagreement", disagreement)
        uncertainty: dict[str, str] = {}
        for horizon in HORIZONS:
            label = str(
                self.max_uncertainty_by_horizon.get(
                    horizon, _DEFAULT_UNCERTAINTY[horizon]
                )
            ).lower()
            if label not in {"low", "medium", "high"}:
                raise ForecastDecisionConfigError(
                    f"max_uncertainty_by_horizon.{horizon} must be low, medium or high"
                )
            uncertainty[horizon] = label
        object.__setattr__(self, "max_uncertainty_by_horizon", uncertainty)
        ages: dict[str, float] = {}
        for horizon in HORIZONS:
            age = _bounded_float(
                self.max_age_hours_by_horizon.get(
                    horizon, _DEFAULT_AGE_HOURS[horizon]
                ),
                f"max_age_hours_by_horizon.{horizon}",
                0.01,
                8760.0,
            )
            ages[horizon] = age
        object.__setattr__(self, "max_age_hours_by_horizon", ages)
        policy = str(self.conflict_policy or "").lower()
        if policy not in {"wait", "prefer_primary", "no_action"}:
            raise ForecastDecisionConfigError(
                "conflict_policy must be wait, prefer_primary or no_action"
            )
        object.__setattr__(self, "conflict_policy", policy)
        ttl = int(self.decision_ttl_minutes)
        if ttl < 1 or ttl > 10080:
            raise ForecastDecisionConfigError(
                "decision_ttl_minutes must be between 1 and 10080"
            )
        object.__setattr__(self, "decision_ttl_minutes", ttl)
        if not str(self.version or "").strip():
            raise ForecastDecisionConfigError("version is required")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ForecastDecisionConfig":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise ForecastDecisionConfigError("forecast policy must be an object")
        raw = dict(payload)
        horizons = raw.get("horizons")
        if isinstance(horizons, Mapping):
            raw["horizons"] = {
                key: _horizon_tuple(value, f"horizons.{key}")
                for key, value in horizons.items()
            }
        priority = raw.get("priority")
        if isinstance(priority, Mapping):
            raw["priority"] = {
                key: _horizon_tuple(value, f"priority.{key}")
                for key, value in priority.items()
            }
        for mapping_key in (
            "max_uncertainty_by_horizon",
            "max_age_hours_by_horizon",
        ):
            value = raw.get(mapping_key)
            if value is not None and not isinstance(value, Mapping):
                raise ForecastDecisionConfigError(f"{mapping_key} must be an object")
        allowed = {
            "horizons",
            "priority",
            "min_expected_return",
            "min_probability",
            "max_disagreement",
            "max_uncertainty_by_horizon",
            "max_age_hours_by_horizon",
            "require_calibrated_probability",
            "require_final_stage",
            "conflict_policy",
            "allow_add_to_existing",
            "require_technical_confirmation",
            "block_on_verified_event",
            "decision_ttl_minutes",
            "version",
        }
        # ``to_dict`` carries descriptive metadata that is not part of the
        # policy itself.  Accepting it makes a GET response round-trippable and
        # lets a stored policy survive a restart without being mistaken for an
        # unknown field (which would silently fall back to defaults).
        metadata = {
            "phase",
            "contract_version",
            "automatic_promotion",
            "risk_authority",
            "execution_authority",
            "updated_at",
        }
        unknown = set(raw) - allowed - metadata
        if unknown:
            raise ForecastDecisionConfigError(
                f"unknown forecast policy fields: {sorted(unknown)}"
            )
        for key in metadata:
            raw.pop(key, None)
        booleans = {
            "require_calibrated_probability",
            "require_final_stage",
            "allow_add_to_existing",
            "require_technical_confirmation",
            "block_on_verified_event",
        }
        for key in booleans:
            if key in raw and not isinstance(raw[key], bool):
                raise ForecastDecisionConfigError(f"{key} must be boolean")
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": FORECAST_DECISION_PHASE,
            "contract_version": FORECAST_DECISION_CONTRACT_VERSION,
            "version": self.version,
            "horizons": {key: list(value) for key, value in self.horizons.items()},
            "priority": {key: list(value) for key, value in self.priority.items()},
            "min_expected_return": self.min_expected_return,
            "min_probability": self.min_probability,
            "max_disagreement": self.max_disagreement,
            "max_uncertainty_by_horizon": dict(self.max_uncertainty_by_horizon),
            "max_age_hours_by_horizon": dict(self.max_age_hours_by_horizon),
            "require_calibrated_probability": self.require_calibrated_probability,
            "require_final_stage": self.require_final_stage,
            "conflict_policy": self.conflict_policy,
            "allow_add_to_existing": self.allow_add_to_existing,
            "require_technical_confirmation": self.require_technical_confirmation,
            "block_on_verified_event": self.block_on_verified_event,
            "decision_ttl_minutes": self.decision_ttl_minutes,
            "automatic_promotion": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }

    def selected_horizons(self, portfolio_horizon: str) -> tuple[str, ...]:
        return tuple(self.horizons.get(portfolio_horizon, ()))

    def priority_for(self, portfolio_horizon: str) -> tuple[str, ...]:
        return tuple(self.priority.get(portfolio_horizon, ()))

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalized_forecast(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize PI-08/PI-10 forecast output without inventing fields."""

    horizon = _text(row.get("forecast_horizon")) or _text(row.get("horizon"))
    direction = _text(row.get("predicted_direction")) or _text(row.get("direction"))
    return {
        "forecast_id": _text(row.get("forecast_id")),
        "lifecycle_id": _text(row.get("lifecycle_id")),
        "lifecycle_stage": _text(row.get("lifecycle_stage")),
        "instrument_id": _text(row.get("instrument_id")),
        "exchange": _text(row.get("exchange")),
        "horizon": horizon,
        "direction": direction,
        "predicted_return": _number(
            row.get("predicted_return", row.get("expected_return"))
        ),
        "predicted_price": _text(row.get("predicted_price")),
        "reference_price": _text(row.get("reference_price")),
        "lower_return": _number(row.get("lower_return")),
        "upper_return": _number(row.get("upper_return")),
        "p_up": _number(row.get("p_up", row.get("probability_up"))),
        "p_down": _number(row.get("p_down")),
        "p_neutral": _number(row.get("p_neutral")),
        "probability_status": _text(row.get("probability_status")),
        "calibration_status": _text(row.get("calibration_status")),
        "uncertainty": _text(row.get("forecast_uncertainty"))
        or _text(row.get("uncertainty"))
        or "unknown",
        "uncertainty_width": _number(row.get("uncertainty_width")),
        "disagreement": _number(
            row.get("disagreement", row.get("ensemble_disagreement"))
        ),
        "model_id": _text(row.get("model_id")),
        "model_version": _text(row.get("model_version")),
        "ensemble_id": _text(row.get("ensemble_id")),
        "member_versions": [
            str(item) for item in row.get("member_versions") or ()
        ],
        "feature_set_version": _text(row.get("feature_set_version")),
        "dataset_version": _text(row.get("dataset_version")),
        "validity": _text(
            row.get("validity") or row.get("forecast_validity")
        ),
        "data_freshness": _text(
            row.get("data_freshness") or row.get("data_freshness_status")
        ),
        "data_quality": _text(row.get("data_quality")),
        "data_snapshot_id": _text(row.get("data_snapshot_id")),
        "generated_at": _text(
            row.get("generated_at") or row.get("forecast_generation_timestamp")
        ),
        "data_observed_at": _text(
            row.get("data_observed_at") or row.get("underlying_data_timestamp")
        ),
        "target_at": _text(
            row.get("target_at") or row.get("forecast_target_timestamp")
        ),
        "reference_at": _text(
            row.get("reference_at") or row.get("reference_price_timestamp")
        ),
        "validity_conditions": [
            str(item)
            for item in (
                row.get("validity_conditions")
                or row.get("forecast_validity_conditions")
                or ()
            )
        ],
        "digest": _text(row.get("digest")),
        "reasons": [str(item) for item in row.get("reasons") or ()],
        "pi10_context": dict(row.get("pi10_context") or {})
        if isinstance(row.get("pi10_context"), Mapping)
        else {},
    }


def _technical(bars: Sequence[Any]) -> dict[str, Any]:
    rows: list[Mapping[str, Any]] = []
    for bar in bars:
        if isinstance(bar, Mapping):
            rows.append(bar)
        else:
            rows.append(
                {
                    "close": getattr(bar, "close", None),
                    "high": getattr(bar, "high", None),
                    "low": getattr(bar, "low", None),
                    "volume": getattr(bar, "volume", None),
                    "observed_at": getattr(bar, "observed_at", None),
                }
            )
    closes = [value for row in rows if (value := _number(row.get("close"))) is not None]
    if not closes:
        return {"available": False, "reason": "no market bars are available"}
    latest = closes[-1]
    sma20 = sum(closes[-20:]) / min(len(closes), 20)
    sma5 = sum(closes[-5:]) / min(len(closes), 5)
    change_5 = (latest / closes[-5] - 1.0) if len(closes) >= 5 and closes[-5] else 0.0
    ranges = [
        max(0.0, (_number(row.get("high")) or 0.0) - (_number(row.get("low")) or 0.0))
        for row in rows[-14:]
    ]
    atr_pct = (sum(ranges) / len(ranges) / latest) if ranges and latest else 0.0
    volumes = [
        value for row in rows[-20:] if (value := _number(row.get("volume"))) is not None
    ]
    median_volume = sorted(volumes)[len(volumes) // 2] if volumes else 0.0
    return {
        "available": True,
        "last_close": latest,
        "sma5": round(sma5, 6),
        "sma20": round(sma20, 6),
        "above_sma20": latest > sma20,
        "change_5_bars": round(change_5, 6),
        "atr_pct": round(atr_pct, 6),
        "median_volume": round(median_volume, 2),
        "observation_count": len(closes),
        "method": "deterministic bars; descriptive, not a forecast",
    }


def _regime_context(regime: Any) -> dict[str, Any]:
    if regime is None:
        return {"available": False}
    body = regime.to_dict() if hasattr(regime, "to_dict") else regime
    if not isinstance(body, Mapping):
        return {"available": False}
    return {
        "available": True,
        "regime_id": _text(body.get("regime_id")),
        "primary": _text(body.get("primary_regime")) or _text(body.get("primary")),
        "secondary": [str(item) for item in body.get("secondary") or ()],
        "confidence": _number(body.get("confidence")),
        "as_of": _text(body.get("as_of")),
        "method_version": _text(body.get("method_version")),
    }


def _event_context(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    verified = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if _text(event.get("verification")) not in _VERIFIED_EVENTS:
            continue
        verified.append(
            {
                "event_id": _text(event.get("event_id")),
                "category": _text(event.get("category")),
                "subcategory": _text(event.get("subcategory")),
                "importance": _text(event.get("importance")),
                "verification": _text(event.get("verification")),
                "headline": _text(event.get("headline") or event.get("title")),
                "published_at": _text(event.get("published_at")),
                "source_ids": [str(item) for item in event.get("source_ids") or ()],
            }
        )
    high = [
        row
        for row in verified
        if row["importance"] in {"high", "critical"}
    ]
    return {
        "available": bool(verified),
        "verified": verified[:20],
        "high_impact": high[:10],
        "unverified_excluded": max(0, len(events) - len(verified)),
    }


@dataclass(frozen=True, slots=True)
class ForecastDecisionDraft:
    """The immutable reasoning result of one candidate evaluation."""

    decision_type: ForecastDecisionType
    horizon: str
    primary_forecast: Mapping[str, Any] | None
    forecasts: tuple[Mapping[str, Any], ...]
    reasons: tuple[str, ...]
    contradictions: tuple[str, ...]
    observations: tuple[str, ...]
    model_conclusions: tuple[str, ...]
    portfolio_context: Mapping[str, Any]
    technical_context: Mapping[str, Any]
    regime_context: Mapping[str, Any]
    event_context: Mapping[str, Any]
    evaluation_context: Mapping[str, Any]
    horizon_evidence: tuple[Mapping[str, Any], ...]
    expected_return: float | None
    uncertainty: str
    disagreement: float | None
    probability_up: float | None
    probability_down: float | None
    reference_price: str
    source_observed_at: str
    target_at: str
    action: str
    side: str
    policy_version: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_type": self.decision_type.value,
            "horizon": self.horizon,
            "primary_forecast": dict(self.primary_forecast or {}),
            "forecasts": [dict(row) for row in self.forecasts],
            "reasons": list(self.reasons),
            "contradictions": list(self.contradictions),
            "observations": list(self.observations),
            "model_conclusions": list(self.model_conclusions),
            "portfolio_context": dict(self.portfolio_context),
            "technical_context": dict(self.technical_context),
            "regime_context": dict(self.regime_context),
            "event_context": dict(self.event_context),
            "evaluation_context": dict(self.evaluation_context),
            "horizon_evidence": [dict(row) for row in self.horizon_evidence],
            "expected_return": self.expected_return,
            "uncertainty": self.uncertainty,
            "disagreement": self.disagreement,
            "probability_up": self.probability_up,
            "probability_down": self.probability_down,
            "reference_price": self.reference_price,
            "source_observed_at": self.source_observed_at,
            "target_at": self.target_at,
            "action": self.action,
            "side": self.side,
            "policy_version": self.policy_version,
            "automatic_promotion": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }


@dataclass(frozen=True, slots=True)
class _ForecastAssessment:
    forecast: dict[str, Any]
    eligible: bool
    direction: str
    reliability: str
    failures: tuple[str, ...]


class ForecastDecisionEvaluator:
    """Convert stored forecasts plus portfolio context into one decision."""

    def __init__(self, config: ForecastDecisionConfig | None = None) -> None:
        self.config = config or ForecastDecisionConfig()

    def evaluate(
        self,
        *,
        instrument_id: str,
        exchange: str,
        portfolio: Mapping[str, Any],
        forecasts: Sequence[Mapping[str, Any]],
        bars: Sequence[Any] = (),
        regime: Any = None,
        events: Sequence[Mapping[str, Any]] = (),
        open_orders: Sequence[Mapping[str, Any]] = (),
        evaluation_evidence: Mapping[str, Any] | None = None,
        candidate_quality: str = "good",
        now: datetime | None = None,
    ) -> ForecastDecisionDraft:
        current = (now or datetime.now(tz=UTC)).astimezone(UTC)
        portfolio_horizon = _text(portfolio.get("horizon"))
        selected = self.config.selected_horizons(portfolio_horizon)
        priority = self.config.priority_for(portfolio_horizon)
        technical = _technical(bars)
        regime_body = _regime_context(regime)
        event_body = _event_context(events)
        portfolio_body = self._portfolio_context(
            portfolio, instrument_id, open_orders
        )
        evaluation_body = dict(evaluation_evidence or {"available": False})
        normalized = [normalized_forecast(row) for row in forecasts]
        by_horizon: dict[str, list[dict[str, Any]]] = {}
        for row in normalized:
            if row["horizon"] in selected and row["forecast_id"]:
                by_horizon.setdefault(row["horizon"], []).append(row)
        assessments: list[_ForecastAssessment] = []
        for horizon in selected:
            rows = by_horizon.get(horizon, [])
            if not rows:
                continue
            latest = max(
                rows,
                key=lambda item: (
                    _parse_time(item["generated_at"]) or datetime.min.replace(tzinfo=UTC)
                ),
            )
            assessments.append(self._assess(latest, current))
        eligible = [row for row in assessments if row.eligible]
        direction_rows = [
            row for row in eligible if row.direction in {"up", "down", "neutral"}
        ]
        directions = {row.direction for row in direction_rows}
        conflict = "up" in directions and "down" in directions
        evidence = tuple(
            {
                "forecast_id": item.forecast["forecast_id"],
                "horizon": item.forecast["horizon"],
                "direction": item.direction,
                "expected_return": item.forecast["predicted_return"],
                "uncertainty": item.forecast["uncertainty"],
                "disagreement": item.forecast["disagreement"],
                "eligible": item.eligible,
                "reliability": item.reliability,
                "target_at": item.forecast["target_at"],
                "failures": list(item.failures),
            }
            for item in assessments
        )
        assess_by_horizon = {item.forecast["horizon"]: item for item in assessments}
        chosen: _ForecastAssessment | None = None
        if not conflict or self.config.conflict_policy == "prefer_primary":
            for horizon in priority:
                if horizon not in selected:
                    continue
                candidate = assess_by_horizon.get(horizon)
                if candidate is None or not candidate.eligible:
                    continue
                if candidate.direction in {"up", "down", "neutral"}:
                    chosen = candidate
                    break

        primary = chosen.forecast if chosen else self._primary_unavailable(
            assessments, priority
        )
        expected_return = primary.get("predicted_return") if primary else None
        uncertainty = _text(primary.get("uncertainty")) if primary else "unknown"
        disagreement = _number(primary.get("disagreement")) if primary else None
        p_up = _number(primary.get("p_up")) if primary else None
        p_down = _number(primary.get("p_down")) if primary else None
        reference_price = _text(primary.get("reference_price")) if primary else ""
        source_observed = _text(primary.get("data_observed_at")) if primary else ""
        target_at = _text(primary.get("target_at")) if primary else ""
        observations: list[str] = []
        contradictions: list[str] = []
        reasons: list[str] = []
        model_conclusions: list[str] = []
        decision = ForecastDecisionType.NO_ACTION
        action = ""
        side = ""

        holding = portfolio_body.get("holding")
        if portfolio_body.get("open_order"):
            decision = ForecastDecisionType.WAIT
            reasons.append(
                "an existing paper order for this instrument is not reconciled; "
                "a new decision waits for NM-10"
            )
        elif not assessments:
            decision = ForecastDecisionType.NO_ACTION
            reasons.append(
                "no stored forecast exists for the configured horizons "
                f"({', '.join(selected)})"
            )
        elif conflict and self.config.conflict_policy != "prefer_primary":
            decision = (
                ForecastDecisionType.NO_ACTION
                if self.config.conflict_policy == "no_action"
                else ForecastDecisionType.WAIT
            )
            reasons.append(
                "eligible horizons disagree about direction; incompatible evidence "
                "is not merged"
            )
            contradictions.extend(
                f"{item.forecast['horizon']} {item.direction}"
                for item in direction_rows
            )
        elif candidate_quality not in {"good", "partial"}:
            decision = ForecastDecisionType.WAIT
            reasons.append(
                f"candidate market-data quality {candidate_quality!r} is not eligible"
            )
        elif chosen is None:
            decision = (
                ForecastDecisionType.HOLD
                if holding is not None
                else ForecastDecisionType.WAIT
            )
            reasons.append(
                "no configured horizon has a reliable, actionable forecast"
            )
            contradictions.extend(
                failure
                for item in assessments
                for failure in item.failures
            )
        else:
            model_conclusions.append(
                f"the selected {chosen.forecast['horizon']} forecast expects "
                f"{chosen.direction} with return {expected_return}"
            )
            if technical.get("available"):
                observations.append(
                    f"last close {technical['last_close']} versus SMA20 "
                    f"{technical['sma20']}"
                )
            if regime_body.get("available"):
                observations.append(
                    f"market regime {regime_body.get('primary') or 'unclassified'}"
                )
            if event_body.get("available"):
                observations.append(
                    f"{len(event_body.get('verified') or ())} verified event(s) available"
                )
            if evaluation_body.get("available"):
                observations.append(
                    "PI-11 historical evaluation evidence is attached by reference"
                )
            if chosen.direction == "up":
                if holding is not None and not self.config.allow_add_to_existing:
                    decision = ForecastDecisionType.HOLD
                    reasons.append(
                        "an existing long position is retained; automatic add-on "
                        "exposure is disabled"
                    )
                elif self._technical_contradicts("up", technical):
                    contradictions.append(
                        "price is below its SMA20 despite the upward forecast"
                    )
                    if self.config.require_technical_confirmation:
                        decision = ForecastDecisionType.WAIT
                        reasons.append(
                            "the configured technical confirmation is absent"
                        )
                    else:
                        decision = self._entry_decision(
                            portfolio_body, ForecastDecisionType.BUY, reasons
                        )
                else:
                    decision = self._entry_decision(
                        portfolio_body, ForecastDecisionType.BUY, reasons
                    )
                if decision is ForecastDecisionType.BUY:
                    action, side = "add_position", "buy"
                    reasons.append(
                        f"valid upward {chosen.forecast['horizon']} forecast "
                        f"exceeds the {self.config.min_expected_return:.1%} threshold"
                    )
                    if event_body.get("high_impact") and self.config.block_on_verified_event:
                        decision = ForecastDecisionType.WAIT
                        action = side = ""
                        reasons.append(
                            "a verified high-impact event blocks a new entry until reviewed"
                        )
            elif chosen.direction == "down":
                if holding is None:
                    decision = ForecastDecisionType.NO_ACTION
                    reasons.append(
                        "a downward forecast does not create a short position"
                    )
                elif event_body.get("high_impact") and self.config.block_on_verified_event:
                    decision = ForecastDecisionType.WAIT
                    reasons.append(
                        "a verified high-impact event defers the exit decision"
                    )
                else:
                    decision = ForecastDecisionType.SELL
                    action, side = "reduce_position", "sell"
                    reasons.append(
                        f"valid downward {chosen.forecast['horizon']} forecast "
                        "applies to an existing long position"
                    )
            else:
                decision = (
                    ForecastDecisionType.HOLD
                    if holding is not None
                    else ForecastDecisionType.NO_ACTION
                )
                reasons.append(
                    "the selected forecast is neutral; no entry or exit is justified"
                )
            if holding is None and portfolio_body.get("cash", 0.0) <= 0 and decision is ForecastDecisionType.BUY:
                decision = ForecastDecisionType.WAIT
                action = side = ""
                reasons.append("the portfolio has no available virtual cash")

        if not reasons:
            reasons.append("no eligible trading action is justified")
        if holding is not None:
            model_conclusions.append(
                f"existing long position {holding.get('quantity')} shares is in scope"
            )
        if expected_return is not None and decision in {
            ForecastDecisionType.HOLD,
            ForecastDecisionType.WAIT,
            ForecastDecisionType.NO_ACTION,
        }:
            contradictions.append(
                "forecast evidence alone did not satisfy the configured action policy"
            )
        return ForecastDecisionDraft(
            decision_type=decision,
            horizon=_text(primary.get("horizon")) if primary else "",
            primary_forecast=primary,
            forecasts=tuple(row.forecast for row in eligible) or tuple(
                item.forecast for item in assessments[:10]
            ),
            reasons=tuple(dict.fromkeys(reasons)),
            contradictions=tuple(dict.fromkeys(contradictions)),
            observations=tuple(dict.fromkeys(observations)),
            model_conclusions=tuple(dict.fromkeys(model_conclusions)),
            portfolio_context=portfolio_body,
            technical_context=technical,
            regime_context=regime_body,
            event_context=event_body,
            evaluation_context=evaluation_body,
            horizon_evidence=evidence,
            expected_return=expected_return,
            uncertainty=uncertainty,
            disagreement=disagreement,
            probability_up=p_up,
            probability_down=p_down,
            reference_price=reference_price,
            source_observed_at=source_observed,
            target_at=target_at,
            action=action,
            side=side,
            policy_version=self.config.version,
        )

    def _entry_decision(
        self,
        portfolio: Mapping[str, Any],
        preferred: ForecastDecisionType,
        reasons: list[str],
    ) -> ForecastDecisionType:
        if portfolio.get("holding") is not None:
            return ForecastDecisionType.HOLD
        if int(portfolio.get("open_positions") or 0) >= int(
            portfolio.get("max_positions") or 0
        ) and int(portfolio.get("max_positions") or 0) > 0:
            reasons.append("the horizon position cap is already reached")
            return ForecastDecisionType.WAIT
        if float(portfolio.get("cash") or 0.0) <= 0:
            reasons.append("the portfolio has no available virtual cash")
            return ForecastDecisionType.WAIT
        return preferred

    @staticmethod
    def _technical_contradicts(direction: str, technical: Mapping[str, Any]) -> bool:
        if not technical.get("available"):
            return False
        if direction == "up":
            return technical.get("above_sma20") is False
        if direction == "down":
            return technical.get("above_sma20") is True
        return False

    def _primary_unavailable(
        self, assessments: Sequence[_ForecastAssessment], priority: Sequence[str]
    ) -> dict[str, Any] | None:
        for horizon in priority:
            for item in assessments:
                if item.forecast.get("horizon") == horizon:
                    return item.forecast
        return assessments[0].forecast if assessments else None

    def _assess(
        self, forecast: Mapping[str, Any], now: datetime
    ) -> _ForecastAssessment:
        horizon = _text(forecast.get("horizon"))
        failures: list[str] = []
        stage = _text(forecast.get("lifecycle_stage"))
        if self.config.require_final_stage and stage != "t_minus_1":
            failures.append(
                f"{horizon} forecast stage {stage or 'missing'} is not final"
            )
        validity = _text(forecast.get("validity"))
        if validity not in _ALLOWED_VALIDITY:
            failures.append(
                f"{horizon} forecast validity {validity or 'missing'} is not actionable"
            )
        quality = _text(forecast.get("data_quality"))
        if quality not in _ALLOWED_QUALITY:
            failures.append(
                f"{horizon} forecast data quality {quality or 'missing'} is not valid"
            )
        freshness = _text(forecast.get("data_freshness"))
        if freshness not in _ALLOWED_FRESHNESS:
            failures.append(
                f"{horizon} forecast data freshness {freshness or 'missing'} is not fresh"
            )
        generated = _parse_time(forecast.get("generated_at"))
        target = _parse_time(forecast.get("target_at"))
        if generated is None:
            failures.append(f"{horizon} forecast has no readable generation time")
        else:
            age_hours = (now - generated).total_seconds() / 3600.0
            limit = float(self.config.max_age_hours_by_horizon.get(horizon, 24.0))
            if age_hours < -0.1:
                failures.append(f"{horizon} forecast generation time is in the future")
            if age_hours > limit:
                failures.append(
                    f"{horizon} forecast is {age_hours:.1f}h old; the policy allows {limit:.0f}h"
                )
        if target is None:
            failures.append(f"{horizon} forecast has no readable target time")
        elif target <= now:
            failures.append(f"{horizon} forecast target has expired")
        uncertainty = _text(forecast.get("uncertainty")) or "unknown"
        allowed_uncertainty = self.config.max_uncertainty_by_horizon.get(horizon, "medium")
        if UNCERTAINTY_RANK.get(uncertainty, 9) > UNCERTAINTY_RANK.get(
            allowed_uncertainty, 1
        ):
            failures.append(
                f"{horizon} forecast uncertainty {uncertainty} exceeds "
                f"{allowed_uncertainty}"
            )
        disagreement = _number(forecast.get("disagreement"))
        if disagreement is not None and disagreement > self.config.max_disagreement:
            failures.append(
                f"{horizon} model disagreement {disagreement:.3f} exceeds "
                f"{self.config.max_disagreement:.3f}"
            )
        direction = _text(forecast.get("direction"))
        if direction not in {"up", "down", "neutral", "uncertain", "invalid"}:
            failures.append(f"{horizon} forecast direction is unreadable")
        if direction in {"uncertain", "invalid"}:
            failures.append(f"{horizon} forecast direction is {direction}")
        probability_status = _text(forecast.get("probability_status"))
        p_up = _number(forecast.get("p_up"))
        p_down = _number(forecast.get("p_down"))
        if p_up is not None and not 0.0 <= p_up <= 1.0:
            failures.append(f"{horizon} p_up is outside [0, 1]")
        if p_down is not None and not 0.0 <= p_down <= 1.0:
            failures.append(f"{horizon} p_down is outside [0, 1]")
        if (
            p_up is not None
            and p_down is not None
            and p_up + p_down > 1.0 + 1e-6
        ):
            failures.append(f"{horizon} directional probabilities sum above 1")
        if self.config.require_calibrated_probability:
            if probability_status != "calibrated":
                failures.append(
                    f"{horizon} probability status {probability_status or 'missing'} "
                    "is not calibrated"
                )
            if p_up is None or p_down is None:
                failures.append(f"{horizon} calibrated probabilities are missing")
            elif direction == "up" and p_up < self.config.min_probability:
                failures.append(
                    f"{horizon} p_up {p_up:.3f} is below {self.config.min_probability:.3f}"
                )
            elif direction == "down" and p_down < self.config.min_probability:
                failures.append(
                    f"{horizon} p_down {p_down:.3f} is below {self.config.min_probability:.3f}"
                )
        expected = _number(forecast.get("predicted_return"))
        if expected is None:
            failures.append(f"{horizon} expected return is missing")
        elif direction == "up" and expected <= 0:
            failures.append(f"{horizon} upward direction has a non-positive expected return")
        elif direction == "down" and expected >= 0:
            failures.append(f"{horizon} downward direction has a non-negative expected return")
        elif direction in {"up", "down"} and abs(expected) < self.config.min_expected_return:
            failures.append(
                f"{horizon} expected return {expected:.4f} is below "
                f"{self.config.min_expected_return:.4f}"
            )
        reference = _decimal_or_none(forecast.get("reference_price"))
        if reference is None or reference <= 0:
            failures.append(f"{horizon} reference price is missing or invalid")
        if not _text(forecast.get("forecast_id")):
            failures.append(f"{horizon} forecast has no identity")
        return _ForecastAssessment(
            forecast=dict(forecast),
            eligible=not failures,
            direction=direction,
            reliability=(
                "reliable"
                if not failures
                else "insufficient"
            ),
            failures=tuple(failures),
        )

    def _portfolio_context(
        self,
        portfolio: Mapping[str, Any],
        instrument_id: str,
        open_orders: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        detail = portfolio.get("detail") if isinstance(portfolio, Mapping) else None
        detail = detail if isinstance(detail, Mapping) else {}
        holding: dict[str, Any] | None = None
        for row in detail.get("holdings") or ():
            if isinstance(row, Mapping) and _text(row.get("instrument_id")) == instrument_id:
                holding = dict(row)
                break
        total = _number(portfolio.get("total_value")) or 0.0
        market_value = _number((holding or {}).get("market_value")) or 0.0
        cash = _number(detail.get("cash")) or 0.0
        open_positions = 0
        for row in detail.get("holdings") or ():
            if not isinstance(row, Mapping):
                continue
            quantity = _number(row.get("quantity"))
            value = _number(row.get("market_value"))
            if (quantity is not None and quantity > 0) or (
                quantity is None and value is not None and value > 0
            ):
                open_positions += 1
        orders = [dict(row) for row in open_orders if isinstance(row, Mapping)]
        pending = orders[0] if orders else None
        try:
            from novacontrol.novamarket.portfolio.models import policy_for

            max_positions = int(policy_for(_text(portfolio.get("horizon"))).max_positions)
        except Exception:  # noqa: BLE001 - risk still owns the final constraint
            max_positions = 0
        return {
            "portfolio_id": _text(portfolio.get("portfolio_id")),
            "horizon": _text(portfolio.get("horizon")),
            "total_value": total,
            "cash": cash,
            "holding": holding,
            "holding_quantity": (holding or {}).get("quantity") if holding else None,
            "holding_market_value": market_value if holding else 0.0,
            "sector": _text((holding or {}).get("sector")) if holding else "",
            "position_weight": round(market_value / total, 6) if total > 0 else 0.0,
            "unrealized_pnl": (holding or {}).get("unrealized_pnl") if holding else None,
            # The authoritative portfolio detail already computes concentration
            # and sector constraints.  Preserve them as read-only evidence; the
            # evaluator does not recalculate or override NM-08 policy.
            "constraints": [
                dict(row)
                for row in detail.get("constraints") or ()
                if isinstance(row, Mapping)
            ],
            "open_positions": open_positions,
            "max_positions": max_positions,
            "open_orders": orders,
            "open_order": pending,
            "pending_order_reference": (
                _text(pending.get("order_id") or pending.get("proposal_id"))
                if pending
                else ""
            ),
            "position_age_days": (holding or {}).get("holding_days") if holding else None,
            "duplicate_exposure": holding is not None,
        }


class ForecastDecisionSource:
    """Read-only bridge to the existing PI-08/PI-10/PI-11 read services."""

    def __init__(
        self,
        ensemble: Any,
        outlook: Any | None = None,
        evaluations: Any | None = None,
        *,
        clock: Any | None = None,
    ) -> None:
        self._ensemble = ensemble
        self._outlook = outlook
        self._evaluations = evaluations
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    def forecasts(self, instrument_id: str) -> list[dict[str, Any]]:
        cache_key = f"forecasts:{instrument_id}"
        cached = self._cache.get(cache_key)
        now = self._clock()
        if cached and (now - cached[0]).total_seconds() < 30:
            return [dict(row) for row in cached[1].get("forecasts") or ()]
        if self._ensemble is None:
            raise ForecastEvidenceUnavailable(
                "the PI-08 ensemble forecast source is unavailable"
            )
        rows: dict[str, dict[str, Any]] = {}
        outlook_context: dict[str, Any] = {}
        try:
            body = self._ensemble.forecasts(instrument_id=instrument_id, limit=200)
            if not isinstance(body, Mapping) or "forecasts" not in body:
                raise ForecastEvidenceUnavailable(
                    "the PI-08 ensemble forecast response has no readable forecast list"
                )
            for row in body.get("forecasts") or ():
                if isinstance(row, Mapping):
                    item = dict(row)
                    forecast_id = _text(item.get("forecast_id"))
                    if forecast_id:
                        rows[forecast_id] = item
        except ForecastEvidenceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - required evidence must fail closed
            raise ForecastEvidenceUnavailable(
                "the PI-08 ensemble forecast source could not be read: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if self._outlook is not None:
            try:
                outlook = self._outlook.latest(instrument_id)
                for item in outlook.get("results") or ():
                    if not isinstance(item, Mapping):
                        continue
                    forecast = item.get("forecast")
                    if not isinstance(forecast, Mapping):
                        continue
                    body_row = dict(forecast)
                    body_row["pi10_context"] = dict(item.get("context") or {})
                    body_row["pi10_horizon_status"] = item.get("status")
                    forecast_id = _text(body_row.get("forecast_id"))
                    if forecast_id:
                        rows[forecast_id] = body_row
                        outlook_context[_text(item.get("horizon"))] = {
                            "status": item.get("status"),
                            "expected_target_at": item.get("expected_target_at"),
                            "authorizes_action": False,
                        }
            except Exception:  # noqa: BLE001 - PI-10 context remains optional evidence
                outlook_context = {}
        result = list(rows.values())
        self._cache[cache_key] = (
            now,
            {"forecasts": result, "outlook": outlook_context},
        )
        return [dict(row) for row in result]

    def evaluation_evidence(
        self, instrument_id: str, horizon: str
    ) -> dict[str, Any]:
        if self._evaluations is None:
            return {
                "available": False,
                "reason": "the PI-11 evaluation service is not attached",
                "automatic_promotion": False,
            }
        try:
            body = self._evaluations.list(limit=20)
        except Exception as exc:  # noqa: BLE001 - evidence absence is explicit
            return {
                "available": False,
                "reason": f"PI-11 evaluation evidence is unavailable: {type(exc).__name__}",
                "automatic_promotion": False,
            }
        reports = []
        for row in body.get("evaluations") or ():
            if not isinstance(row, Mapping):
                continue
            summary = row.get("source_summary")
            summary_text = json.dumps(summary or {}, sort_keys=True, default=str)
            if instrument_id and instrument_id not in summary_text and horizon not in summary_text:
                continue
            reports.append(
                {
                    "evaluation_id": _text(row.get("evaluation_id")),
                    "created_at": _text(row.get("created_at")),
                    "status": _text(row.get("status")),
                    "samples": row.get("samples"),
                    "report_hash": _text(row.get("report_hash")),
                }
            )
            if len(reports) >= 5:
                break
        return {
            "available": bool(reports),
            "reports": reports,
            "scope": "historical PI-11 evidence; not an automatic promotion",
            "automatic_promotion": False,
            "reason": "" if reports else "no matching PI-11 evaluation report is stored",
        }

    def clear(self) -> None:
        self._cache.clear()


class ForecastRiskAdapter:
    """Read-only NM-06-compatible view over PI-08/PI-10 forecasts.

    NM-08's existing forecast reliability check reads ``forecasts()`` and
    ``models()``.  It must remain the authority; this adapter only maps a
    stored PI-12 forecast to the NM-06 horizon label that its existing policy
    already uses.  No policy, limit or violation is calculated here.
    """

    def __init__(self, primary: Any, provider: Any, *, clock: Any | None = None) -> None:
        self._primary = primary
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def forecasts(self, instrument_id: str = "", limit: int = 50) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        try:
            body = self._primary.forecasts(instrument_id=instrument_id, limit=limit)
            rows.extend(
                dict(row) for row in body.get("forecasts") or () if isinstance(row, Mapping)
            )
        except Exception:  # noqa: BLE001 - PI evidence is additive
            pass
        try:
            evidence = self._provider(instrument_id)
            rows.extend(dict(row) for row in evidence.get("forecasts") or ())
        except Exception:  # noqa: BLE001 - missing PI evidence is a risk failure
            pass
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"forecasts": rows[: max(1, min(int(limit), 200))]}

    def models(
        self, instrument_id: str | None = None, horizon: str | None = None
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        try:
            # The store filters by its own exact label, so it is queried without
            # one and the canonical translation is applied here.
            body = self._primary.models(instrument_id=instrument_id)
            rows.extend(
                dict(row) for row in body.get("models") or () if isinstance(row, Mapping)
            )
        except Exception:  # noqa: BLE001
            pass
        if instrument_id:
            try:
                evidence = self._provider(instrument_id)
                rows.extend(dict(row) for row in evidence.get("models") or ())
            except Exception:  # noqa: BLE001
                pass
        if horizon:
            wanted = canonical_horizon(horizon)
            rows = [
                row
                for row in rows
                if canonical_horizon(row.get("horizon")) == wanted
            ]
        deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                _text(row.get("instrument_id")),
                _text(row.get("model_id")),
                _text(row.get("horizon")),
            )
            deduped[key] = row
        return {"models": list(deduped.values())}


def risk_evidence_from_forecasts(
    instrument_id: str, forecasts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Map stored PI forecasts to the labels NM-08 independently evaluates."""

    mapped: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    for row in forecasts:
        item = normalized_forecast(row)
        # NM-06 stores the label form (``one_week``); PI-12 may hold either
        # form.  Resolve canonically so both are recognised exactly once.
        label = canonical_horizon(item["horizon"])
        if label not in set(RISK_HORIZON_MAP.values()) or not item["forecast_id"]:
            continue
        stage = item["lifecycle_stage"]
        validity = item["validity"]
        state = "failed"
        if (
            stage == "t_minus_1"
            and validity in _ALLOWED_VALIDITY
            and item["data_quality"] in _ALLOWED_QUALITY
        ):
            state = "validated" if item["uncertainty"] in {"low", "medium"} else "degraded"
        mapped.append(
            {
                "forecast_id": item["forecast_id"],
                "instrument_id": item["instrument_id"] or instrument_id,
                "horizon": label,
                "predicted_return": item["predicted_return"],
                "probability_up": item["p_up"],
                "uncertainty": item["uncertainty"],
                "model_id": item["model_id"] or "pi12-ensemble",
                "created_at": item["generated_at"],
                "source": "PI-08/PI-10",
                "source_horizon": item["horizon"],
                "target_at": item["target_at"],
            }
        )
        models.append(
            {
                "model_id": item["model_id"] or "pi12-ensemble",
                "instrument_id": item["instrument_id"] or instrument_id,
                "horizon": label,
                "state": state,
                "source": "PI-08/PI-10",
                "source_horizon": item["horizon"],
            }
        )
    return {"forecasts": mapped, "models": models}


def decision_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


__all__ = [
    "FORECAST_DECISION_CONTRACT_VERSION",
    "FORECAST_DECISION_PHASE",
    "ForecastDecisionConfig",
    "ForecastDecisionConfigError",
    "ForecastDecisionDraft",
    "ForecastDecisionEvaluator",
    "ForecastDecisionSource",
    "ForecastDecisionStatus",
    "ForecastDecisionType",
    "ForecastEvidenceUnavailable",
    "ForecastRiskAdapter",
    "HORIZONS",
    "HORIZON_CANONICAL",
    "RISK_HORIZON_MAP",
    "canonical_horizon",
    "UNCERTAINTY_RANK",
    "decision_digest",
    "normalized_forecast",
    "risk_evidence_from_forecasts",
]
