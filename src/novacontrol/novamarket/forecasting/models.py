"""NM-06 forecast value types, horizons, policy and model lifecycle.

A forecast is an immutable record of one probabilistic statement made at one
instant with one data cutoff. It carries the distribution it came from (median,
10th/90th percentile), its directional probabilities, its uncertainty, and the
provenance needed to reproduce it. It is never a promise of a price.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

FEATURE_SET_VERSION = "nm06-features-1.0"
MODEL_CATALOG_VERSION = "nm06-models-1.0"
METHODOLOGY_VERSION = "nm06-forecast-1.0"


class Horizon(StrEnum):
    """The only horizons this build forecasts, in trading sessions."""

    NEXT_SESSION = "next_session"
    FEW_SESSIONS = "few_sessions"
    ONE_WEEK = "one_week"
    ONE_MONTH = "one_month"
    THREE_MONTHS = "three_months"


HORIZON_SESSIONS: dict[Horizon, int] = {
    Horizon.NEXT_SESSION: 1,
    Horizon.FEW_SESSIONS: 3,
    Horizon.ONE_WEEK: 5,
    Horizon.ONE_MONTH: 21,
    Horizon.THREE_MONTHS: 63,
}


class ModelState(StrEnum):
    """A model's lifecycle. Only ACTIVE is used to label live forecasts."""

    EXPERIMENTAL = "experimental"
    VALIDATED = "validated"
    ACTIVE = "active"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    RETIRED = "retired"


#: An illegal move raises; nothing silently overwrites a model's state.
MODEL_TRANSITIONS: dict[ModelState, frozenset[ModelState]] = {
    ModelState.EXPERIMENTAL: frozenset(
        {ModelState.VALIDATED, ModelState.DEGRADED, ModelState.RETIRED}
    ),
    ModelState.VALIDATED: frozenset(
        {ModelState.ACTIVE, ModelState.DEGRADED, ModelState.RETIRED}
    ),
    ModelState.ACTIVE: frozenset(
        {ModelState.DEGRADED, ModelState.SUSPENDED, ModelState.RETIRED}
    ),
    ModelState.DEGRADED: frozenset(
        {ModelState.ACTIVE, ModelState.SUSPENDED, ModelState.RETIRED}
    ),
    ModelState.SUSPENDED: frozenset({ModelState.EXPERIMENTAL, ModelState.RETIRED}),
    ModelState.RETIRED: frozenset(),
}


def ensure_model_transition(
    current: ModelState | str, target: ModelState | str
) -> tuple[ModelState, ModelState]:
    source = ModelState(current)
    destination = ModelState(target)
    if source is destination:
        return source, destination
    if destination not in MODEL_TRANSITIONS[source]:
        raise ValueError(
            f"model cannot move from {source.value} to {destination.value}"
        )
    return source, destination


class ForecastStatus(StrEnum):
    PENDING = "pending"
    EVALUATED = "evaluated"
    UNRESOLVED = "unresolved"


class UncertaintyLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class Regime(StrEnum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ForecastPolicy:
    """Every threshold is a stated decision, published in the contract."""

    max_history_bars: int = 320
    min_history_bars: int = 80
    min_training_bars: int = 140
    min_validation_samples: int = 5
    #: |return| at or below this is SIDEWAYS; both edges are explicit.
    directional_threshold_pct: float = 0.5
    interval_low: float = 0.10
    interval_high: float = 0.90
    uncertainty_low_pct: float = 3.0
    uncertainty_high_pct: float = 10.0
    calibration_min_samples: int = 30
    calibration_holdout_fraction: float = 0.4
    #: A verified event inside the horizon widens the interval by this factor.
    event_interval_widen: float = 1.25
    degrade_brier_margin: float = 0.05
    degrade_coverage_floor: float = 0.55
    suspend_unresolved: int = 10
    unresolved_grace_days: int = 10
    max_forecast_age_hours: float = 72.0
    bootstrap_paths: int = 400
    max_bootstrap_bars: int = 400

    def __post_init__(self) -> None:
        if self.min_history_bars < 30:
            raise ValueError("min_history_bars must be at least 30")
        if self.min_training_bars < self.min_history_bars:
            raise ValueError("min_training_bars must cover min_history_bars")
        if not 0 < self.directional_threshold_pct <= 10:
            raise ValueError("directional_threshold_pct must be within (0, 10]")
        if not 0.0 < self.interval_low < self.interval_high < 1.0:
            raise ValueError("interval quantiles must satisfy 0 < low < high < 1")
        if self.uncertainty_low_pct >= self.uncertainty_high_pct:
            raise ValueError("uncertainty_low_pct must be below the high mark")
        if not 0.0 < self.calibration_holdout_fraction < 1.0:
            raise ValueError("calibration_holdout_fraction must be within (0, 1)")
        if self.bootstrap_paths < 50:
            raise ValueError("bootstrap_paths must be at least 50")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_history_bars": self.max_history_bars,
            "min_history_bars": self.min_history_bars,
            "min_training_bars": self.min_training_bars,
            "min_validation_samples": self.min_validation_samples,
            "directional_threshold_pct": self.directional_threshold_pct,
            "interval_low": self.interval_low,
            "interval_high": self.interval_high,
            "uncertainty_low_pct": self.uncertainty_low_pct,
            "uncertainty_high_pct": self.uncertainty_high_pct,
            "calibration_min_samples": self.calibration_min_samples,
            "calibration_holdout_fraction": self.calibration_holdout_fraction,
            "event_interval_widen": self.event_interval_widen,
            "degrade_brier_margin": self.degrade_brier_margin,
            "degrade_coverage_floor": self.degrade_coverage_floor,
            "suspend_unresolved": self.suspend_unresolved,
            "unresolved_grace_days": self.unresolved_grace_days,
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "bootstrap_paths": self.bootstrap_paths,
        }


@dataclass(frozen=True, slots=True)
class ForecastRecord:
    """One immutable probabilistic forecast."""

    forecast_id: str
    instrument_id: str
    created_at: datetime
    data_cutoff: datetime
    horizon: Horizon
    horizon_sessions: int
    target_at: datetime
    model_id: str
    model_version: str
    feature_set_version: str
    predicted_return: float
    predicted_price: float
    p_up: float
    p_down: float
    p_sideways: float
    lower_return: float
    upper_return: float
    lower_price: float
    upper_price: float
    uncertainty: UncertaintyLevel
    uncertainty_width: float
    data_quality: str
    regime: Regime
    event_ids: tuple[str, ...]
    candidate_score: float | None
    methodology_version: str
    digest: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in ("p_up", "p_down", "p_sideways"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        total = self.p_up + self.p_down + self.p_sideways
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"directional probabilities must sum to 1 (got {total:.9f})"
            )
        if not self.lower_return <= self.predicted_return <= self.upper_return:
            raise ValueError("predicted return must lie inside its interval")
        if self.target_at <= self.created_at:
            raise ValueError("a forecast target must be after its creation")
        if self.data_cutoff > self.created_at:
            raise ValueError("the data cutoff cannot be after creation")
        if self.horizon_sessions < 1:
            raise ValueError("horizon_sessions must be positive")

    @property
    def direction(self) -> Direction:
        if self.p_up > self.p_down and self.p_up > self.p_sideways:
            return Direction.UP
        if self.p_down > self.p_up and self.p_down > self.p_sideways:
            return Direction.DOWN
        return Direction.SIDEWAYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "instrument_id": self.instrument_id,
            "created_at": self.created_at.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "horizon": self.horizon.value,
            "horizon_sessions": self.horizon_sessions,
            "target_at": self.target_at.isoformat(),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "feature_set_version": self.feature_set_version,
            "predicted_return": round(self.predicted_return, 6),
            "predicted_price": round(self.predicted_price, 4),
            "p_up": round(self.p_up, 6),
            "p_down": round(self.p_down, 6),
            "p_sideways": round(self.p_sideways, 6),
            "lower_return": round(self.lower_return, 6),
            "upper_return": round(self.upper_return, 6),
            "lower_price": round(self.lower_price, 4),
            "upper_price": round(self.upper_price, 4),
            "uncertainty": self.uncertainty.value,
            "uncertainty_width": round(self.uncertainty_width, 6),
            "data_quality": self.data_quality,
            "regime": self.regime.value,
            "event_ids": list(self.event_ids),
            "candidate_score": self.candidate_score,
            "methodology_version": self.methodology_version,
            "digest": self.digest,
            "notes": list(self.notes),
            "is_price_prediction": False,
            "is_investment_advice": False,
            "guaranteed": False,
        }


@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    forecast_id: str
    instrument_id: str
    status: ForecastStatus
    evaluated_at: datetime
    actual_return: float | None
    actual_price: float | None
    error: float | None
    direction_correct: bool | None
    brier: float | None
    log_loss: float | None
    inside_interval: bool | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "instrument_id": self.instrument_id,
            "status": self.status.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "actual_return": (
                None if self.actual_return is None else round(self.actual_return, 6)
            ),
            "actual_price": (
                None if self.actual_price is None else round(self.actual_price, 4)
            ),
            "error": None if self.error is None else round(self.error, 6),
            "direction_correct": self.direction_correct,
            "brier": None if self.brier is None else round(self.brier, 6),
            "log_loss": None if self.log_loss is None else round(self.log_loss, 6),
            "inside_interval": self.inside_interval,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ModelRecord:
    model_id: str
    version: str
    state: ModelState
    kind: str
    description: str
    trained_at: datetime | None
    updated_at: datetime
    instrument_id: str
    horizon: str
    metrics: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "state": self.state.value,
            "kind": self.kind,
            "description": self.description,
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
            "updated_at": self.updated_at.isoformat(),
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "metrics": self.metrics,
            "reason": self.reason,
        }


def forecast_id_for(
    instrument_id: str,
    created_at: datetime,
    horizon: Horizon,
    model_id: str,
) -> str:
    seed = f"{instrument_id}|{created_at.isoformat()}|{horizon.value}|{model_id}"
    return "fc_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def digest_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "FEATURE_SET_VERSION",
    "HORIZON_SESSIONS",
    "METHODOLOGY_VERSION",
    "MODEL_CATALOG_VERSION",
    "MODEL_TRANSITIONS",
    "Direction",
    "ForecastOutcome",
    "ForecastPolicy",
    "ForecastRecord",
    "ForecastStatus",
    "Horizon",
    "ModelRecord",
    "ModelState",
    "Regime",
    "UncertaintyLevel",
    "digest_payload",
    "ensure_model_transition",
    "forecast_id_for",
    "utcnow",
]
