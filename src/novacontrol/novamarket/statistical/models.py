"""PI-05 statistical forecasting contracts.

The statistical subsystem is deliberately narrower than the general prediction
stack: it fits transparent, deterministic time-series models to a bounded,
cutoff-safe price series and emits the PI-01 ``ForecastOutput`` contract.  No
value in this module is an order, a position, a risk decision or a promise of a
future price.

Model identity and configuration are separate.  A model id such as ``arima``
names the method; a :class:`ModelConfig` fixes its parameters.  The digest of
that configuration is persisted with every fit and forecast so a later run can
be reproduced without guessing which window, smoothing constant or ARIMA order
was used.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.marketdata.models import DataInterval
from novacontrol.novamarket.prediction.models import (
    Direction,
    ForecastHorizon,
    ForecastOutput,
    ModelFamily,
)

PI05_PHASE = "PI-05"
STATISTICAL_CONTRACT_VERSION = "pi05.statistical-forecast.1.0"
STATISTICAL_MODEL_VERSION = "pi05-stat-1.0"
STATISTICAL_METHODOLOGY_VERSION = "pi05-statistical-methodology-1.0"
STATISTICAL_SCHEMA_VERSION = 1

_INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")
_MODEL_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class StatisticalModelError(RuntimeError):
    """Base class for a statistical-model refusal."""


class ModelUnavailableError(StatisticalModelError, ValueError):
    """A model cannot be fitted or forecast from the supplied history."""


class ModelFitError(StatisticalModelError, ValueError):
    """The numerical fit failed (singular, unstable or invalid output)."""


class InsufficientHistoryError(ModelUnavailableError):
    """The model's stated minimum observation count is not available."""


class UnsupportedHorizonError(StatisticalModelError, ValueError):
    """The requested horizon cannot be supported by the supplied resolution."""


class StatisticalValidationError(StatisticalModelError, ValueError):
    """A request or metric input violates a PI-05 contract."""


class ModelKind(StrEnum):
    NAIVE = "naive"
    MOVING_AVERAGE = "moving_average"
    EXPONENTIAL_SMOOTHING = "exponential_smoothing"
    ARIMA = "arima"


class IntervalMethod(StrEnum):
    EMPIRICAL = "empirical_residual_quantiles"
    NORMAL_RESIDUAL = "normal_residual"
    NONE = "not_supported"


class FitStatus(StrEnum):
    FITTED = "fitted"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


#: Canonical model ids.  Aliases are accepted at the request boundary and
#: normalised to these names before a fit or a persisted identity is created.
MODEL_IDS: tuple[str, ...] = (
    "naive_last",
    "random_walk",
    "historical_return",
    "seasonal_naive",
    "sma",
    "wma",
    "ema",
    "ses",
    "holt",
    "holt_winters",
    "arima",
)

MODEL_ALIASES: dict[str, str] = {
    "last_value": "naive_last",
    "last": "naive_last",
    "naive": "naive_last",
    "naive_forecast": "naive_last",
    "random_walk_baseline": "random_walk",
    "historical_mean": "historical_return",
    "simple_moving_average": "sma",
    "moving_average": "sma",
    "weighted_moving_average": "wma",
    "exponential_moving_average": "ema",
    "simple_exponential_smoothing": "ses",
    "holt_linear": "holt",
    "holt_winters_additive": "holt_winters",
    "arima_p1_d1_q1": "arima",
}

MODEL_METADATA: dict[str, dict[str, Any]] = {
    "naive_last": {
        "kind": ModelKind.NAIVE,
        "family": ModelFamily.NAIVE,
        "description": "Reference price persistence: the last completed close.",
        "min_observations": 2,
        "interval": IntervalMethod.NONE,
    },
    "random_walk": {
        "kind": ModelKind.NAIVE,
        "family": ModelFamily.NAIVE,
        "description": (
            "Persistence with the empirical distribution of historical "
            "h-step returns; no directional edge is claimed."
        ),
        "min_observations": 30,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "historical_return": {
        "kind": ModelKind.NAIVE,
        "family": ModelFamily.NAIVE,
        "description": (
            "Historical h-step mean return with empirical residual quantiles."
        ),
        "min_observations": 40,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "seasonal_naive": {
        "kind": ModelKind.NAIVE,
        "family": ModelFamily.NAIVE,
        "description": (
            "Seasonal persistence: the price at the equivalent historical "
            "phase, with errors from earlier seasonal matches."
        ),
        "min_observations": 40,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "sma": {
        "kind": ModelKind.MOVING_AVERAGE,
        "family": ModelFamily.MOVING_AVERAGE,
        "description": (
            "Forecast the future price as the recent simple moving-average "
            "level; the window is explicit, not a hidden indicator default."
        ),
        "min_observations": 20,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "wma": {
        "kind": ModelKind.MOVING_AVERAGE,
        "family": ModelFamily.MOVING_AVERAGE,
        "description": (
            "Linearly weighted moving-average price level, giving newer "
            "observations more weight."
        ),
        "min_observations": 20,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "ema": {
        "kind": ModelKind.MOVING_AVERAGE,
        "family": ModelFamily.MOVING_AVERAGE,
        "description": (
            "Exponentially weighted moving-average price level with a "
            "configured span."
        ),
        "min_observations": 20,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "ses": {
        "kind": ModelKind.EXPONENTIAL_SMOOTHING,
        "family": ModelFamily.EXPONENTIAL_SMOOTHING,
        "description": (
            "Simple exponential smoothing of the price level, with no trend."
        ),
        "min_observations": 30,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "holt": {
        "kind": ModelKind.EXPONENTIAL_SMOOTHING,
        "family": ModelFamily.EXPONENTIAL_SMOOTHING,
        "description": "Holt's linear level-and-trend exponential smoothing.",
        "min_observations": 40,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "holt_winters": {
        "kind": ModelKind.EXPONENTIAL_SMOOTHING,
        "family": ModelFamily.EXPONENTIAL_SMOOTHING,
        "description": (
            "Additive Holt-Winters level, trend and seasonality; refused when "
            "the history cannot support a meaningful seasonal cycle."
        ),
        "min_observations": 60,
        "interval": IntervalMethod.EMPIRICAL,
    },
    "arima": {
        "kind": ModelKind.ARIMA,
        "family": ModelFamily.ARIMA,
        "description": (
            "Bounded ARIMA(p,d,q) fitted by conditional least squares, with "
            "stationarity testing and residual diagnostics."
        ),
        "min_observations": 60,
        "interval": IntervalMethod.NORMAL_RESIDUAL,
    },
}


def normalise_model_id(model_id: str) -> str:
    """Return a canonical model id or raise a named validation error."""
    cleaned = str(model_id or "").strip().lower()
    cleaned = MODEL_ALIASES.get(cleaned, cleaned)
    if cleaned not in MODEL_IDS:
        known = ", ".join(MODEL_IDS)
        raise StatisticalValidationError(
            f"unknown statistical model {model_id!r}; known: {known}"
        )
    return cleaned


def model_family(model_id: str) -> ModelFamily:
    value = MODEL_METADATA[normalise_model_id(model_id)]["family"]
    return value if isinstance(value, ModelFamily) else ModelFamily(value)


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """A reproducible model configuration.

    All defaults are valid for both daily and intraday bars.  ``seasonal_period``
    is measured in bars, not calendar days, so a five-session trading week is
    five bars while a 5-minute session may use the caller's explicit period.
    """

    window: int = 20
    alpha: float = 0.3
    beta: float = 0.1
    gamma: float = 0.3
    seasonal_period: int = 5
    seasonal: bool = False
    arima_order: tuple[int, int, int] = (1, 1, 1)
    auto_differencing: bool = False
    damped_trend: bool = False
    interval_level: float = 0.80
    neutral_band_pct: float = 0.5
    min_observations: int = 30
    max_arima_order: int = 5
    allow_low_seasonality: bool = False
    adjusted: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.window, bool) or not isinstance(self.window, int):
            raise StatisticalValidationError("window must be an integer")
        if not 2 <= self.window <= 500:
            raise StatisticalValidationError("window must be between 2 and 500")
        for name in ("alpha", "beta", "gamma"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StatisticalValidationError(f"{name} must be numeric")
            if not 0.0 < float(value) <= 1.0:
                raise StatisticalValidationError(f"{name} must be within (0, 1]")
        if not isinstance(self.seasonal, bool):
            raise StatisticalValidationError("seasonal must be a boolean")
        if isinstance(self.seasonal_period, bool) or not isinstance(
            self.seasonal_period, int
        ):
            raise StatisticalValidationError("seasonal_period must be an integer")
        if not 2 <= self.seasonal_period <= 366:
            raise StatisticalValidationError(
                "seasonal_period must be between 2 and 366 bars"
            )
        try:
            order = tuple(self.arima_order)
        except TypeError as exc:
            raise StatisticalValidationError(
                "arima_order must be a three-item (p, d, q) tuple"
            ) from exc
        if len(order) != 3:
            raise StatisticalValidationError(
                "arima_order must be a three-item (p, d, q) tuple"
            )
        p, d, q = order
        for name, value in (("p", p), ("d", d), ("q", q)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise StatisticalValidationError(f"arima {name} must be an integer")
            if not 0 <= value <= 3:
                raise StatisticalValidationError(f"arima {name} must be 0..3")
        if isinstance(self.max_arima_order, bool) or not isinstance(
            self.max_arima_order, int
        ):
            raise StatisticalValidationError("max_arima_order must be an integer")
        if not 1 <= self.max_arima_order <= 5:
            raise StatisticalValidationError("max_arima_order must be 1..5")
        if p + q > self.max_arima_order:
            raise StatisticalValidationError(
                f"arima p+q must not exceed {self.max_arima_order}"
            )
        try:
            interval_level = float(self.interval_level)
        except (TypeError, ValueError) as exc:
            raise StatisticalValidationError("interval_level must be numeric") from exc
        if not 0.5 <= interval_level < 1.0:
            raise StatisticalValidationError(
                "interval_level must be within [0.5, 1)"
            )
        try:
            neutral_band_pct = float(self.neutral_band_pct)
        except (TypeError, ValueError) as exc:
            raise StatisticalValidationError("neutral_band_pct must be numeric") from exc
        if not 0.0 < neutral_band_pct <= 10.0:
            raise StatisticalValidationError(
                "neutral_band_pct must be within (0, 10]"
            )
        if isinstance(self.min_observations, bool) or not isinstance(
            self.min_observations, int
        ):
            raise StatisticalValidationError("min_observations must be an integer")
        if not 10 <= self.min_observations <= 20_000:
            raise StatisticalValidationError(
                "min_observations must be between 10 and 20,000"
            )
        for name in (
            "auto_differencing",
            "damped_trend",
            "allow_low_seasonality",
            "adjusted",
        ):
            if not isinstance(getattr(self, name), bool):
                raise StatisticalValidationError(f"{name} must be a boolean")
        object.__setattr__(self, "arima_order", (p, d, q))
        object.__setattr__(self, "alpha", float(self.alpha))
        object.__setattr__(self, "beta", float(self.beta))
        object.__setattr__(self, "gamma", float(self.gamma))
        object.__setattr__(self, "interval_level", interval_level)
        object.__setattr__(self, "neutral_band_pct", neutral_band_pct)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> ModelConfig:
        """Validate a JSON/dict configuration without silently dropping keys."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise StatisticalValidationError("model config must be an object")
        body = dict(value)
        unknown = sorted(set(body) - set(cls.__dataclass_fields__))
        if unknown:
            raise StatisticalValidationError(
                "unknown model config key(s): " + ", ".join(unknown)
            )
        if "arima_order" in body:
            order = body["arima_order"]
            if isinstance(order, str) or not isinstance(order, Sequence):
                raise StatisticalValidationError(
                    "arima_order must be a three-item sequence"
                )
            body["arima_order"] = tuple(order)
        return cls(**body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "alpha": self.alpha,
            "beta": self.beta,
            "gamma": self.gamma,
            "seasonal_period": self.seasonal_period,
            "seasonal": self.seasonal,
            "arima_order": list(self.arima_order),
            "auto_differencing": self.auto_differencing,
            "damped_trend": self.damped_trend,
            "interval_level": self.interval_level,
            "neutral_band_pct": self.neutral_band_pct,
            "min_observations": self.min_observations,
            "max_arima_order": self.max_arima_order,
            "allow_low_seasonality": self.allow_low_seasonality,
            "adjusted": self.adjusted,
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def with_changes(self, **changes: Any) -> "ModelConfig":
        return replace(self, **changes)


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise StatisticalValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise StatisticalValidationError(
            f"{field_name} must be timezone-aware; a naive timestamp cannot be "
            "ordered against market observations"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class StatisticalForecastRequest:
    """Everything needed to reproduce one statistical forecast."""

    instrument_id: str
    horizon: ForecastHorizon
    as_of: datetime
    exchange: str = ""
    currency: str = ""
    resolution: DataInterval | str | None = None
    model_id: str = ""
    config: ModelConfig = field(default_factory=ModelConfig)
    dataset_id: str = ""
    dataset_version: str = ""
    feature_set_version: str = "unspecified"
    feature_set_id: str = ""
    bars: tuple[Any, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id or "").strip()
        if not _INSTRUMENT_ID.match(instrument_id):
            raise StatisticalValidationError(
                "instrument_id must be MIC:SYMBOL, e.g. XNSE:RELIANCE"
            )
        try:
            horizon = ForecastHorizon(self.horizon)
        except ValueError as exc:
            known = ", ".join(item.value for item in ForecastHorizon)
            raise StatisticalValidationError(
                f"unknown forecast horizon {self.horizon!r}; known: {known}"
            ) from exc
        resolution = None
        if self.resolution is not None:
            try:
                resolution = DataInterval(self.resolution)
            except ValueError as exc:
                raise StatisticalValidationError(
                    f"unknown bar resolution {self.resolution!r}"
                ) from exc
        model_id = ""
        if str(self.model_id or "").strip():
            model_id = normalise_model_id(str(self.model_id))
        if not isinstance(self.config, ModelConfig):
            raise StatisticalValidationError("config must be a ModelConfig")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "exchange", str(self.exchange or "").strip().upper())
        object.__setattr__(self, "currency", str(self.currency or "").strip().upper())
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "dataset_id", str(self.dataset_id or "").strip())
        object.__setattr__(
            self, "dataset_version", str(self.dataset_version or "").strip()
        )
        object.__setattr__(
            self,
            "feature_set_version",
            str(self.feature_set_version or "unspecified").strip() or "unspecified",
        )
        object.__setattr__(self, "feature_set_id", str(self.feature_set_id or "").strip())
        object.__setattr__(self, "bars", tuple(self.bars))
        object.__setattr__(self, "notes", str(self.notes or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "as_of": self.as_of.isoformat(),
            "exchange": self.exchange,
            "currency": self.currency,
            "resolution": (
                self.resolution.value
                if isinstance(self.resolution, DataInterval)
                else self.resolution
            ),
            "model_id": self.model_id,
            "config": self.config.to_dict(),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "feature_set_id": self.feature_set_id,
            "notes": self.notes,
            "contract_version": STATISTICAL_CONTRACT_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ModelFit:
    """A fitted model artifact description and its diagnostics."""

    model_id: str
    version: str
    family: ModelFamily
    config: ModelConfig
    status: FitStatus
    training_start: datetime
    training_end: datetime
    observations: int
    parameters: Mapping[str, Any] = field(default_factory=dict)
    residual_mean: float | None = None
    residual_std: float | None = None
    residual_autocorrelation: float | None = None
    ljung_box_p: float | None = None
    stationarity: Mapping[str, Any] = field(default_factory=dict)
    artifact_location: str = "inline://deterministic-parameters"
    dependency_version: str = "python-stdlib"
    reason: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", normalise_model_id(self.model_id))
        object.__setattr__(self, "family", ModelFamily(self.family))
        object.__setattr__(self, "status", FitStatus(self.status))
        object.__setattr__(self, "training_start", _aware(self.training_start, "training_start"))
        object.__setattr__(self, "training_end", _aware(self.training_end, "training_end"))
        if self.observations < 0:
            raise StatisticalValidationError("a fit observation count is non-negative")
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "stationarity", dict(self.stationarity))
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.version,
            "family": self.family.value,
            "config": self.config.to_dict(),
            "config_digest": self.config.digest,
            "status": self.status.value,
            "training_start": self.training_start.isoformat(),
            "training_end": self.training_end.isoformat(),
            "observations": self.observations,
            "parameters": dict(self.parameters),
            "residual_mean": self.residual_mean,
            "residual_std": self.residual_std,
            "residual_autocorrelation": self.residual_autocorrelation,
            "ljung_box_p": self.ljung_box_p,
            "stationarity": dict(self.stationarity),
            "artifact_location": self.artifact_location,
            "dependency_version": self.dependency_version,
            "reason": self.reason,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ModelForecast:
    """One model's fitted point forecast and statistical interval."""

    model_id: str
    family: ModelFamily
    config: ModelConfig
    fit: ModelFit
    predicted_price: float
    predicted_return: float
    lower_return: float | None
    upper_return: float | None
    lower_price: float | None
    upper_price: float | None
    interval_level: float
    interval_method: IntervalMethod
    interval_supported: bool
    direction: Direction
    direction_rule: str
    notes: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", normalise_model_id(self.model_id))
        object.__setattr__(self, "family", ModelFamily(self.family))
        object.__setattr__(self, "interval_method", IntervalMethod(self.interval_method))
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))
        object.__setattr__(self, "diagnostics", dict(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": self.family.value,
            "config": self.config.to_dict(),
            "fit": self.fit.to_dict(),
            "predicted_price": round(self.predicted_price, 8),
            "predicted_return": round(self.predicted_return, 8),
            "lower_return": (
                None if self.lower_return is None else round(self.lower_return, 8)
            ),
            "upper_return": (
                None if self.upper_return is None else round(self.upper_return, 8)
            ),
            "lower_price": (
                None if self.lower_price is None else round(self.lower_price, 8)
            ),
            "upper_price": (
                None if self.upper_price is None else round(self.upper_price, 8)
            ),
            "interval_level": self.interval_level,
            "interval_method": self.interval_method.value,
            "interval_supported": self.interval_supported,
            "predicted_direction": self.direction.value,
            "direction_rule": self.direction_rule,
            "notes": list(self.notes),
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class SuitabilityReport:
    """Whether a model may legitimately be used for this instrument/horizon."""

    instrument_id: str
    horizon: ForecastHorizon
    resolution: DataInterval | None
    observations: int
    eligible: bool
    reasons: tuple[str, ...] = ()
    requirements: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "resolution": (
                self.resolution.value
                if isinstance(self.resolution, DataInterval)
                else self.resolution
            ),
            "observations": self.observations,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "requirements": dict(self.requirements),
        }


@dataclass(frozen=True, slots=True)
class StatisticalForecast:
    """The PI-01 contract plus PI-05 fit provenance."""

    contract: ForecastOutput
    fit: ModelFit
    dataset_id: str
    dataset_version: str
    feature_set_version: str
    interval_level: float
    interval_method: IntervalMethod
    interval_supported: bool
    suitability: SuitabilityReport
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        body = self.contract.to_dict()
        body.update(
            {
                "statistical_contract_version": STATISTICAL_CONTRACT_VERSION,
                "statistical_model": self.fit.to_dict(),
                "dataset_id": self.dataset_id,
                "dataset_version": self.dataset_version,
                "feature_set_version": self.feature_set_version,
                "interval_level": self.interval_level,
                "interval_method": self.interval_method.value,
                "interval_supported": self.interval_supported,
                "uncertainty_detail": {
                    "interval_level": self.interval_level,
                    "interval_method": self.interval_method.value,
                    "supported": self.interval_supported,
                    "lower_price": str(self.contract.lower_price),
                    "upper_price": str(self.contract.upper_price),
                    "lower_return": self.contract.lower_return,
                    "upper_return": self.contract.upper_return,
                    "width_return": self.contract.uncertainty_width,
                    "residual_std": self.fit.residual_std,
                    "residual_samples": int(
                        self.fit.parameters.get("residual_samples", self.fit.observations)
                    ),
                    "model_uncertainty": (
                        "not separately quantified; parameter/refit uncertainty is "
                        "not propagated into this interval"
                    ),
                    "residual_uncertainty": self.fit.residual_std,
                    "data_quality_uncertainty": self.contract.data_quality.value,
                    "data_freshness": self.contract.data_freshness.value,
                    "coverage": (
                        "not evaluated for a single forecast; see the "
                        "walk-forward evaluation's interval_coverage"
                    ),
                    "limitations": (
                        "not a probability guarantee that the market price will "
                        "fall inside the interval",
                        "does not include event, liquidity or regime-shift risk",
                    ),
                },
                "suitability": self.suitability.to_dict(),
                "notes": list(dict.fromkeys((*self.contract.notes, *self.notes))),
            }
        )
        return body


def classify_direction(
    expected_return: float,
    lower_return: float,
    upper_return: float,
    *,
    neutral_band_pct: float,
    interval_supported: bool,
) -> tuple[Direction, str]:
    """Classify a forecast without turning noise or a large interval into a signal."""
    if not math.isfinite(expected_return):
        return Direction.INVALID, "non-finite expected return"
    try:
        band_pct = float(neutral_band_pct)
    except (TypeError, ValueError) as exc:
        raise StatisticalValidationError("neutral_band_pct must be numeric") from exc
    if not math.isfinite(band_pct) or band_pct <= 0:
        raise StatisticalValidationError("neutral_band_pct must be positive")
    band = abs(band_pct) / 100.0
    if abs(expected_return) <= band:
        return (
            Direction.NEUTRAL,
            f"|expected return| <= neutral band {band_pct:.4g}%",
        )
    if interval_supported and lower_return <= 0.0 <= upper_return:
        return (
            Direction.UNCERTAIN,
            "the predictive interval straddles zero, so direction is uncertain",
        )
    return (
        (Direction.UP if expected_return > 0 else Direction.DOWN),
        f"expected return lies outside the neutral band {band_pct:.4g}%",
    )


def price_from_return(last_price: float, expected_return: float) -> float:
    last = float(last_price)
    if not math.isfinite(last) or last <= 0:
        raise ModelFitError("the reference price must be finite and positive")
    price = last * (1.0 + float(expected_return))
    if not math.isfinite(price) or price <= 0:
        raise ModelFitError(
            "the forecast implies a non-positive or non-finite price; refusing it"
        )
    return price


def decimal_price(value: float) -> Decimal:
    """Convert a finite price to the Decimal representation the PI-01 contract requires."""
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ModelFitError("a forecast price must be finite and positive")
    return Decimal(str(round(float(value), 10)))


def config_candidates(
    model_id: str, base: ModelConfig | Mapping[str, Any] | None = None
) -> tuple[ModelConfig, ...]:
    """A bounded configuration grid; the caller selects with validation data only."""
    model = normalise_model_id(model_id)
    if base is None:
        resolved = ModelConfig()
    elif isinstance(base, ModelConfig):
        resolved = base
    elif isinstance(base, Mapping):
        resolved = ModelConfig.from_dict(base)
    else:
        raise StatisticalValidationError(
            "base model configuration must be a ModelConfig or an object"
        )
    base = resolved
    if model in {"naive_last", "random_walk", "historical_return", "seasonal_naive"}:
        return (base,)
    if model in {"sma", "wma", "ema"}:
        windows = tuple(dict.fromkeys((5, 10, 20, 50, base.window)))
        return tuple(base.with_changes(window=window) for window in windows)
    if model == "ses":
        alphas = tuple(dict.fromkeys((base.alpha, 0.2, 0.5, 0.8)))
        return tuple(base.with_changes(alpha=alpha) for alpha in alphas)
    if model == "holt":
        pairs = tuple(
            dict.fromkeys(
                (
                    (base.alpha, base.beta),
                    (0.2, 0.1),
                    (0.5, 0.2),
                    (0.8, 0.3),
                )
            )
        )
        return tuple(
            base.with_changes(alpha=alpha, beta=beta)
            for alpha, beta in pairs
        )
    if model == "holt_winters":
        periods = tuple(
            dict.fromkeys((base.seasonal_period, 5, 20))
        )
        return tuple(
            base.with_changes(seasonal=True, seasonal_period=period)
            for period in periods
        )
    if model == "arima":
        orders = tuple(
            dict.fromkeys(
                (
                    base.arima_order,
                    (0, 1, 0),
                    (1, 1, 0),
                    (0, 1, 1),
                    (1, 1, 1),
                    (2, 1, 1),
                    (1, 1, 2),
                )
            )
        )
        # A user-bounded search must skip orders outside the bound, not fail
        # the whole comparison on an otherwise valid candidate grid.
        orders = tuple(
            order for order in orders if order[0] + order[2] <= base.max_arima_order
        )
        return tuple(
            base.with_changes(
                arima_order=order,
                min_observations=max(base.min_observations, 40),
            )
            for order in orders
        )
    raise StatisticalValidationError(f"no candidate configuration for {model_id!r}")


__all__ = [
    "MODEL_ALIASES",
    "MODEL_IDS",
    "MODEL_METADATA",
    "PI05_PHASE",
    "STATISTICAL_CONTRACT_VERSION",
    "STATISTICAL_METHODOLOGY_VERSION",
    "STATISTICAL_MODEL_VERSION",
    "STATISTICAL_SCHEMA_VERSION",
    "FitStatus",
    "InsufficientHistoryError",
    "IntervalMethod",
    "ModelConfig",
    "ModelFamily",
    "ModelFit",
    "ModelFitError",
    "ModelForecast",
    "ModelKind",
    "ModelUnavailableError",
    "StatisticalForecast",
    "StatisticalForecastRequest",
    "StatisticalModelError",
    "StatisticalValidationError",
    "SuitabilityReport",
    "UnsupportedHorizonError",
    "classify_direction",
    "config_candidates",
    "decimal_price",
    "model_family",
    "normalise_model_id",
    "price_from_return",
]
