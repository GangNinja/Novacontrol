"""PI-08 ensemble contracts.

The ensemble is an additive layer over PI-01's ``EnsembleAssembler``.  This
module keeps the phase's configuration, eligibility evidence and calibration
records typed and reproducible; it does not define another forecast contract
or another model registry.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.contracts import NovaMarketError
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
    ForecastHorizon,
    ModelFamily,
    ModelState,
)

ENSEMBLE_PHASE = "PI-08"
ENSEMBLE_CONTRACT_VERSION = "pi08.ensemble.1.0"
ENSEMBLE_SCHEMA_VERSION = 1
ENSEMBLE_METHODOLOGY_VERSION = "pi08-evidence-weighted-ensemble-v1"
ENSEMBLE_MODEL_VERSION = "pi08-ensemble-1.0"
ENSEMBLE_CALIBRATION_VERSION = "pi08-split-conformal-1.0"
ENSEMBLE_FORECAST_PREFIX = "ens_"
ENSEMBLE_FALLBACK_PREFIX = "ensfb_"
ENSEMBLE_CONFIG_PREFIX = "enc_"
ENSEMBLE_EVALUATION_PREFIX = "enev_"
ENSEMBLE_CALIBRATION_PREFIX = "encal_"

SUPPORTED_FAMILIES = (
    ModelFamily.NAIVE,
    ModelFamily.MOVING_AVERAGE,
    ModelFamily.EXPONENTIAL_SMOOTHING,
    ModelFamily.ARIMA,
    ModelFamily.XGBOOST,
    ModelFamily.LIGHTGBM,
    ModelFamily.LSTM,
)


class EnsembleError(NovaMarketError):
    """Base class for a PI-08 refusal."""


class EnsembleValidationError(EnsembleError, ValueError):
    """A configuration or contribution is not valid for an ensemble."""


class EnsembleUnavailableError(EnsembleError):
    """No eligible ensemble or constituent is available."""


def ensemble_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _clean(value: object) -> str:
    return str(value or "").strip()


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EnsembleValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise EnsembleValidationError(f"{name} must be a timestamp")
    if value.tzinfo is None or value.utcoffset() is None:
        raise EnsembleValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: object, name: str, *, minimum: float | None = None) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise EnsembleValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise EnsembleValidationError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise EnsembleValidationError(f"{name} must be at least {minimum}")
    return number


def _enabled(value: object) -> bool:
    return bool(value)


@dataclass(frozen=True, slots=True)
class EnsembleConfig:
    """Reproducible policy and methodology choices for one ensemble."""

    ensemble_version: str = ENSEMBLE_MODEL_VERSION
    methodology_version: str = ENSEMBLE_METHODOLOGY_VERSION
    weighting_method: str = "validation_inverse_mae_smoothed"
    calibration_method: str = ENSEMBLE_CALIBRATION_VERSION
    interval_level: float = 0.8
    min_models: int = 2
    max_models: int = 12
    max_weight: float = 0.55
    min_weight: float = 0.05
    smoothing: float = 0.25
    degraded_penalty: float = 0.55
    recency_half_life_days: float = 365.0
    min_validation_samples: int = 5
    min_regime_samples: int = 5
    max_disagreement: float = 0.03
    abstain_disagreement: float = 0.08
    max_interval_width: float = 0.20
    min_direction_probability: float = 0.40
    allow_degraded: bool = True
    allow_equal_weight_baseline: bool = True
    require_training_evidence: bool = True
    regime_aware: bool = True
    automatic_promotion: bool = False

    def __post_init__(self) -> None:
        for name in ("ensemble_version", "methodology_version", "weighting_method"):
            if not _clean(getattr(self, name)):
                raise EnsembleValidationError(f"{name} is required")
        if self.calibration_method != ENSEMBLE_CALIBRATION_VERSION:
            raise EnsembleValidationError(
                f"calibration_method must be {ENSEMBLE_CALIBRATION_VERSION!r}"
            )
        level = _finite(self.interval_level, "interval_level")
        if not 0.05 <= level < 1.0:
            raise EnsembleValidationError("interval_level must be within [0.05, 1)")
        if not 1 <= int(self.min_models) <= int(self.max_models) <= 50:
            raise EnsembleValidationError(
                "min_models must be between 1 and max_models (at most 50)"
            )
        maximum = _finite(self.max_weight, "max_weight")
        minimum = _finite(self.min_weight, "min_weight")
        if not 0.0 < minimum <= maximum <= 1.0:
            raise EnsembleValidationError(
                "weights must satisfy 0 < min_weight <= max_weight <= 1"
            )
        smoothing = _finite(self.smoothing, "smoothing")
        if not 0.0 <= smoothing <= 0.95:
            raise EnsembleValidationError("smoothing must be within [0, 0.95]")
        penalty = _finite(self.degraded_penalty, "degraded_penalty")
        if not 0.0 < penalty <= 1.0:
            raise EnsembleValidationError("degraded_penalty must be within (0, 1]")
        if not 0.0 < float(self.recency_half_life_days) <= 36500.0:
            raise EnsembleValidationError("recency_half_life_days is out of range")
        if int(self.min_validation_samples) < 1:
            raise EnsembleValidationError("min_validation_samples must be positive")
        if int(self.min_regime_samples) < 1:
            raise EnsembleValidationError("min_regime_samples must be positive")
        max_disagreement = _finite(self.max_disagreement, "max_disagreement")
        abstain = _finite(self.abstain_disagreement, "abstain_disagreement")
        if not 0.0 <= max_disagreement < abstain <= 1.0:
            raise EnsembleValidationError(
                "max_disagreement must be below abstain_disagreement"
            )
        width = _finite(self.max_interval_width, "max_interval_width")
        if not 0.0 < width <= 2.0:
            raise EnsembleValidationError("max_interval_width must be within (0, 2]")
        probability = _finite(
            self.min_direction_probability, "min_direction_probability"
        )
        if not 0.0 <= probability <= 1.0:
            raise EnsembleValidationError(
                "min_direction_probability must be within [0, 1]"
            )
        if self.automatic_promotion:
            raise EnsembleValidationError(
                "automatic promotion is forbidden; PI-08 never promotes a model"
            )
        object.__setattr__(self, "interval_level", level)
        object.__setattr__(self, "min_models", int(self.min_models))
        object.__setattr__(self, "max_models", int(self.max_models))
        object.__setattr__(self, "min_regime_samples", int(self.min_regime_samples))
        object.__setattr__(self, "max_weight", maximum)
        object.__setattr__(self, "min_weight", minimum)
        object.__setattr__(self, "smoothing", smoothing)
        object.__setattr__(self, "degraded_penalty", penalty)
        object.__setattr__(self, "max_disagreement", max_disagreement)
        object.__setattr__(self, "abstain_disagreement", abstain)
        object.__setattr__(self, "max_interval_width", width)
        object.__setattr__(
            self, "min_direction_probability", probability
        )
        for name in (
            "allow_degraded",
            "allow_equal_weight_baseline",
            "require_training_evidence",
            "regime_aware",
        ):
            object.__setattr__(self, name, bool(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ensemble_version": self.ensemble_version,
            "methodology_version": self.methodology_version,
            "weighting_method": self.weighting_method,
            "calibration_method": self.calibration_method,
            "interval_level": self.interval_level,
            "min_models": self.min_models,
            "max_models": self.max_models,
            "max_weight": self.max_weight,
            "min_weight": self.min_weight,
            "smoothing": self.smoothing,
            "degraded_penalty": self.degraded_penalty,
            "recency_half_life_days": self.recency_half_life_days,
            "min_validation_samples": self.min_validation_samples,
            "min_regime_samples": self.min_regime_samples,
            "max_disagreement": self.max_disagreement,
            "abstain_disagreement": self.abstain_disagreement,
            "max_interval_width": self.max_interval_width,
            "min_direction_probability": self.min_direction_probability,
            "allow_degraded": self.allow_degraded,
            "allow_equal_weight_baseline": self.allow_equal_weight_baseline,
            "require_training_evidence": self.require_training_evidence,
            "regime_aware": self.regime_aware,
            "automatic_promotion": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> EnsembleConfig:
        if not payload:
            return cls()
        allowed = {
            field_name
            for field_name in cls.__dataclass_fields__
            if field_name != "automatic_promotion"
        }
        return cls(**{key: payload[key] for key in allowed if key in payload})

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class ModelEvidence:
    """Validation-only evidence used to decide eligibility and weight."""

    model_id: str
    version: str
    family: ModelFamily
    state: ModelState
    horizon: ForecastHorizon
    instrument_id: str
    validation_mae: float | None = None
    validation_direction_accuracy: float | None = None
    validation_samples: int = 0
    walk_forward_mae: float | None = None
    walk_forward_direction_accuracy: float | None = None
    regime_mae: float | None = None
    regime_direction_accuracy: float | None = None
    regime: str = ""
    regime_samples: int = 0
    trained_at: datetime | None = None
    dataset_version: str = "unspecified"
    feature_set_version: str = "unspecified"
    status: str = ""
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _clean(self.model_id) or not _clean(self.version):
            raise EnsembleValidationError("model identity is required")
        try:
            family = ModelFamily(self.family)
            state = ModelState(self.state)
            horizon = ForecastHorizon(self.horizon)
        except ValueError as exc:
            raise EnsembleValidationError(f"invalid evidence enum: {exc}") from exc
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "instrument_id", _clean(self.instrument_id))
        object.__setattr__(
            self,
            "validation_mae",
            None
            if self.validation_mae is None
            else _finite(self.validation_mae, "validation_mae", minimum=0.0),
        )
        object.__setattr__(
            self,
            "walk_forward_mae",
            None
            if self.walk_forward_mae is None
            else _finite(self.walk_forward_mae, "walk_forward_mae", minimum=0.0),
        )
        object.__setattr__(
            self,
            "regime_mae",
            None
            if self.regime_mae is None
            else _finite(self.regime_mae, "regime_mae", minimum=0.0),
        )
        for name in ("validation_samples", "regime_samples"):
            value = int(getattr(self, name))
            if value < 0:
                raise EnsembleValidationError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "family": self.family.value,
            "state": self.state.value,
            "horizon": self.horizon.value,
            "instrument_id": self.instrument_id,
            "validation_mae": self.validation_mae,
            "validation_direction_accuracy": self.validation_direction_accuracy,
            "validation_samples": self.validation_samples,
            "walk_forward_mae": self.walk_forward_mae,
            "walk_forward_direction_accuracy": self.walk_forward_direction_accuracy,
            "regime_mae": self.regime_mae,
            "regime_direction_accuracy": self.regime_direction_accuracy,
            "regime": self.regime,
            "regime_samples": self.regime_samples,
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "status": self.status,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    model_id: str
    version: str
    family: ModelFamily
    eligible: bool
    weight: float
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "family": self.family.value,
            "eligible": self.eligible,
            "weight": round(self.weight, 12),
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "fallback": self.fallback,
        }


@dataclass(frozen=True, slots=True)
class WeightResult:
    weights: Mapping[str, float]
    decisions: tuple[EligibilityDecision, ...]
    methodology: str
    config_digest: str
    previous_config_id: str = ""
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": {key: round(value, 12) for key, value in self.weights.items()},
            "decisions": [item.to_dict() for item in self.decisions],
            "methodology": self.methodology,
            "config_digest": self.config_digest,
            "previous_config_id": self.previous_config_id,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ConformalCalibration:
    calibration_id: str
    instrument_id: str
    horizon: ForecastHorizon
    level: float
    quantile: float
    samples: int
    method: str
    fitted_on: str
    dataset_version: str
    config_digest: str
    created_at: datetime
    minimum_half_width: float = 0.0
    status: str = "provisional"

    def __post_init__(self) -> None:
        horizon = ForecastHorizon(self.horizon)
        level = _finite(self.level, "level")
        if not 0.05 <= level < 1.0:
            raise EnsembleValidationError("calibration level must be within [0.05, 1)")
        quantile = _finite(self.quantile, "quantile", minimum=0.0)
        if int(self.samples) < 1:
            raise EnsembleValidationError("calibration requires at least one sample")
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "quantile", quantile)
        object.__setattr__(self, "samples", int(self.samples))
        object.__setattr__(self, "minimum_half_width", float(self.minimum_half_width))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_id": self.calibration_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "level": self.level,
            "quantile": self.quantile,
            "samples": self.samples,
            "method": self.method,
            "fitted_on": self.fitted_on,
            "dataset_version": self.dataset_version,
            "config_digest": self.config_digest,
            "created_at": self.created_at.isoformat(),
            "minimum_half_width": self.minimum_half_width,
            "status": self.status,
            "guarantee": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ConformalCalibration:
        return cls(
            calibration_id=str(payload["calibration_id"]),
            instrument_id=str(payload["instrument_id"]),
            horizon=ForecastHorizon(str(payload["horizon"])),
            level=float(payload["level"]),
            quantile=float(payload["quantile"]),
            samples=int(payload["samples"]),
            method=str(payload["method"]),
            fitted_on=str(payload.get("fitted_on") or "validation"),
            dataset_version=str(payload.get("dataset_version") or "unspecified"),
            config_digest=str(payload.get("config_digest") or ""),
            created_at=_aware(payload.get("created_at"), "created_at"),
            minimum_half_width=float(payload.get("minimum_half_width") or 0.0),
            status=str(payload.get("status") or "provisional"),
        )

    def apply(self, predicted_return: float) -> tuple[float, float]:
        half_width = max(float(self.quantile), float(self.minimum_half_width))
        lower = float(predicted_return) - half_width
        upper = float(predicted_return) + half_width
        if lower <= -1.0:
            lower = -0.999999
        return lower, upper


@dataclass(frozen=True, slots=True)
class EnsembleObservation:
    """One out-of-sample ensemble result used by calibration/evaluation."""

    instrument_id: str
    horizon: ForecastHorizon
    target_at: datetime
    predicted_return: float
    actual_return: float
    predicted_price: float
    actual_price: float
    lower_return: float
    upper_return: float
    predicted_direction: str
    actual_direction: str
    regime: str = "unclassified"
    model_id: str = "ensemble"
    validation: bool = False
    constituent_returns: Mapping[str, float] = field(default_factory=dict)
    constituent_weights: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            horizon = ForecastHorizon(self.horizon)
        except ValueError as exc:
            raise EnsembleValidationError(f"invalid observation horizon: {exc}") from exc
        object.__setattr__(self, "horizon", horizon)
        for name in (
            "predicted_return",
            "actual_return",
            "predicted_price",
            "actual_price",
            "lower_return",
            "upper_return",
        ):
            object.__setattr__(
                self, name, _finite(getattr(self, name), name)
            )
        if self.predicted_price <= 0.0 or self.actual_price <= 0.0:
            raise EnsembleValidationError("observation prices must be positive")
        if not self.lower_return <= self.predicted_return <= self.upper_return:
            raise EnsembleValidationError(
                "observation point must lie inside its interval"
            )
        object.__setattr__(self, "target_at", _aware(self.target_at, "target_at"))
        object.__setattr__(self, "regime", _clean(self.regime) or "unclassified")
        object.__setattr__(self, "model_id", _clean(self.model_id) or "ensemble")
        object.__setattr__(self, "validation", bool(self.validation))
        object.__setattr__(
            self,
            "constituent_returns",
            {str(key): float(value) for key, value in self.constituent_returns.items()},
        )
        object.__setattr__(
            self,
            "constituent_weights",
            {str(key): float(value) for key, value in self.constituent_weights.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "target_at": self.target_at.isoformat(),
            "predicted_return": self.predicted_return,
            "actual_return": self.actual_return,
            "predicted_price": self.predicted_price,
            "actual_price": self.actual_price,
            "lower_return": self.lower_return,
            "upper_return": self.upper_return,
            "predicted_direction": self.predicted_direction,
            "actual_direction": self.actual_direction,
            "regime": self.regime,
            "model_id": self.model_id,
            "validation": self.validation,
            "constituent_returns": dict(self.constituent_returns),
            "constituent_weights": dict(self.constituent_weights),
        }


@dataclass(frozen=True, slots=True)
class EnsembleEvaluation:
    """Persisted validation/test evaluation for one ensemble configuration."""

    evaluation_id: str
    ensemble_id: str
    config_id: str
    instrument_id: str
    horizon: ForecastHorizon
    dataset_version: str
    feature_set_version: str
    created_at: datetime
    config: EnsembleConfig
    weights: Mapping[str, float]
    eligibility: tuple[Mapping[str, Any], ...]
    validation: Mapping[str, Any]
    test: Mapping[str, Any]
    calibration: ConformalCalibration
    component_metrics: Mapping[str, Any] = field(default_factory=dict)
    baseline_metrics: Mapping[str, Any] = field(default_factory=dict)
    regime_metrics: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    automatic_promotion: bool = False

    def __post_init__(self) -> None:
        try:
            horizon = ForecastHorizon(self.horizon)
        except ValueError as exc:
            raise EnsembleValidationError(f"invalid evaluation horizon: {exc}") from exc
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        if self.automatic_promotion:
            raise EnsembleValidationError("automatic promotion is forbidden")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "ensemble_id": self.ensemble_id,
            "config_id": self.config_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "created_at": self.created_at.isoformat(),
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "weights": {key: round(value, 12) for key, value in self.weights.items()},
            "eligibility": [dict(item) for item in self.eligibility],
            "validation": dict(self.validation),
            "test": dict(self.test),
            "calibration": self.calibration.to_dict(),
            "component_metrics": dict(self.component_metrics),
            "baseline_metrics": dict(self.baseline_metrics),
            "regime_metrics": dict(self.regime_metrics),
            "reasons": list(self.reasons),
            "automatic_promotion": False,
            "phase": ENSEMBLE_PHASE,
            "contract_version": ENSEMBLE_CONTRACT_VERSION,
        }


def calibration_status(value: CalibrationStatus | str) -> CalibrationStatus:
    try:
        return CalibrationStatus(value)
    except ValueError as exc:
        raise EnsembleValidationError(f"invalid calibration status: {exc}") from exc


__all__ = [
    "ENSEMBLE_CALIBRATION_PREFIX",
    "ENSEMBLE_CALIBRATION_VERSION",
    "ENSEMBLE_CONFIG_PREFIX",
    "ENSEMBLE_CONTRACT_VERSION",
    "ENSEMBLE_EVALUATION_PREFIX",
    "ENSEMBLE_FALLBACK_PREFIX",
    "ENSEMBLE_FORECAST_PREFIX",
    "ENSEMBLE_METHODOLOGY_VERSION",
    "ENSEMBLE_MODEL_VERSION",
    "ENSEMBLE_PHASE",
    "ENSEMBLE_SCHEMA_VERSION",
    "SUPPORTED_FAMILIES",
    "ConformalCalibration",
    "EligibilityDecision",
    "EnsembleConfig",
    "EnsembleError",
    "EnsembleEvaluation",
    "EnsembleObservation",
    "EnsembleUnavailableError",
    "EnsembleValidationError",
    "ModelEvidence",
    "WeightResult",
    "calibration_status",
    "ensemble_id",
]
