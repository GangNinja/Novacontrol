"""PI-07 LSTM contracts, bounded configuration and provenance payloads.

The classes here are deliberately storage-neutral: the authoritative model
registry remains ``PredictionStore`` and the forecast/evaluation history remains
``ForecastStore``.  A sequence model adds an artifact descriptor, a persisted
preprocessing transform and a report shape; it does not add another registry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

import numpy as np

from novacontrol.novamarket.contracts import ForecastDataError, ForecastValidationError
from novacontrol.novamarket.gradient.models import (
    ClassifierReport,
    FeatureSchema,
    FeatureStats,
    IntervalCalibration,
)
from novacontrol.novamarket.prediction.models import (
    ForecastHorizon,
    ForecastOutput,
    HORIZON_SPECS,
)
from novacontrol.novamarket.statistical.metrics import StatisticalMetrics

NDArray = np.ndarray[Any, Any]

PI07_PHASE = "PI-07"
LSTM_CONTRACT_VERSION = "pi07-lstm-contract-v1"
LSTM_SCHEMA_VERSION = "pi07-lstm-schema-v1"
LSTM_METHODOLOGY_VERSION = "pi07-lstm-methodology-v2"
LSTM_MODEL_VERSION_PREFIX = "pi07-lstm-1.1"
LSTM_MODEL_ID = "lstm"
LSTM_FORECAST_PREFIX = "lstm_"
LSTM_RUN_PREFIX = "lstmr_"
LSTM_PERFORMANCE_PREFIX = "lperf_"


class LSTMError(RuntimeError):
    """Base class for PI-07 sequence-model failures."""


class LSTMValidationError(LSTMError, ForecastValidationError):
    """A bad configuration, sequence or target request."""


class LSTMUnavailableError(LSTMError, ForecastDataError):
    """The sequence model cannot produce a defensible forecast."""


class LSTMTrainingError(LSTMError):
    """Training or artifact creation failed without corrupting prior evidence."""


def _finite(value: object, name: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise LSTMValidationError(f"{name} is not numeric: {value!r}") from exc
    if not np.isfinite(number):
        raise LSTMValidationError(f"{name} must be finite")
    return number


def _integer(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LSTMValidationError(f"{name} must be an integer")
    if not low <= value <= high:
        raise LSTMValidationError(f"{name} must be within [{low}, {high}]")
    return value


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LSTMValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise LSTMValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LSTMValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LSTMConfig:
    """Bounded CPU-first sequence model configuration.

    ``sequence_length=0`` means "use the horizon profile".  The bounded grids
    are deterministic; test data is never consulted for a choice.
    """

    sequence_length: int = 0
    hidden_size: int = 32
    num_layers: int = 1
    dropout: float = 0.10
    learning_rate: float = 0.001
    batch_size: int = 32
    max_epochs: int = 40
    patience: int = 6
    weight_decay: float = 1e-4
    gradient_clip: float = 1.0
    optimizer: str = "adam"
    loss: str = "huber"
    classification_loss_weight: float = 0.35
    random_seed: int = 42
    num_threads: int = 2
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
    early_stopping_min_delta: float = 1e-5
    checkpoint_restore: bool = True

    def __post_init__(self) -> None:
        _integer(self.sequence_length, "sequence_length", 0, 120)
        _integer(self.hidden_size, "hidden_size", 8, 256)
        _integer(self.num_layers, "num_layers", 1, 3)
        dropout = _finite(self.dropout, "dropout")
        if not 0.0 <= dropout <= 0.5:
            raise LSTMValidationError("dropout must be within [0, 0.5]")
        lr = _finite(self.learning_rate, "learning_rate")
        if not 0.0 < lr <= 0.1:
            raise LSTMValidationError("learning_rate must be within (0, 0.1]")
        batch = _integer(self.batch_size, "batch_size", 4, 512)
        if batch < 2:
            raise LSTMValidationError("batch_size must be at least 2")
        _integer(self.max_epochs, "max_epochs", 1, 500)
        _integer(self.patience, "patience", 1, 100)
        for name in ("weight_decay", "gradient_clip", "classification_loss_weight"):
            if _finite(getattr(self, name), name) < 0:
                raise LSTMValidationError(f"{name} must be non-negative")
        if self.gradient_clip > 10.0:
            raise LSTMValidationError("gradient_clip must not exceed 10")
        if self.optimizer not in {"adam", "adamw"}:
            raise LSTMValidationError("optimizer must be 'adam' or 'adamw'")
        if self.loss not in {"huber", "mse"}:
            raise LSTMValidationError("loss must be 'huber' or 'mse'")
        _integer(self.random_seed, "random_seed", 0, 2**31 - 1)
        _integer(self.num_threads, "num_threads", 1, 8)
        for name in (
            "neutral_band_pct",
            "round_trip_cost_bps",
            "min_train_samples",
            "min_validation_samples",
            "min_test_samples",
            "max_features",
            "window_bars",
            "max_samples",
            "min_candidates",
        ):
            if name in {"min_train_samples", "min_validation_samples", "min_test_samples"}:
                _integer(getattr(self, name), name, 1, 20_000)
            elif name in {"max_features", "window_bars", "max_samples"}:
                _integer(getattr(self, name), name, 1, 100_000)
            elif name == "min_candidates":
                _integer(self.min_candidates, "min_candidates", 1, 3)
            elif _finite(getattr(self, name), name) < 0:
                raise LSTMValidationError(f"{name} must be non-negative")
        interval = _finite(self.interval_level, "interval_level")
        if not 0.5 <= interval < 1.0:
            raise LSTMValidationError("interval_level must be within [0.5, 1)")
        missing = _finite(self.max_missing_fraction, "max_missing_fraction")
        if not 0.0 <= missing < 1.0:
            raise LSTMValidationError("max_missing_fraction must be within [0, 1)")
        inference_missing = _finite(
            self.max_inference_missing_fraction, "max_inference_missing_fraction"
        )
        if not 0.0 <= inference_missing <= 1.0:
            raise LSTMValidationError(
                "max_inference_missing_fraction must be within [0, 1]"
            )
        width = _finite(self.max_interval_width, "max_interval_width")
        if not 0.0 < width <= 5.0:
            raise LSTMValidationError("max_interval_width must be within (0, 5]")
        if _finite(self.extrapolation_z, "extrapolation_z") <= 0:
            raise LSTMValidationError("extrapolation_z must be positive")
        fraction = _finite(self.max_extrapolation_fraction, "max_extrapolation_fraction")
        if not 0.0 <= fraction <= 1.0:
            raise LSTMValidationError("max_extrapolation_fraction must be within [0, 1]")
        train = _finite(self.train_ratio, "train_ratio")
        validation = _finite(self.validation_ratio, "validation_ratio")
        if not 0.0 < train < 1.0 or not 0.0 < validation < 1.0:
            raise LSTMValidationError("split ratios must be within (0, 1)")
        if train + validation >= 1.0:
            raise LSTMValidationError("train + validation must leave a test portion")
        if _finite(self.early_stopping_min_delta, "early_stopping_min_delta") < 0:
            raise LSTMValidationError("early_stopping_min_delta must be non-negative")

    @property
    def effective_neutral_band_pct(self) -> float:
        return max(
            float(self.neutral_band_pct), float(self.round_trip_cost_bps) / 100.0
        )

    def sequence_length_for(
        self, horizon: ForecastHorizon, resolution: Any
    ) -> int:
        """A configurable horizon-specific default, never one universal window."""
        if self.sequence_length:
            return self.sequence_length
        family = HORIZON_SPECS[horizon].family.value
        resolution_value = getattr(resolution, "value", str(resolution))
        if family == "intraday":
            return 30 if resolution_value == "1m" else 20
        if horizon in {
            ForecastHorizon.DAY_1,
            ForecastHorizon.DAYS_3,
            ForecastHorizon.DAYS_5,
        }:
            return 30
        return 15

    def matrix_config(self) -> Any:
        """Reuse PI-06's validated PI-02/PI-04 matrix builder."""
        from novacontrol.novamarket.gradient.models import GradientConfig

        return GradientConfig(
            neutral_band_pct=self.neutral_band_pct,
            round_trip_cost_bps=self.round_trip_cost_bps,
            interval_level=self.interval_level,
            min_train_samples=self.min_train_samples,
            min_validation_samples=self.min_validation_samples,
            min_test_samples=self.min_test_samples,
            max_missing_fraction=self.max_missing_fraction,
            max_inference_missing_fraction=self.max_inference_missing_fraction,
            max_interval_width=self.max_interval_width,
            extrapolation_z=self.extrapolation_z,
            max_extrapolation_fraction=self.max_extrapolation_fraction,
            max_samples=self.max_samples,
            max_features=self.max_features,
            window_bars=self.window_bars,
            train_ratio=self.train_ratio,
            validation_ratio=self.validation_ratio,
            random_seed=self.random_seed,
            n_jobs=self.num_threads,
            min_candidates=self.min_candidates,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "LSTMConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise LSTMValidationError("lstm config must be an object")
        body = dict(value)
        generated = {
            "effective_neutral_band_pct",
            "target_definition",
            "sequence_normalization",
            "probability_rule",
        }
        body = {key: item for key, item in body.items() if key not in generated}
        unknown = sorted(set(body) - set(cls.__dataclass_fields__))
        if unknown:
            raise LSTMValidationError(
                "unknown lstm config key(s): " + ", ".join(unknown)
            )
        return cls(**body)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        } | {
            "effective_neutral_band_pct": self.effective_neutral_band_pct,
            "target_definition": "future_close / decision_close - 1",
            "sequence_normalization": "training-only robust median/IQR feature scaling",
            "probability_rule": "uncalibrated classifier probabilities are withheld",
        }

    @property
    def digest(self) -> str:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        return _digest(payload)[:16]

    def candidates(self) -> tuple["LSTMConfig", ...]:
        """Three bounded candidates; validation chooses, test never does."""
        smaller = replace(
            self,
            hidden_size=max(8, self.hidden_size // 2),
            num_layers=1,
            dropout=min(self.dropout, 0.10),
            learning_rate=max(1e-5, self.learning_rate / 1.5),
            max_epochs=min(self.max_epochs, 30),
        )
        larger = replace(
            self,
            hidden_size=min(128, max(32, self.hidden_size * 2)),
            num_layers=min(2, self.num_layers + 1),
            dropout=max(0.15, self.dropout),
            learning_rate=max(1e-5, self.learning_rate * 0.7),
        )
        return (self, smaller, larger)[: self.min_candidates]


def _digest(payload: Mapping[str, Any]) -> str:
    import json

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SequenceScaler:
    """Training-only feature/target transform persisted with the model."""

    schema: FeatureSchema
    medians: Mapping[str, float]
    scales: Mapping[str, float]
    target_mean: float
    target_std: float
    method: str = "robust_median_iqr"

    def __post_init__(self) -> None:
        names = set(self.schema.names)
        if set(self.medians) != names or set(self.scales) != names:
            raise LSTMValidationError(
                "scaler columns do not match the persisted feature schema"
            )
        clean_medians = {name: float(self.medians[name]) for name in self.schema.names}
        clean_scales = {
            name: (float(self.scales[name]) if abs(float(self.scales[name])) > 1e-12 else 1.0)
            for name in self.schema.names
        }
        if not np.isfinite(list(clean_medians.values())).all():
            raise LSTMValidationError("scaler medians must be finite")
        if not np.isfinite(list(clean_scales.values())).all():
            raise LSTMValidationError("scaler scales must be finite")
        target_mean = float(self.target_mean)
        target_std = float(self.target_std)
        if not np.isfinite(target_mean):
            raise LSTMValidationError("target mean must be finite")
        if not np.isfinite(target_std):
            raise LSTMValidationError("target scale must be finite")
        object.__setattr__(self, "medians", clean_medians)
        object.__setattr__(self, "scales", clean_scales)
        object.__setattr__(self, "target_mean", target_mean)
        object.__setattr__(self, "target_std", target_std if abs(target_std) > 1e-12 else 1.0)

    def transform(self, raw: NDArray) -> tuple[NDArray, NDArray]:
        values = np.asarray(raw, dtype=np.float64)
        if values.ndim != 3 or values.shape[2] != len(self.schema.names):
            raise LSTMValidationError("sequence input shape does not match the schema")
        mask = np.isfinite(values).astype(np.float64)
        scaled = np.zeros_like(values, dtype=np.float64)
        for index, name in enumerate(self.schema.names):
            median = float(self.medians[name])
            scale = float(self.scales[name])
            column = values[:, :, index]
            finite = np.isfinite(column)
            scaled[:, :, index] = np.where(
                finite, (column - median) / scale, 0.0
            )
        return scaled, mask

    def inverse_target(self, value: float) -> float:
        number = float(value) * float(self.target_std) + float(self.target_mean)
        if not np.isfinite(number):
            raise LSTMValidationError("the model produced a non-finite target")
        return number

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema.to_dict(),
            "medians": dict(self.medians),
            "scales": dict(self.scales),
            "target_mean": self.target_mean,
            "target_std": self.target_std,
            "method": self.method,
            "fit_on": "training split only",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SequenceScaler":
        if not isinstance(payload, Mapping):
            raise LSTMValidationError("scaler payload must be an object")
        schema_payload = payload.get("schema")
        if not isinstance(schema_payload, Mapping):
            raise LSTMValidationError("scaler schema is missing")
        names = tuple(str(item) for item in (schema_payload.get("names") or ()))
        schema = FeatureSchema(
            names=names,
            feature_set_version=str(schema_payload.get("feature_set_version") or ""),
            profile=str(schema_payload.get("profile") or ""),
            source_names=tuple(str(item) for item in (schema_payload.get("source_names") or ())),
            dropped={
                str(key): str(value)
                for key, value in (schema_payload.get("dropped") or {}).items()
            },
            contract_version=str(schema_payload.get("contract_version") or ""),
            digest_value=str(schema_payload.get("digest") or ""),
        )
        return cls(
            schema=schema,
            medians={
                str(key): float(value)
                for key, value in (payload.get("medians") or {}).items()
            },
            scales={
                str(key): float(value)
                for key, value in (payload.get("scales") or {}).items()
            },
            target_mean=float(payload.get("target_mean") or 0.0),
            target_std=float(payload.get("target_std") or 1.0),
            method=str(payload.get("method") or "robust_median_iqr"),
        )


@dataclass(frozen=True, slots=True)
class LSTMArtifact:
    version: str
    state_path: str
    state_sha256: str
    state_bytes: int
    architecture: Mapping[str, Any]
    format: str = "torch_state_dict"
    loaded_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "state_path": self.state_path,
            "state_sha256": self.state_sha256,
            "state_bytes": self.state_bytes,
            "architecture": dict(self.architecture),
            "format": self.format,
            "loaded_verified": self.loaded_verified,
        }


@dataclass(frozen=True, slots=True)
class LSTMResult:
    model_id: str
    version: str
    status: str
    config: LSTMConfig
    schema: FeatureSchema | None
    scaler: SequenceScaler | None
    validation: StatisticalMetrics | None
    test: StatisticalMetrics | None
    classifier_validation: ClassifierReport | None
    classifier_test: ClassifierReport | None
    interval: IntervalCalibration | None
    feature_importance: tuple[Mapping[str, Any], ...]
    artifact: LSTMArtifact | None
    train_samples: int
    validation_samples: int
    test_samples: int
    training_start: datetime | None
    training_end: datetime | None
    dependency_version: str
    feature_stats: FeatureStats | None = None
    sequence_length: int = 0
    training_duration_seconds: float = 0.0
    inference_latency_ms: float = 0.0
    peak_memory_mb: float = 0.0
    best_epoch: int = 0
    epochs_observed: int = 0
    early_stopped: bool = False
    checkpoint_restored: bool = False
    selected: bool = False
    reason: str = ""
    notes: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    promotion: str = "not_promoted"
    regime_metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    regime_samples: Mapping[str, int] = field(default_factory=dict)
    walk_forward: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": "lstm",
            "model_version": self.version,
            "version": self.version,
            "status": self.status,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "feature_schema": self.schema.to_dict() if self.schema else None,
            "scaler": self.scaler.to_dict() if self.scaler else None,
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
            "training_end": (
                self.training_end.isoformat() if self.training_end else None
            ),
            "dependency_version": self.dependency_version,
            "feature_stats": self.feature_stats.to_dict() if self.feature_stats else None,
            "sequence_length": self.sequence_length,
            "training_duration_seconds": round(self.training_duration_seconds, 6),
            "inference_latency_ms": round(self.inference_latency_ms, 6),
            "peak_memory_mb": round(self.peak_memory_mb, 3),
            "best_epoch": self.best_epoch,
            "epochs_observed": self.epochs_observed,
            "early_stopped": self.early_stopped,
            "checkpoint_restored": self.checkpoint_restored,
            "selected": self.selected,
            "reason": self.reason,
            "notes": list(self.notes),
            "failures": list(self.failures),
            "promotion": self.promotion,
            "regime_metrics": {
                key: dict(value) for key, value in self.regime_metrics.items()
            },
            "regime_samples": dict(self.regime_samples),
            "walk_forward": [dict(item) for item in self.walk_forward],
        }

    def registry_metrics(self, report_id: str = "") -> dict[str, Any]:
        return {
            "phase": PI07_PHASE,
            "contract_version": LSTM_CONTRACT_VERSION,
            "schema_version": LSTM_SCHEMA_VERSION,
            "methodology_version": LSTM_METHODOLOGY_VERSION,
            "status": self.status,
            "report_id": report_id,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "sequence_length": self.sequence_length,
            "architecture": {
                "hidden_size": self.config.hidden_size,
                "num_layers": self.config.num_layers,
                "dropout": self.config.dropout,
                "input": "last hidden state of an LSTM over scaled PI-04 feature windows",
                "heads": "return regression + three-class direction",
            },
            "training": {
                "epochs": self.epochs_observed,
                "best_epoch": self.best_epoch,
                "patience": self.config.patience,
                "early_stopped": self.early_stopped,
                "checkpoint_restored": self.checkpoint_restored,
                "duration_seconds": self.training_duration_seconds,
                "optimizer": self.config.optimizer,
                "loss": self.config.loss,
            },
            "preprocessing": self.scaler.to_dict() if self.scaler else None,
            "scaler": self.scaler.to_dict() if self.scaler else None,
            "feature_schema": self.schema.to_dict() if self.schema else None,
            "feature_stats": self.feature_stats.to_dict() if self.feature_stats else None,
            "interval": self.interval.to_dict() if self.interval else None,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "test": self.test.to_dict() if self.test else None,
            "classifier_test": (
                self.classifier_test.to_dict() if self.classifier_test else None
            ),
            "walk_forward": [dict(item) for item in self.walk_forward],
            "feature_importance": [dict(item) for item in self.feature_importance],
            "resource": {
                "inference_latency_ms": self.inference_latency_ms,
                "peak_memory_mb": self.peak_memory_mb,
                "device": "cpu",
            },
            "production_ready": False,
            "automatic_promotion": False,
        }


@dataclass(frozen=True, slots=True)
class LSTMEvaluationReport:
    instrument_id: str
    horizon: ForecastHorizon
    resolution: Any
    horizon_steps: int
    dataset_id: str
    dataset_version: str
    feature_set_version: str
    feature_profile: str
    target_definition: str
    train_range: Mapping[str, Any]
    validation_range: Mapping[str, Any]
    test_range: Mapping[str, Any]
    families: Mapping[str, LSTMResult]
    baselines: Mapping[str, StatisticalMetrics]
    pi06_comparison: Mapping[str, Any]
    selected_model: str | None
    selected_version: str | None
    preferred_overall: str
    selection_metric: str
    suitability: Mapping[str, Mapping[str, Any]]
    regime_samples: Mapping[str, int]
    insufficient_regimes: Sequence[str]
    failures: Sequence[str]
    notes: Sequence[str]
    resource: Mapping[str, Any]
    report_id: str
    test_used_for_selection: bool = False
    leakage_safe: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": PI07_PHASE,
            "contract_version": LSTM_CONTRACT_VERSION,
            "report_id": self.report_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "resolution": self.resolution.value,
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
            "pi06_comparison": {
                key: dict(value) if isinstance(value, Mapping) else value
                for key, value in self.pi06_comparison.items()
            },
            "selected_model": self.selected_model,
            "selected_version": self.selected_version,
            "selection_metric": self.selection_metric,
            "preferred_overall": self.preferred_overall,
            "suitability": {
                key: dict(value) for key, value in self.suitability.items()
            },
            "regime_samples": dict(self.regime_samples),
            "insufficient_regimes": list(self.insufficient_regimes),
            "failures": list(self.failures),
            "notes": list(self.notes),
            "resource": dict(self.resource),
            "test_used_for_selection": self.test_used_for_selection,
            "leakage_safe": self.leakage_safe,
            "experimental": True,
            "production_ready": False,
            "automatic_promotion": False,
        }


@dataclass(frozen=True, slots=True)
class LSTMLoaded:
    version: str
    config: LSTMConfig
    schema: FeatureSchema
    scaler: SequenceScaler
    network: Any
    interval: IntervalCalibration
    feature_stats: FeatureStats
    feature_importance: tuple[Mapping[str, Any], ...]
    artifact: LSTMArtifact


@dataclass(frozen=True, slots=True)
class LSTMForecast:
    contract: ForecastOutput
    result: LSTMResult | None
    schema: FeatureSchema
    scaler: SequenceScaler
    feature_values: Mapping[str, float | None]
    sequence_length: int
    missing_fraction: float
    interval: IntervalCalibration
    persisted: bool = False

    def to_dict(self) -> dict[str, Any]:
        body = self.contract.to_dict()
        body.update(
            {
                "phase": PI07_PHASE,
                "lstm_contract_version": LSTM_CONTRACT_VERSION,
                "sequence_model": self.result.to_dict() if self.result else None,
                "feature_schema": self.schema.to_dict(),
                "scaler": self.scaler.to_dict(),
                "feature_values": dict(self.feature_values),
                "sequence_length": self.sequence_length,
                "missing_feature_fraction": self.missing_fraction,
                "interval": self.interval.to_dict(),
                "probabilities_withheld": True,
                "automatic_promotion": False,
                "production_ready": False,
            }
        )
        return body


def lstm_model_version(
    *,
    instrument_id: str,
    horizon: ForecastHorizon,
    dataset_id: str,
    dataset_version: str,
    feature_set_version: str,
    target_steps: int,
    sequence_length: int,
    config_digest: str,
    train_start: datetime | None,
    train_end: datetime | None,
) -> str:
    token = _digest(
        {
            "instrument_id": instrument_id,
            "horizon": horizon.value,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "feature_set_version": feature_set_version,
            "target_steps": int(target_steps),
            "sequence_length": int(sequence_length),
            "config": config_digest,
            "train_start": train_start.isoformat() if train_start else "",
            "train_end": train_end.isoformat() if train_end else "",
            "methodology": LSTM_METHODOLOGY_VERSION,
        }
    )[:20]
    version = f"{LSTM_MODEL_VERSION_PREFIX}-{token}"
    if not all(character.isalnum() or character in ".-" for character in version):
        raise LSTMValidationError("derived LSTM artifact version is not path-safe")
    return version


__all__ = [
    "LSTM_CONTRACT_VERSION",
    "LSTM_FORECAST_PREFIX",
    "LSTM_MODEL_ID",
    "LSTM_MODEL_VERSION_PREFIX",
    "LSTM_METHODOLOGY_VERSION",
    "LSTM_PERFORMANCE_PREFIX",
    "LSTM_RUN_PREFIX",
    "LSTM_SCHEMA_VERSION",
    "PI07_PHASE",
    "LSTMArtifact",
    "LSTMConfig",
    "LSTMEvaluationReport",
    "LSTMError",
    "LSTMForecast",
    "LSTMLoaded",
    "LSTMResult",
    "LSTMTrainingError",
    "LSTMUnavailableError",
    "LSTMValidationError",
    "SequenceScaler",
    "lstm_model_version",
]
