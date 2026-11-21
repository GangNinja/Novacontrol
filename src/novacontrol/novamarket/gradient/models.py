"""PI-06 gradient-boosting contracts.

The module deliberately keeps model *identity* separate from model
*configuration*.  A canonical family (``xgboost`` or ``lightgbm``) and a
content-derived version identify an artifact; the configuration records every
training choice needed to reproduce it.  Forecasts still use the PI-01
``ForecastOutput`` contract, so gradient models cannot introduce a parallel
prediction shape or a second financial authority.

The model is a research/diagnostic component.  It has no order, fill, position,
wallet, ledger or risk field.  NM-08 remains the risk authority and NM-10 remains
the sole paper-execution authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.contracts import ForecastDataError, ForecastValidationError
from novacontrol.novamarket.prediction.models import ForecastHorizon, ForecastOutput
from novacontrol.novamarket.statistical.metrics import StatisticalMetrics

PI06_PHASE = "PI-06"
GRADIENT_CONTRACT_VERSION = "pi06.gradient-forecast.1.0"
GRADIENT_METHODOLOGY_VERSION = "pi06-gradient-methodology-1.0"
GRADIENT_MODEL_VERSION_PREFIX = "pi06-gb-1.0"
GRADIENT_SCHEMA_VERSION = 1
GRADIENT_FEATURE_CONTRACT = "pi06.gradient-features.1.0"

_INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class GradientError(RuntimeError):
    """Base class for a PI-06 refusal or training failure."""


class GradientValidationError(GradientError, ForecastValidationError):
    """A request/config/schema violates a PI-06 contract."""


class GradientUnavailableError(GradientError, ForecastDataError):
    """A required backend, artifact or trained model is unavailable."""


class GradientTrainingError(GradientError):
    """A fit failed without taking down the other model family."""


class GradientFamily(StrEnum):
    XGBOOST = "xgboost"
    LIGHTGBM = "lightgbm"


class GradientStatus(StrEnum):
    TRAINED = "trained"
    INSUFFICIENT_DATA = "insufficient_data"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    BASELINES_PREFERRED = "baselines_preferred"
    GRADIENT_SUITABLE = "gradient_suitable"
    PROVISIONAL = "provisional"


def family_from(value: GradientFamily | str) -> GradientFamily:
    try:
        return GradientFamily(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in GradientFamily)
        raise GradientValidationError(
            f"unknown gradient family {value!r}; known: {allowed}"
        ) from exc


def digest_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise GradientValidationError(f"{name} must be numeric, not bool")
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise GradientValidationError(f"{name} must be numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise GradientValidationError(f"{name} must be finite")
    return number


def _integer(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GradientValidationError(f"{name} must be an integer")
    if not low <= value <= high:
        raise GradientValidationError(f"{name} must be between {low} and {high}")
    return value


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GradientValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise GradientValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise GradientValidationError(
            f"{name} must be timezone-aware; naive market time is not accepted"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class GradientConfig:
    """Bounded, reproducible gradient-boosting parameters.

    The defaults favour a small, CPU-only model on a local workstation.  Tuning
    is a short deterministic grid evaluated on validation data only; there is no
    unrestricted or random search in this phase.
    """

    n_estimators: int = 160
    max_depth: int = 4
    num_leaves: int = 15
    learning_rate: float = 0.05
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: float = 2.0
    min_child_samples: int = 20
    reg_lambda: float = 1.0
    reg_alpha: float = 0.0
    early_stopping_rounds: int = 20
    random_seed: int = 42
    n_jobs: int = 2
    neutral_band_pct: float = 0.5
    round_trip_cost_bps: float = 10.0
    interval_level: float = 0.80
    min_train_samples: int = 30
    min_validation_samples: int = 5
    min_test_samples: int = 3
    max_missing_fraction: float = 0.60
    max_inference_missing_fraction: float = 0.25
    max_interval_width: float = 0.50
    extrapolation_z: float = 8.0
    max_extrapolation_fraction: float = 0.20
    max_samples: int = 5_000
    max_features: int = 256
    window_bars: int = 60
    train_ratio: float = 0.60
    validation_ratio: float = 0.20
    min_candidates: int = 3

    def __post_init__(self) -> None:
        _integer(self.n_estimators, "n_estimators", 10, 2_000)
        _integer(self.max_depth, "max_depth", 2, 12)
        _integer(self.num_leaves, "num_leaves", 2, 255)
        if self.num_leaves > 2**self.max_depth:
            raise GradientValidationError(
                "num_leaves cannot exceed 2**max_depth for a bounded tree"
            )
        for name in ("learning_rate", "subsample", "colsample_bytree"):
            value = _finite(getattr(self, name), name)
            if not 0.0 < value <= 1.0:
                raise GradientValidationError(f"{name} must be within (0, 1]")
        for name in (
            "min_child_weight",
            "reg_lambda",
            "reg_alpha",
            "neutral_band_pct",
            "round_trip_cost_bps",
        ):
            if _finite(getattr(self, name), name) < 0:
                raise GradientValidationError(f"{name} must be non-negative")
        _integer(self.min_child_samples, "min_child_samples", 1, 10_000)
        _integer(self.early_stopping_rounds, "early_stopping_rounds", 0, 200)
        _integer(self.random_seed, "random_seed", 0, 2**31 - 1)
        _integer(self.n_jobs, "n_jobs", 1, 4)
        interval = _finite(self.interval_level, "interval_level")
        if not 0.5 <= interval < 1.0:
            raise GradientValidationError("interval_level must be within [0.5, 1)")
        _integer(self.min_train_samples, "min_train_samples", 10, 20_000)
        _integer(self.min_validation_samples, "min_validation_samples", 1, 5_000)
        _integer(self.min_test_samples, "min_test_samples", 1, 5_000)
        missing = _finite(self.max_missing_fraction, "max_missing_fraction")
        if not 0.0 <= missing < 1.0:
            raise GradientValidationError(
                "max_missing_fraction must be within [0, 1)"
            )
        inference_missing = _finite(
            self.max_inference_missing_fraction, "max_inference_missing_fraction"
        )
        if not 0.0 <= inference_missing <= 1.0:
            raise GradientValidationError(
                "max_inference_missing_fraction must be within [0, 1]"
            )
        interval_width = _finite(self.max_interval_width, "max_interval_width")
        if not 0.0 < interval_width <= 5.0:
            raise GradientValidationError(
                "max_interval_width must be within (0, 5]"
            )
        if _finite(self.extrapolation_z, "extrapolation_z") <= 0:
            raise GradientValidationError("extrapolation_z must be positive")
        extrapolation_fraction = _finite(
            self.max_extrapolation_fraction, "max_extrapolation_fraction"
        )
        if not 0.0 <= extrapolation_fraction <= 1.0:
            raise GradientValidationError(
                "max_extrapolation_fraction must be within [0, 1]"
            )
        _integer(self.max_samples, "max_samples", 50, 100_000)
        _integer(self.max_features, "max_features", 1, 1_000)
        _integer(self.window_bars, "window_bars", 10, 1_000)
        train = _finite(self.train_ratio, "train_ratio")
        validation = _finite(self.validation_ratio, "validation_ratio")
        if not 0.0 < train < 1.0 or not 0.0 < validation < 1.0:
            raise GradientValidationError("split ratios must be within (0, 1)")
        if train + validation >= 1.0:
            raise GradientValidationError(
                "train + validation must leave a test portion"
            )
        _integer(self.min_candidates, "min_candidates", 1, 3)

    @property
    def effective_neutral_band_pct(self) -> float:
        """A movement must clear both the noise band and round-trip costs."""
        return max(float(self.neutral_band_pct), float(self.round_trip_cost_bps) / 100.0)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> GradientConfig:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise GradientValidationError("gradient config must be an object")
        body = {
            key: item
            for key, item in dict(value).items()
            # Descriptive fields are serialized alongside the parameters for
            # auditability but are not constructor inputs.
            if key not in {"target_definition", "direction_rule"}
        }
        unknown = sorted(set(body) - set(cls.__dataclass_fields__))
        if unknown:
            raise GradientValidationError(
                "unknown gradient config key(s): " + ", ".join(unknown)
            )
        return cls(**body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_estimators": self.n_estimators,
            "max_depth": self.max_depth,
            "num_leaves": self.num_leaves,
            "learning_rate": self.learning_rate,
            "subsample": self.subsample,
            "colsample_bytree": self.colsample_bytree,
            "min_child_weight": self.min_child_weight,
            "min_child_samples": self.min_child_samples,
            "reg_lambda": self.reg_lambda,
            "reg_alpha": self.reg_alpha,
            "early_stopping_rounds": self.early_stopping_rounds,
            "random_seed": self.random_seed,
            "n_jobs": self.n_jobs,
            "neutral_band_pct": self.neutral_band_pct,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "interval_level": self.interval_level,
            "min_train_samples": self.min_train_samples,
            "min_validation_samples": self.min_validation_samples,
            "min_test_samples": self.min_test_samples,
            "max_missing_fraction": self.max_missing_fraction,
            "max_inference_missing_fraction": self.max_inference_missing_fraction,
            "max_interval_width": self.max_interval_width,
            "extrapolation_z": self.extrapolation_z,
            "max_extrapolation_fraction": self.max_extrapolation_fraction,
            "max_samples": self.max_samples,
            "max_features": self.max_features,
            "window_bars": self.window_bars,
            "train_ratio": self.train_ratio,
            "validation_ratio": self.validation_ratio,
            "min_candidates": self.min_candidates,
            "target_definition": "future_close / decision_close - 1",
            "direction_rule": (
                "up/down outside max(neutral_band_pct, round_trip_cost_bps/100); "
                "neutral inside; uncertain when a supported interval crosses zero"
            ),
        }

    @property
    def digest(self) -> str:
        return digest_payload(self.to_dict())[:16]

    def candidates(self) -> tuple[GradientConfig, ...]:
        """Three bounded candidates; validation selects among them."""
        shallow = replace(
            self,
            max_depth=3,
            num_leaves=min(7, self.num_leaves),
            n_estimators=min(self.n_estimators, 120),
            learning_rate=min(self.learning_rate, 0.05),
            min_child_samples=max(self.min_child_samples, 20),
        )
        deep = replace(
            self,
            max_depth=min(6, max(5, self.max_depth)),
            num_leaves=min(31, max(self.num_leaves, 15)),
            n_estimators=min(self.n_estimators, 220),
            learning_rate=min(self.learning_rate, 0.03),
            min_child_samples=max(self.min_child_samples, 25),
            reg_lambda=max(self.reg_lambda, 2.0),
        )
        return (self, shallow, deep)


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """The exact ordered feature contract persisted with an artifact."""

    names: tuple[str, ...]
    feature_set_version: str
    profile: str
    source_names: tuple[str, ...]
    dropped: Mapping[str, str] = field(default_factory=dict)
    contract_version: str = GRADIENT_FEATURE_CONTRACT
    digest_value: str = ""

    def __post_init__(self) -> None:
        if not self.names:
            raise GradientValidationError("a feature schema needs at least one column")
        if len(set(self.names)) != len(self.names):
            raise GradientValidationError("feature schema names must be unique")
        if not self.feature_set_version:
            raise GradientValidationError("feature_set_version is required")
        if len(self.names) > 1_000:
            raise GradientValidationError("feature schema exceeds the bounded column cap")
        object.__setattr__(self, "source_names", tuple(str(item) for item in self.source_names))
        object.__setattr__(self, "dropped", dict(self.dropped))
        object.__setattr__(
            self,
            "digest_value",
            self.digest_value
            or digest_payload(
                {
                    "names": list(self.names),
                    "feature_set_version": self.feature_set_version,
                    "profile": self.profile,
                    "source_names": list(self.source_names),
                    "dropped": dict(self.dropped),
                    "contract_version": self.contract_version,
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "names": list(self.names),
            "feature_set_version": self.feature_set_version,
            "profile": self.profile,
            "source_names": list(self.source_names),
            "dropped": dict(self.dropped),
            "contract_version": self.contract_version,
            "digest": self.digest_value,
            "order_is_significant": True,
        }


@dataclass(frozen=True, slots=True)
class FeatureStats:
    """Training-only distribution used to detect out-of-distribution inputs."""

    medians: Mapping[str, float]
    scales: Mapping[str, float]
    minimums: Mapping[str, float]
    maximums: Mapping[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "medians": dict(self.medians),
            "scales": dict(self.scales),
            "minimums": dict(self.minimums),
            "maximums": dict(self.maximums),
        }


@dataclass(frozen=True, slots=True)
class IntervalCalibration:
    """Validation-residual interval, not a calibrated probability."""

    level: float
    lower_offset: float
    upper_offset: float
    samples: int
    method: str = "validation_residual_quantiles"
    supported: bool = True

    def apply(self, predicted_return: float) -> tuple[float, float]:
        if not self.supported:
            return predicted_return, predicted_return
        lower = min(predicted_return + self.lower_offset, predicted_return)
        upper = max(predicted_return + self.upper_offset, predicted_return)
        return lower, upper

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "lower_offset": self.lower_offset,
            "upper_offset": self.upper_offset,
            "samples": self.samples,
            "method": self.method,
            "supported": self.supported,
            "calibrated_probability": False,
            "guarantee": False,
        }


@dataclass(frozen=True, slots=True)
class ClassifierReport:
    classes: tuple[str, ...]
    accuracy: float | None
    macro_f1: float | None
    confusion: Mapping[str, Mapping[str, int]]
    samples: int
    status: str = "implemented"
    probabilities_calibrated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "classes": list(self.classes),
            "accuracy": self.accuracy,
            "macro_f1": self.macro_f1,
            "confusion": {key: dict(row) for key, row in self.confusion.items()},
            "samples": self.samples,
            "status": self.status,
            "probabilities_calibrated": False,
            "probabilities_withheld": True,
        }


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    family: GradientFamily
    version: str
    regressor_path: str
    regressor_sha256: str
    regressor_bytes: int
    classifier_path: str = ""
    classifier_sha256: str = ""
    classifier_bytes: int = 0
    format: str = "native"
    loaded_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "version": self.version,
            "regressor_path": self.regressor_path,
            "regressor_sha256": self.regressor_sha256,
            "regressor_bytes": self.regressor_bytes,
            "classifier_path": self.classifier_path,
            "classifier_sha256": self.classifier_sha256,
            "classifier_bytes": self.classifier_bytes,
            "format": self.format,
            "loaded_verified": self.loaded_verified,
        }


@dataclass(frozen=True, slots=True)
class FamilyResult:
    family: GradientFamily
    version: str
    status: GradientStatus
    config: GradientConfig
    schema: FeatureSchema | None
    validation: StatisticalMetrics | None
    test: StatisticalMetrics | None
    classifier_validation: ClassifierReport | None
    classifier_test: ClassifierReport | None
    interval: IntervalCalibration | None
    feature_importance: tuple[Mapping[str, Any], ...]
    artifact: ModelArtifact | None
    train_samples: int
    validation_samples: int
    test_samples: int
    training_start: datetime | None
    training_end: datetime | None
    dependency_version: str
    reason: str = ""
    notes: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    promotion: str = "not_promoted"
    selected: bool = False
    regime_metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    regime_samples: Mapping[str, int] = field(default_factory=dict)
    walk_forward: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "model_id": self.family.value,
            "version": self.version,
            "model_version": self.version,
            "status": self.status.value,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "feature_schema": self.schema.to_dict() if self.schema else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "test": self.test.to_dict() if self.test else None,
            "classifier_validation": (
                self.classifier_validation.to_dict()
                if self.classifier_validation
                else None
            ),
            "classifier_test": (
                self.classifier_test.to_dict() if self.classifier_test else None
            ),
            "interval": self.interval.to_dict() if self.interval else None,
            "feature_importance": [dict(item) for item in self.feature_importance],
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "train_samples": self.train_samples,
            "validation_samples": self.validation_samples,
            "test_samples": self.test_samples,
            "training_start": (
                self.training_start.isoformat() if self.training_start else None
            ),
            "training_end": self.training_end.isoformat() if self.training_end else None,
            "dependency_version": self.dependency_version,
            "reason": self.reason,
            "notes": list(self.notes),
            "failures": list(self.failures),
            "promotion": self.promotion,
            "selected": self.selected,
            "regime_metrics": {
                key: dict(value) for key, value in self.regime_metrics.items()
            },
            "regime_samples": dict(self.regime_samples),
            "walk_forward": [dict(item) for item in self.walk_forward],
        }

    def registry_metrics(self, report_id: str = "") -> dict[str, Any]:
        return {
            "methodology_version": GRADIENT_METHODOLOGY_VERSION,
            "contract_version": GRADIENT_CONTRACT_VERSION,
            "report_id": report_id,
            "instrument_id": None,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "feature_schema": self.schema.to_dict() if self.schema else None,
            "feature_set_version": (
                self.schema.feature_set_version if self.schema else "unspecified"
            ),
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "artifact_location": self.artifact.regressor_path if self.artifact else "",
            "validation": self.validation.to_dict() if self.validation else None,
            "test": self.test.to_dict() if self.test else None,
            "classifier_validation": (
                self.classifier_validation.to_dict()
                if self.classifier_validation
                else None
            ),
            "classifier_test": (
                self.classifier_test.to_dict() if self.classifier_test else None
            ),
            "interval": self.interval.to_dict() if self.interval else None,
            "feature_importance": [dict(item) for item in self.feature_importance],
            "train_samples": self.train_samples,
            "validation_samples": self.validation_samples,
            "test_samples": self.test_samples,
            "training_start": (
                self.training_start.isoformat() if self.training_start else None
            ),
            "training_end": self.training_end.isoformat() if self.training_end else None,
            "dependency_version": self.dependency_version,
            "promotion": "not_promoted",
            "automatic_promotion": False,
            "status": self.status.value,
            "reason": self.reason,
            "limitations": list(self.notes),
            "walk_forward": [dict(item) for item in self.walk_forward],
            "regime_metrics": {
                key: dict(value) for key, value in self.regime_metrics.items()
            },
            "regime_samples": dict(self.regime_samples),
        }


@dataclass(frozen=True, slots=True)
class GradientEvaluationReport:
    instrument_id: str
    horizon: ForecastHorizon
    resolution: str
    horizon_steps: int
    dataset_id: str
    dataset_version: str
    feature_set_version: str
    feature_profile: str
    target_definition: str
    train_range: Mapping[str, Any]
    validation_range: Mapping[str, Any]
    test_range: Mapping[str, Any]
    families: Mapping[str, FamilyResult]
    baselines: Mapping[str, StatisticalMetrics]
    selected_family: str | None
    selected_version: str | None
    selected_config: GradientConfig | None
    preferred_overall: str
    selection_metric: str
    suitability: Mapping[str, Mapping[str, Any]]
    regime_samples: Mapping[str, int]
    insufficient_regimes: tuple[str, ...]
    failures: tuple[str, ...]
    notes: tuple[str, ...]
    report_id: str = ""
    test_used_for_selection: bool = False
    leakage_safe: bool = True
    automatic_promotion: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "phase": PI06_PHASE,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "resolution": self.resolution,
            "horizon_steps": self.horizon_steps,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "feature_profile": self.feature_profile,
            "target_definition": self.target_definition,
            "splits": {
                "train": dict(self.train_range),
                "validation": dict(self.validation_range),
                "test": dict(self.test_range),
            },
            "families": {
                key: value.to_dict() for key, value in self.families.items()
            },
            "baselines": {
                key: value.to_dict() for key, value in self.baselines.items()
            },
            "selected_family": self.selected_family,
            "selected_version": self.selected_version,
            "selected_config": (
                self.selected_config.to_dict() if self.selected_config else None
            ),
            "preferred_overall": self.preferred_overall,
            "selection_metric": self.selection_metric,
            "suitability": {
                key: dict(value) for key, value in self.suitability.items()
            },
            "regime_samples": dict(self.regime_samples),
            "insufficient_regimes": list(self.insufficient_regimes),
            "failures": list(self.failures),
            "notes": list(self.notes),
            "test_used_for_selection": self.test_used_for_selection,
            "automatic_promotion": self.automatic_promotion,
            "leakage_safe": self.leakage_safe,
            "authorities": {"risk": "NM-08", "execution": "NM-10"},
        }


@dataclass(frozen=True, slots=True)
class LoadedGradient:
    """A native booster loaded from disk for inference/explanation."""

    family: GradientFamily
    version: str
    config: GradientConfig
    schema: FeatureSchema
    regressor: Any
    classifier: Any
    regressor_iterations: int
    classifier_iterations: int
    interval: IntervalCalibration
    feature_stats: FeatureStats
    feature_importance: tuple[Mapping[str, Any], ...]
    artifact: ModelArtifact


@dataclass(frozen=True, slots=True)
class GradientForecast:
    """The PI-01 forecast plus the PI-06 artifact/explanation provenance."""

    contract: ForecastOutput
    family: FamilyResult
    schema: FeatureSchema
    feature_values: Mapping[str, float | None]
    classifier_probabilities: Mapping[str, float] | None
    interval: IntervalCalibration
    out_of_distribution: bool
    missing_fraction: float
    explanation_available: bool = True
    persisted: bool = False

    def to_dict(self) -> dict[str, Any]:
        body = self.contract.to_dict()
        body.update(
            {
                "phase": PI06_PHASE,
                "gradient_contract_version": GRADIENT_CONTRACT_VERSION,
                "gradient_family": self.family.family.value,
                "gradient_model": self.family.to_dict(),
                "feature_schema": self.schema.to_dict(),
                "feature_values": dict(self.feature_values),
                "classifier_probabilities": (
                    dict(self.classifier_probabilities)
                    if self.classifier_probabilities
                    else None
                ),
                "classifier_probabilities_calibrated": False,
                "interval": self.interval.to_dict(),
                "out_of_distribution": self.out_of_distribution,
                "missing_feature_fraction": self.missing_fraction,
                "explanation_available": self.explanation_available,
                "persisted": self.persisted,
                "promotion": "not_promoted",
                "automatic_promotion": False,
                "authorities": {"risk": "NM-08", "execution": "NM-10"},
                "guarantees": [
                    "features are observed at or before the requested cutoff",
                    "the target is strictly later than the decision bar",
                    "validation selects the candidate; test data is reported once",
                    "a tree model's raw score is not represented as a probability",
                ],
            }
        )
        return body


@dataclass(frozen=True, slots=True)
class GradientExplanation:
    forecast_id: str
    instrument_id: str
    horizon: str
    model_id: str
    model_version: str
    method: str
    global_importance: tuple[Mapping[str, Any], ...]
    local_contributions: tuple[Mapping[str, Any], ...]
    feature_values: Mapping[str, float | None]
    base_value: float | None
    prediction: float
    limitations: tuple[str, ...]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "method": self.method,
            "global_importance": [dict(item) for item in self.global_importance],
            "local_contributions": [
                dict(item) for item in self.local_contributions
            ],
            "feature_values": dict(self.feature_values),
            "base_value": self.base_value,
            "prediction": self.prediction,
            "generated_at": self.generated_at.isoformat(),
            "limitations": list(self.limitations),
            "contract_version": GRADIENT_CONTRACT_VERSION,
            "explanation_is_prediction": False,
            "automatic_promotion": False,
        }


def model_version(
    family: GradientFamily,
    *,
    instrument_id: str,
    horizon: ForecastHorizon | str,
    dataset_id: str,
    dataset_version: str,
    feature_set_version: str,
    target_steps: int,
    config_digest: str,
    train_start: datetime | None,
    train_end: datetime | None,
) -> str:
    if not _INSTRUMENT_ID.match(instrument_id):
        raise GradientValidationError("instrument_id is not canonical")
    token = digest_payload(
        {
            "family": family.value,
            "instrument_id": instrument_id,
            "horizon": getattr(horizon, "value", str(horizon)),
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "feature_set_version": feature_set_version,
            "target_steps": int(target_steps),
            "config": config_digest,
            "train_start": train_start.isoformat() if train_start else "",
            "train_end": train_end.isoformat() if train_end else "",
            "methodology": GRADIENT_METHODOLOGY_VERSION,
        }
    )[:16]
    version = f"{GRADIENT_MODEL_VERSION_PREFIX}-{family.value}-{token}"
    if not _VERSION.match(version):
        raise GradientValidationError("derived artifact version is not path-safe")
    return version


__all__ = [
    "GRADIENT_CONTRACT_VERSION",
    "GRADIENT_FEATURE_CONTRACT",
    "GRADIENT_METHODOLOGY_VERSION",
    "GRADIENT_MODEL_VERSION_PREFIX",
    "PI06_PHASE",
    "GRADIENT_SCHEMA_VERSION",
    "ClassifierReport",
    "FamilyResult",
    "FeatureSchema",
    "FeatureStats",
    "GradientConfig",
    "GradientError",
    "GradientEvaluationReport",
    "GradientExplanation",
    "GradientFamily",
    "GradientForecast",
    "GradientStatus",
    "GradientTrainingError",
    "GradientUnavailableError",
    "GradientValidationError",
    "IntervalCalibration",
    "LoadedGradient",
    "ModelArtifact",
    "digest_payload",
    "family_from",
    "model_version",
]
