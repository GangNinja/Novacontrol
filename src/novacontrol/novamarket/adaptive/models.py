"""PI-16 adaptive-intelligence contracts.

The subsystem is deliberately separate from NM-13's strategy-promotion
machinery.  It consumes PI-04 regime evidence, PI-11/PI-14 evaluation evidence
and PI-15 policy artifacts, then records *research* conclusions.  No record in
this module carries an order, fill, portfolio, wallet, autonomy or operator
permission handle.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

ADAPTIVE_PHASE = "PI-16"
ADAPTIVE_CONTRACT_VERSION = "pi16-adaptive-intelligence-v1"
ADAPTIVE_SCHEMA_VERSION = 1
ADAPTIVE_METHOD_VERSION = "pi16-adaptive-1.0"
DETECTOR_VERSION = "pi16-detectors-1.0"
DEFAULT_CONFIG_ID = "adpconfig_default"

MAX_CONFIGS = 200
MAX_RECORDS = 10_000
MAX_WINDOW = 5_000
MAX_RECOMMENDATIONS = 5_000
MAX_REASSESSMENTS = 1_000
MAX_TEXT = 500


class AdaptiveError(ValueError):
    """A caller supplied an invalid adaptive-intelligence request."""


class AdaptiveValidationError(AdaptiveError):
    """A configuration, observation or evidence payload failed validation."""


class AdaptiveNotFoundError(LookupError):
    """The requested research record does not exist."""


class AdaptiveStateError(RuntimeError):
    """The record state does not permit the requested operation."""


class AdaptiveResourceError(RuntimeError):
    """A bounded adaptive-intelligence limit would be exceeded."""


class AdaptiveStoreError(RuntimeError):
    """The isolated adaptive-intelligence store cannot complete a request."""


class SuitabilityStatus(StrEnum):
    SUITABLE = "suitable"
    CONDITIONALLY_SUITABLE = "conditionally_suitable"
    UNSUITABLE = "unsuitable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    STALE_EVALUATION = "stale_evaluation"
    NOT_EVALUATED = "not_evaluated"


class ChangeStatus(StrEnum):
    NORMAL_VARIATION = "normal_variation"
    TEMPORARY_ANOMALY = "temporary_anomaly"
    POTENTIAL_REGIME_TRANSITION = "potential_regime_transition"
    SUSTAINED_CHANGE = "sustained_change"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DATA_UNAVAILABLE = "data_unavailable"


class DriftStatus(StrEnum):
    NO_DRIFT = "no_drift"
    DATA_DRIFT = "data_drift"
    CONCEPT_DRIFT = "concept_drift"
    BOTH = "both"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DATA_UNAVAILABLE = "data_unavailable"


class DegradationStatus(StrEnum):
    NONE = "none"
    TEMPORARY_UNDERPERFORMANCE = "temporary_underperformance"
    PERSISTENT_DEGRADATION = "persistent_degradation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DATA_UNAVAILABLE = "data_unavailable"


class RecommendationAction(StrEnum):
    CONTINUE_MONITORING = "continue_monitoring"
    NO_ADAPTATION_REQUIRED = "no_adaptation_required"
    REEVALUATE_POLICY = "reevaluate_policy"
    RUN_NEW_BACKTEST = "run_new_backtest"
    RETRAIN_RECOMMENDED = "retrain_recommended"
    REASSESS_FEATURES = "reassess_features"
    REVIEW_REWARD_CONFIGURATION = "review_reward_configuration"
    POLICY_UNSUITABLE = "policy_unsuitable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RecommendationStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    SUPERSEDED = "superseded"


class ReassessmentStatus(StrEnum):
    PENDING_REASSESSMENT = "pending_reassessment"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    STALE_EVIDENCE = "stale_evidence"


class EvidenceStrength(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Regime aliases kept for PI-04's canonical labels.  The adaptive layer never
#: invents a new market label; it only maps a caller's display form back to the
#: PI-04 vocabulary where the mapping is unambiguous.
REGIME_ALIASES = {
    "strong_bullish": "strong_up",
    "bullish": "weak_up",
    "strong_bearish": "strong_down",
    "bearish": "weak_down",
    "range": "sideways",
    "unclassified": "uncertain",
    "unknown": "uncertain",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: object, name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        raise AdaptiveValidationError(f"{name} is required")
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdaptiveValidationError(f"{name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdaptiveValidationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def optional_time(value: object, name: str = "timestamp") -> datetime | None:
    if value in (None, ""):
        return None
    return parse_time(value, name)


def text(value: object, default: str = "") -> str:
    if value is None:
        return default
    result = str(value).strip()
    return result or default


def finite(
    value: object, name: str, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(value, bool):
        raise AdaptiveValidationError(f"{name} must be numeric")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AdaptiveValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise AdaptiveValidationError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise AdaptiveValidationError(f"{name} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise AdaptiveValidationError(f"{name} must be at most {maximum}")
    return number


def whole(value: object, name: str, *, minimum: int = 0, maximum: int = 100_000) -> int:
    if isinstance(value, bool):
        raise AdaptiveValidationError(f"{name} must be an integer")
    try:
        raw = float(str(value))
        if not raw.is_integer():
            raise ValueError
        number = int(raw)
    except (TypeError, ValueError) as exc:
        raise AdaptiveValidationError(f"{name} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise AdaptiveValidationError(f"{name} must be within [{minimum}, {maximum}]")
    return number


def sequence(value: object, name: str = "items") -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, bytes)):
        return [item.strip() for item in str(value).split(",") if item.strip()]
    if isinstance(value, Sequence):
        return list(value)
    raise AdaptiveValidationError(f"{name} must be a list or comma-separated string")


def mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdaptiveValidationError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def regime_label(value: object) -> str:
    label = text(value, "uncertain").lower().replace(" ", "_").replace("-", "_")
    label = REGIME_ALIASES.get(label, label)
    allowed = {
        "strong_up",
        "weak_up",
        "strong_down",
        "weak_down",
        "sideways",
        "high_volatility",
        "low_volatility",
        "potential_reversal",
        "breakout",
        "breakdown",
        "uncertain",
    }
    return label if label in allowed else "uncertain"


@dataclass(frozen=True, slots=True)
class AdaptiveConfig:
    """Versioned thresholds and bounded resource controls.

    Thresholds are intentionally conservative.  A single metric crossing a
    threshold is reported as temporary variation; persistent degradation needs
    the configured number of confirming windows and at least two independent
    signals unless one signal is severe enough to stand alone.
    """

    config_id: str = DEFAULT_CONFIG_ID
    window_size: int = 20
    min_observations: int = 12
    min_regime_samples: int = 10
    min_policy_observations: int = 8
    psi_threshold: float = 0.20
    ks_alpha: float = 0.05
    js_threshold: float = 0.12
    volatility_change_threshold: float = 0.35
    volume_change_threshold: float = 0.50
    correlation_change_threshold: float = 0.25
    forecast_error_change_threshold: float = 0.30
    relationship_delta_threshold: float = 0.30
    relationship_alpha: float = 0.05
    page_hinkley_delta: float = 0.01
    page_hinkley_threshold: float = 6.0
    cusum_threshold: float = 5.0
    change_persistence_windows: int = 2
    degradation_persistence_windows: int = 2
    degradation_return_drop: float = 0.02
    degradation_drawdown_increase: float = 0.05
    degradation_volatility_increase: float = 0.03
    degradation_sharpe_drop: float = 0.50
    degradation_cost_increase: float = 1.50
    degradation_turnover_increase: float = 1.75
    degradation_invalid_action_increase: float = 0.05
    degradation_action_shift: float = 0.25
    degradation_reward_drop: float = 0.10
    suitability_suitable_score: float = 0.65
    suitability_conditional_score: float = 0.45
    hard_drawdown_limit: float = 0.25
    hard_invalid_action_limit: float = 0.10
    stale_evaluation_days: int = 30
    cooldown_seconds: int = 86_400
    hysteresis_margin: float = 0.05
    max_records: int = MAX_RECORDS
    max_observations: int = MAX_WINDOW
    max_reassessment_seconds: int = 300
    automatic_promotion: bool = False
    automatic_training: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_id", text(self.config_id, DEFAULT_CONFIG_ID))
        positive = {
            "window_size",
            "min_observations",
            "min_regime_samples",
            "min_policy_observations",
            "change_persistence_windows",
            "degradation_persistence_windows",
            "max_records",
            "max_reassessment_seconds",
        }
        for name in (
            *sorted(positive),
            "stale_evaluation_days",
            "cooldown_seconds",
            "max_observations",
        ):
            minimum = 2 if name == "window_size" else 1 if name in positive else 0
            maximum = MAX_RECORDS if name == "max_records" else 100_000
            object.__setattr__(
                self,
                name,
                whole(getattr(self, name), name, minimum=minimum, maximum=maximum),
            )
        if self.min_observations < 4:
            raise AdaptiveValidationError("min_observations must be at least 4")
        if self.min_regime_samples < 2:
            raise AdaptiveValidationError("min_regime_samples must be at least 2")
        if self.min_policy_observations < 4:
            raise AdaptiveValidationError("min_policy_observations must be at least 4")
        for name in (
            "psi_threshold",
            "ks_alpha",
            "js_threshold",
            "volatility_change_threshold",
            "volume_change_threshold",
            "correlation_change_threshold",
            "forecast_error_change_threshold",
            "relationship_delta_threshold",
            "relationship_alpha",
            "page_hinkley_delta",
            "page_hinkley_threshold",
            "cusum_threshold",
            "degradation_return_drop",
            "degradation_drawdown_increase",
            "degradation_volatility_increase",
            "degradation_sharpe_drop",
            "degradation_cost_increase",
            "degradation_turnover_increase",
            "degradation_invalid_action_increase",
            "degradation_action_shift",
            "degradation_reward_drop",
            "suitability_suitable_score",
            "suitability_conditional_score",
            "hard_drawdown_limit",
            "hard_invalid_action_limit",
            "hysteresis_margin",
        ):
            object.__setattr__(self, name, finite(getattr(self, name), name))
        for name in (
            "psi_threshold",
            "volatility_change_threshold",
            "volume_change_threshold",
            "correlation_change_threshold",
            "forecast_error_change_threshold",
            "relationship_delta_threshold",
            "page_hinkley_delta",
            "page_hinkley_threshold",
            "cusum_threshold",
            "degradation_return_drop",
            "degradation_drawdown_increase",
            "degradation_volatility_increase",
            "degradation_sharpe_drop",
            "degradation_cost_increase",
            "degradation_turnover_increase",
            "degradation_invalid_action_increase",
            "degradation_action_shift",
            "degradation_reward_drop",
        ):
            if getattr(self, name) <= 0:
                raise AdaptiveValidationError(f"{name} must be greater than zero")
        if not 0 < self.ks_alpha < 1:
            raise AdaptiveValidationError("ks_alpha must be within (0, 1)")
        if not 0 < self.relationship_alpha < 1:
            raise AdaptiveValidationError("relationship_alpha must be within (0, 1)")
        if not 0 < self.js_threshold < 1:
            raise AdaptiveValidationError("js_threshold must be within (0, 1)")
        if not 0 <= self.suitability_conditional_score < self.suitability_suitable_score <= 1:
            raise AdaptiveValidationError(
                "suitability thresholds must satisfy conditional < suitable <= 1"
            )
        if self.automatic_promotion or self.automatic_training:
            raise AdaptiveValidationError("automatic promotion and training are forbidden in PI-16")
        if self.max_observations > MAX_WINDOW:
            raise AdaptiveValidationError(f"max_observations must not exceed {MAX_WINDOW}")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> AdaptiveConfig:
        if not payload:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(str(key) for key in payload if key not in allowed)
        if unknown:
            raise AdaptiveValidationError(
                "unknown adaptive configuration field(s): " + ", ".join(unknown)
            )
        return cls(**{str(key): value for key, value in payload.items()})

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """A source-preserving view of one PI-04 regime classification."""

    regime_id: str
    instrument_id: str
    as_of: datetime
    primary: str
    secondary: tuple[str, ...] = ()
    confidence: float | None = None
    confidence_calibrated: bool = False
    supporting_features: tuple[str, ...] = ()
    evidence: Mapping[str, float] = field(default_factory=dict)
    method_version: str = ""
    feature_set_version: str = ""
    source: str = "pi04"
    quality: str = "partial"
    available_at: datetime | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not text(self.regime_id):
            raise AdaptiveValidationError("regime_id is required")
        object.__setattr__(self, "instrument_id", text(self.instrument_id).upper())
        object.__setattr__(self, "as_of", parse_time(self.as_of, "as_of"))
        object.__setattr__(self, "primary", regime_label(self.primary))
        object.__setattr__(
            self,
            "secondary",
            tuple(dict.fromkeys(regime_label(item) for item in self.secondary)),
        )
        if self.confidence is not None:
            value = finite(self.confidence, "confidence", minimum=0.0, maximum=1.0)
            if not self.confidence_calibrated:
                raise AdaptiveValidationError("uncalibrated regime confidence must remain unknown")
            object.__setattr__(self, "confidence", value)
        object.__setattr__(
            self, "supporting_features", tuple(str(item) for item in self.supporting_features)
        )
        object.__setattr__(
            self,
            "evidence",
            {str(key): finite(value, f"evidence.{key}") for key, value in self.evidence.items()},
        )
        object.__setattr__(
            self, "reasons", tuple(text(item) for item in self.reasons if text(item))
        )
        if self.available_at is not None:
            object.__setattr__(self, "available_at", parse_time(self.available_at, "available_at"))
        if self.available_at is not None and self.available_at > self.as_of:
            raise AdaptiveValidationError(
                "regime availability cannot follow its observation timestamp"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_id": self.regime_id,
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "primary_regime": self.primary,
            "secondary_conditions": list(self.secondary),
            "confidence": self.confidence,
            "confidence_calibrated": self.confidence_calibrated,
            "supporting_features": list(self.supporting_features),
            "evidence": dict(sorted(self.evidence.items())),
            "method_version": self.method_version,
            "feature_set_version": self.feature_set_version,
            "source": self.source,
            "quality": self.quality,
            "reasons": list(self.reasons),
            "advisory_only": True,
            "production_authority": False,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, source: str = "pi04") -> RegimeSnapshot:
        values = mapping(payload, "regime")
        return cls(
            regime_id=text(values.get("regime_id")),
            instrument_id=text(values.get("instrument_id")),
            as_of=parse_time(values.get("as_of"), "as_of"),
            primary=text(values.get("primary_regime") or values.get("primary") or "uncertain"),
            secondary=tuple(
                sequence(values.get("secondary_conditions") or values.get("secondary"))
            ),
            confidence=(
                None
                if values.get("confidence") is None
                else finite(values.get("confidence"), "confidence")
            ),
            confidence_calibrated=bool(values.get("confidence_calibrated", False)),
            supporting_features=tuple(sequence(values.get("supporting_features"))),
            evidence={
                str(key): finite(value, f"evidence.{key}")
                for key, value in mapping(values.get("evidence") or {}, "evidence").items()
            },
            method_version=text(values.get("method_version") or values.get("method")),
            feature_set_version=text(values.get("feature_set_version")),
            source=text(values.get("source"), source),
            quality=text(values.get("feature_quality") or values.get("quality"), "partial"),
            available_at=optional_time(values.get("available_at"), "available_at"),
            reasons=tuple(sequence(values.get("reasons"))),
        )


def evaluation_age_days(evaluated_at: datetime, now: datetime) -> float:
    return max(0.0, (now - evaluated_at).total_seconds() / 86_400.0)


def stale_deadline(evaluated_at: datetime, stale_days: int) -> datetime:
    return evaluated_at + timedelta(days=stale_days)


@dataclass(frozen=True, slots=True)
class RegimeWindow:
    """A bounded historical window associated with one regime label."""

    regime: str
    start: datetime
    end: datetime
    observations: tuple[Mapping[str, Any], ...] = ()
    regime_version: str = ""
    source: str = "pi04"

    def __post_init__(self) -> None:
        object.__setattr__(self, "regime", regime_label(self.regime))
        object.__setattr__(self, "start", parse_time(self.start, "start"))
        object.__setattr__(self, "end", parse_time(self.end, "end"))
        if self.start > self.end:
            raise AdaptiveValidationError("regime window start must not follow end")
        object.__setattr__(
            self, "observations", tuple(mapping(item, "observation") for item in self.observations)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "observations": [dict(item) for item in self.observations],
            "regime_version": self.regime_version,
            "source": self.source,
        }
