"""PI-01 prediction-intelligence contracts.

These are the foundational value types for the Prediction Intelligence
program.  They do not train a model, place an order, assess risk or move
NovaCoin.  A prediction is a time-stamped statement made from a specific data
snapshot; the lifecycle records every confirmation without rewriting an earlier
statement, and a decision-evidence view says only what the existing autonomous
pipeline may consume.

The one authority rule is encoded here as data, not convention:

* ``risk_authority`` is always NM-08;
* ``execution_authority`` is always NM-10;
* no prediction contract has an order, fill, position or ledger field.

Data timestamps are deliberately explicit.  ``source_observed_at`` is when the
provider says the observation existed; ``received_at`` is when NovaMarket
received it.  A cached observation never becomes fresh because it was read
again.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.contracts import NovaMarketError
from novacontrol.novamarket.marketdata.models import DataInterval, DataQuality, Freshness

PI_PHASE = "PI-01"
PI_CONTRACT_VERSION = "pi-01.1"
PI_SCHEMA_VERSION = 1
FORECAST_CONTRACT_VERSION = "prediction.forecast.1.0"
LIFECYCLE_CONTRACT_VERSION = "prediction.lifecycle.1.0"
ENSEMBLE_CONTRACT_VERSION = "prediction.ensemble.1.0"
DATA_CONTRACT_VERSION = "prediction.data.1.0"
LEARNING_CONTRACT_VERSION = "prediction.learning-boundary.1.0"
RISK_AUTHORITY = "NM-08"
EXECUTION_AUTHORITY = "NM-10"
PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
AUTOMATIC_PROMOTION = False

_MODEL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")
_PROBABILITY_TOLERANCE = 1e-6


class PredictionError(NovaMarketError):
    """Base class for a prediction-domain refusal."""


class PredictionValidationError(PredictionError, ValueError):
    """An input does not satisfy a PI-01 contract."""


class PredictionNotFoundError(PredictionError, LookupError):
    """A lifecycle, forecast, model or evaluation does not exist."""


class PredictionStateError(PredictionError):
    """A lifecycle stage is not allowed in the current state."""


class PredictionDataError(PredictionError, ValueError):
    """The market-data snapshot is missing, stale or incompatible."""


class PredictionAbstainedError(PredictionError):
    """No eligible ensemble could produce a forecast; certainty is not invented."""

    def __init__(self, message: str, *, reasons: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.reasons = tuple(str(item) for item in reasons)


class ForecastFamily(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    LONG_TERM = "long_term"


class ForecastHorizon(StrEnum):
    """The horizon labels the prediction program must distinguish."""

    MINUTES_5 = "5m"
    MINUTES_15 = "15m"
    MINUTES_20 = "20m"
    HOUR_1 = "1h"
    DAY_1 = "1d"
    DAYS_3 = "3d"
    DAYS_5 = "5d"
    DAYS_30 = "30d"
    DAYS_90 = "90d"


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"
    UNCERTAIN = "uncertain"
    INVALID = "invalid"


class ForecastValidity(StrEnum):
    VALID = "valid"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    STALE = "stale"
    INVALID = "invalid"
    EXPIRED = "expired"


class ProbabilityStatus(StrEnum):
    CALIBRATED = "calibrated"
    UNCALIBRATED = "uncalibrated"
    UNAVAILABLE = "unavailable"


class CalibrationStatus(StrEnum):
    CALIBRATED = "calibrated"
    UNCALIBRATED = "uncalibrated"
    NOT_APPLICABLE = "not_applicable"


class PredictionUncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class LifecycleStatus(StrEnum):
    AWAITING_FIRST_CONFIRMATION = "awaiting_first_confirmation"
    AWAITING_FINAL_CONFIRMATION = "awaiting_final_confirmation"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    EVALUATED = "evaluated"
    INVALIDATED = "invalidated"
    CANCELLED = "cancelled"


class ConfirmationStage(StrEnum):
    INITIAL = "t_minus_20"
    FIRST = "t_minus_5"
    FINAL = "t_minus_1"


class StageStatus(StrEnum):
    RECORDED = "recorded"
    SKIPPED = "skipped"
    INVALID = "invalid"


class ModelState(StrEnum):
    ELIGIBLE = "eligible"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    UNAVAILABLE = "unavailable"
    RETIRED = "retired"


class ModelFamily(StrEnum):
    NAIVE = "naive"
    MOVING_AVERAGE = "moving_average"
    EXPONENTIAL_SMOOTHING = "exponential_smoothing"
    ARIMA = "arima"
    XGBOOST = "xgboost"
    LIGHTGBM = "lightgbm"
    LSTM = "lstm"
    ENSEMBLE = "ensemble"
    EXTERNAL = "external"


class DataPath(StrEnum):
    GENERAL_REFRESH = "general_refresh"
    INTRADAY_QUOTE = "intraday_quote"
    HISTORICAL = "historical"
    NEWS_AND_EVENTS = "news_and_events"
    FUNDAMENTALS = "fundamentals"


class RequestPriority(StrEnum):
    ACTIVE_POSITION = "active_position"
    FORECAST_CANDIDATE = "forecast_candidate"
    WATCHLIST = "watchlist"
    DISCOVERY = "discovery"


#: Lower is faster/finer.  SPOT is a point observation; a daily bar is the
#: coarsest usable input.  A five-minute forecast may not be made from daily data.
_INTERVAL_RANK: dict[DataInterval, int] = {
    DataInterval.SPOT: 0,
    DataInterval.MINUTE_1: 1,
    DataInterval.MINUTE_5: 2,
    DataInterval.MINUTE_15: 3,
    DataInterval.MINUTE_30: 4,
    DataInterval.HOUR_1: 5,
    DataInterval.DAY_1: 6,
    DataInterval.WEEK_1: 7,
    DataInterval.MONTH_1: 8,
}


@dataclass(frozen=True, slots=True)
class HorizonSpec:
    horizon: ForecastHorizon
    family: ForecastFamily
    duration: timedelta
    max_input_interval: DataInterval
    min_history_bars: int
    neutral_band_pct: float
    objective: str

    def accepts_resolution(self, resolution: DataInterval | str) -> bool:
        """Whether this resolution is fine enough for the horizon.

        A spot quote is finer than every bar interval.  Conversely, a daily
        bar can never support a five-minute forecast, regardless of how recently
        the daily file was downloaded.
        """
        try:
            value = DataInterval(resolution)
        except ValueError:
            return False
        return _INTERVAL_RANK[value] <= _INTERVAL_RANK[self.max_input_interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "family": self.family.value,
            "duration_seconds": int(self.duration.total_seconds()),
            "max_input_interval": self.max_input_interval.value,
            "min_history_bars": self.min_history_bars,
            "neutral_band_pct": self.neutral_band_pct,
            "objective": self.objective,
        }


#: The three forecasting profiles mandated by PI-01.  The durations are the
#: contract targets; an exchange calendar may move an actual target across a
#: holiday, which is why the persisted target timestamp remains authoritative.
HORIZON_SPECS: dict[ForecastHorizon, HorizonSpec] = {
    ForecastHorizon.MINUTES_5: HorizonSpec(
        ForecastHorizon.MINUTES_5,
        ForecastFamily.INTRADAY,
        timedelta(minutes=5),
        DataInterval.MINUTE_1,
        60,
        0.05,
        "Short-horizon direction and entry/exit timing.",
    ),
    ForecastHorizon.MINUTES_15: HorizonSpec(
        ForecastHorizon.MINUTES_15,
        ForecastFamily.INTRADAY,
        timedelta(minutes=15),
        DataInterval.MINUTE_5,
        48,
        0.08,
        "Intraday momentum and regime change.",
    ),
    ForecastHorizon.MINUTES_20: HorizonSpec(
        ForecastHorizon.MINUTES_20,
        ForecastFamily.INTRADAY,
        timedelta(minutes=20),
        DataInterval.MINUTE_5,
        48,
        0.10,
        "Short-term opportunity assessment.",
    ),
    ForecastHorizon.HOUR_1: HorizonSpec(
        ForecastHorizon.HOUR_1,
        ForecastFamily.INTRADAY,
        timedelta(hours=1),
        DataInterval.MINUTE_15,
        40,
        0.15,
        "Intraday continuation or reversal.",
    ),
    ForecastHorizon.DAY_1: HorizonSpec(
        ForecastHorizon.DAY_1,
        ForecastFamily.SWING,
        timedelta(days=1),
        DataInterval.DAY_1,
        80,
        0.50,
        "Next-session direction and expected return.",
    ),
    ForecastHorizon.DAYS_3: HorizonSpec(
        ForecastHorizon.DAYS_3,
        ForecastFamily.SWING,
        timedelta(days=3),
        DataInterval.DAY_1,
        80,
        1.00,
        "Multi-day swing opportunity.",
    ),
    ForecastHorizon.DAYS_5: HorizonSpec(
        ForecastHorizon.DAYS_5,
        ForecastFamily.SWING,
        timedelta(days=5),
        DataInterval.DAY_1,
        100,
        1.25,
        "One-week trend and event risk.",
    ),
    ForecastHorizon.DAYS_30: HorizonSpec(
        ForecastHorizon.DAYS_30,
        ForecastFamily.LONG_TERM,
        timedelta(days=30),
        DataInterval.DAY_1,
        120,
        2.00,
        "One-month fundamental and sector view.",
    ),
    ForecastHorizon.DAYS_90: HorizonSpec(
        ForecastHorizon.DAYS_90,
        ForecastFamily.LONG_TERM,
        timedelta(days=90),
        DataInterval.DAY_1,
        180,
        3.00,
        "One-quarter fundamental view.",
    ),
}


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """A named, versioned feature whose meaning is fixed before values exist."""

    name: str
    version: str
    dtype: str = "float"
    description: str = ""
    source: str = ""
    lookback: str = ""

    def __post_init__(self) -> None:
        name = _clean(self.name)
        version = _clean(self.version)
        dtype = _clean(self.dtype).lower()
        if not name or not version:
            raise PredictionValidationError("feature definition name and version are required")
        if dtype not in ("float", "int", "bool", "category", "decimal"):
            raise PredictionValidationError(f"unsupported feature dtype {self.dtype!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "dtype", dtype)
        object.__setattr__(self, "description", _clean(self.description))
        object.__setattr__(self, "source", _clean(self.source))
        object.__setattr__(self, "lookback", _clean(self.lookback))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "dtype": self.dtype,
            "description": self.description,
            "source": self.source,
            "lookback": self.lookback,
            "contract_version": "prediction.feature-definition.1.0",
        }


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """A versioned feature row with explicit data lineage.

    PI-01 defines the contract; feature engineering and storage belong to PI-02.
    A feature value is never assumed to be fresh because its containing row was
    read recently; ``as_of`` is the observation cutoff.
    """

    feature_set_id: str
    version: str
    instrument_id: str
    as_of: datetime
    values: Mapping[str, float]
    data_snapshot_ids: tuple[str, ...]
    lineage: Mapping[str, str] = field(default_factory=dict)
    digest_value: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.feature_set_id) or not _clean(self.version):
            raise PredictionValidationError("feature set id and version are required")
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("feature vector instrument_id is not canonical")
        if not self.values:
            raise PredictionValidationError("a feature vector must contain values")
        cleaned: dict[str, float] = {}
        for name, value in self.values.items():
            if not str(name).strip():
                raise PredictionValidationError("feature names may not be blank")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise PredictionValidationError(
                    f"feature {name!r} is not numeric"
                ) from exc
            if not math.isfinite(number):
                raise PredictionValidationError(f"feature {name!r} is not finite")
            cleaned[str(name)] = number
        object.__setattr__(self, "feature_set_id", _clean(self.feature_set_id))
        object.__setattr__(self, "version", _clean(self.version))
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "values", cleaned)
        object.__setattr__(self, "data_snapshot_ids", tuple(str(item) for item in self.data_snapshot_ids))
        object.__setattr__(self, "lineage", dict(self.lineage))
        object.__setattr__(self, "digest_value", _clean(self.digest_value) or digest({
            "feature_set_id": self.feature_set_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "values": cleaned,
            "data_snapshot_ids": list(self.data_snapshot_ids),
            "lineage": dict(self.lineage),
        }))

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_set_id": self.feature_set_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "values": dict(self.values),
            "data_snapshot_ids": list(self.data_snapshot_ids),
            "lineage": dict(self.lineage),
            "digest": self.digest_value,
            "contract_version": "prediction.features.1.0",
        }


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    """A company-fundamental observation and its filing/source timestamps."""

    snapshot_id: str
    instrument_id: str
    fiscal_period: str
    source_observed_at: datetime
    received_at: datetime
    currency: str
    metrics: Mapping[str, str]
    source: str
    quality: DataQuality = DataQuality.VALID
    freshness: Freshness = Freshness.UNKNOWN
    source_url: str = ""
    reasons: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("fundamental instrument_id is not canonical")
        if not _clean(self.fiscal_period) or not _clean(self.source):
            raise PredictionValidationError("fundamental fiscal period and source are required")
        if not self.metrics:
            raise PredictionValidationError("a fundamental snapshot needs at least one metric")
        for name, value in self.metrics.items():
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise PredictionValidationError(
                    f"fundamental metric {name!r} is not decimal"
                ) from exc
            if not number.is_finite():
                raise PredictionValidationError(
                    f"fundamental metric {name!r} is not finite"
                )
        observed = _aware(self.source_observed_at, "source_observed_at")
        received = _aware(self.received_at, "received_at")
        if observed > received + timedelta(seconds=300):
            raise PredictionValidationError("fundamental timestamp is in the future")
        object.__setattr__(self, "source_observed_at", observed)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "currency", _clean(self.currency).upper())
        object.__setattr__(self, "metrics", {str(k): str(v) for k, v in self.metrics.items()})
        object.__setattr__(self, "quality", DataQuality(self.quality))
        object.__setattr__(self, "freshness", Freshness(self.freshness))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "provenance", dict(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "instrument_id": self.instrument_id,
            "fiscal_period": self.fiscal_period,
            "source_observed_at": self.source_observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "currency": self.currency,
            "metrics": dict(self.metrics),
            "source": self.source,
            "source_url": self.source_url,
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "reasons": list(self.reasons),
            "provenance": dict(self.provenance),
            "contract_version": "prediction.fundamentals.1.0",
            "is_company_fundamental": True,
        }


@dataclass(frozen=True, slots=True)
class NewsEventEvidence:
    """Timestamped news, announcement or corporate-event evidence."""

    evidence_id: str
    instrument_ids: tuple[str, ...]
    event_type: str
    headline: str
    source: str
    published_at: datetime
    received_at: datetime
    quality: DataQuality = DataQuality.VALID
    freshness: Freshness = Freshness.UNKNOWN
    source_url: str = ""
    summary: str = ""
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument_ids:
            raise PredictionValidationError("news evidence must name an instrument")
        for item in self.instrument_ids:
            if not _INSTRUMENT_ID.match(str(item)):
                raise PredictionValidationError(
                    f"news evidence instrument {item!r} is not canonical"
                )
        if not _clean(self.event_type) or not _clean(self.headline) or not _clean(self.source):
            raise PredictionValidationError(
                "news evidence needs event type, headline and source"
            )
        published = _aware(self.published_at, "published_at")
        received = _aware(self.received_at, "received_at")
        if published > received + timedelta(seconds=300):
            raise PredictionValidationError("news published_at is in the future")
        object.__setattr__(self, "instrument_ids", tuple(str(item) for item in self.instrument_ids))
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "quality", DataQuality(self.quality))
        object.__setattr__(self, "freshness", Freshness(self.freshness))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "instrument_ids": list(self.instrument_ids),
            "event_type": self.event_type,
            "headline": self.headline,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "source_url": self.source_url,
            "summary": self.summary,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
            "contract_version": "prediction.news-events.1.0",
            "is_news_or_event_evidence": True,
        }


@dataclass(frozen=True, slots=True)
class HistoricalMarketDataset:
    """A versioned historical series reference, not a copied provider dump."""

    dataset_id: str
    version: str
    instrument_id: str
    resolution: DataInterval
    start_at: datetime
    end_at: datetime
    bar_count: int
    source_snapshot_ids: tuple[str, ...]
    quality: DataQuality = DataQuality.VALID
    provenance: Mapping[str, Any] = field(default_factory=dict)
    digest_value: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.dataset_id) or not _clean(self.version):
            raise PredictionValidationError("dataset id and version are required")
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("dataset instrument_id is not canonical")
        start = _aware(self.start_at, "start_at")
        end = _aware(self.end_at, "end_at")
        if end < start:
            raise PredictionValidationError("dataset end precedes its start")
        if isinstance(self.bar_count, bool):
            raise PredictionValidationError("dataset bar_count must be an integer")
        try:
            count = int(self.bar_count)
        except (TypeError, ValueError) as exc:
            raise PredictionValidationError("dataset bar_count must be an integer") from exc
        if count < 0:
            raise PredictionValidationError("dataset bar_count must be non-negative")
        object.__setattr__(self, "resolution", DataInterval(self.resolution))
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        object.__setattr__(self, "bar_count", count)
        object.__setattr__(self, "source_snapshot_ids", tuple(str(item) for item in self.source_snapshot_ids))
        object.__setattr__(self, "quality", DataQuality(self.quality))
        object.__setattr__(self, "provenance", dict(self.provenance))
        object.__setattr__(self, "digest_value", _clean(self.digest_value) or digest({
            "dataset_id": self.dataset_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "resolution": self.resolution.value,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "bar_count": self.bar_count,
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "quality": self.quality.value,
        }))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "resolution": self.resolution.value,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "bar_count": self.bar_count,
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "quality": self.quality.value,
            "provenance": dict(self.provenance),
            "digest": self.digest_value,
            "contract_version": "prediction.dataset.1.0",
        }


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Bounded CPU/memory/inference/background rules for prediction work."""

    model_load_strategy: str = "lazy"
    max_loaded_models: int = 2
    inference_timeout_seconds: int = 10
    max_cpu_seconds: int = 30
    max_memory_mb: int = 512
    cache_owner: str = "prediction_intelligence"
    failure_isolation: bool = True
    background_scheduler: bool = False
    distributed_workers: int = 0

    def __post_init__(self) -> None:
        if self.model_load_strategy not in ("lazy", "eager"):
            raise PredictionValidationError("model_load_strategy must be lazy or eager")
        if not 1 <= int(self.max_loaded_models) <= 16:
            raise PredictionValidationError("max_loaded_models must be 1..16")
        if not 1 <= int(self.inference_timeout_seconds) <= 300:
            raise PredictionValidationError("inference_timeout_seconds must be 1..300")
        if not 1 <= int(self.max_cpu_seconds) <= 600:
            raise PredictionValidationError("max_cpu_seconds must be 1..600")
        if not 16 <= int(self.max_memory_mb) <= 16_384:
            raise PredictionValidationError("max_memory_mb must be 16..16384")
        if not self.failure_isolation:
            raise PredictionValidationError(
                "prediction model/resource failures must remain isolated"
            )
        if self.background_scheduler:
            raise PredictionValidationError(
                "PI-01 must not introduce a background prediction scheduler"
            )
        if self.distributed_workers != 0:
            raise PredictionValidationError(
                "PI-01 must not introduce distributed prediction workers"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_load_strategy": self.model_load_strategy,
            "max_loaded_models": self.max_loaded_models,
            "inference_timeout_seconds": self.inference_timeout_seconds,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_memory_mb": self.max_memory_mb,
            "cache_owner": self.cache_owner,
            "failure_isolation": self.failure_isolation,
            "background_scheduler": self.background_scheduler,
            "distributed_workers": self.distributed_workers,
            "reuses": ["Python process", "FastAPI", "SQLite", "existing event bus"],
        }


def horizon_spec(horizon: ForecastHorizon | str) -> HorizonSpec:
    try:
        return HORIZON_SPECS[ForecastHorizon(horizon)]
    except (ValueError, KeyError) as exc:
        known = ", ".join(item.value for item in ForecastHorizon)
        raise PredictionValidationError(
            f"unknown forecast horizon {horizon!r}; known: {known}"
        ) from exc


@dataclass(frozen=True, slots=True)
class PredictionPolicy:
    """Explicit timing, freshness, ensemble and safety policy."""

    initial_lead_minutes: int = 20
    first_confirmation_lead_minutes: int = 5
    final_confirmation_lead_minutes: int = 1
    stage_tolerance_seconds: int = 90
    max_models: int = 10
    min_models: int = 1
    max_disagreement: float = 0.03
    abstain_disagreement: float = 0.08
    min_direction_probability: float = 0.40
    uncertainty_low_pct: float = 2.0
    uncertainty_high_pct: float = 8.0
    default_model_weight: float = 1.0
    degraded_model_weight: float = 0.5
    automatic_promotion_allowed: bool = False
    allow_stale_reuse: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("initial_lead_minutes", self.initial_lead_minutes),
            ("first_confirmation_lead_minutes", self.first_confirmation_lead_minutes),
            ("final_confirmation_lead_minutes", self.final_confirmation_lead_minutes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise PredictionValidationError(f"{name} must be an integer")
            if not 1 <= value <= 1_440:
                raise PredictionValidationError(f"{name} must be between 1 and 1440")
        if not (
            self.initial_lead_minutes
            > self.first_confirmation_lead_minutes
            > self.final_confirmation_lead_minutes
        ):
            raise PredictionValidationError(
                "lifecycle leads must decrease: initial > first > final"
            )
        if not 0 <= self.stage_tolerance_seconds <= 600:
            raise PredictionValidationError(
                "stage_tolerance_seconds must be between 0 and 600"
            )
        if isinstance(self.max_models, bool) or not 1 <= self.max_models <= 50:
            raise PredictionValidationError("max_models must be between 1 and 50")
        if isinstance(self.min_models, bool) or not 1 <= self.min_models <= self.max_models:
            raise PredictionValidationError("min_models must be between 1 and max_models")
        for name, numeric_value in (
            ("max_disagreement", self.max_disagreement),
            ("abstain_disagreement", self.abstain_disagreement),
            ("min_direction_probability", self.min_direction_probability),
        ):
            if isinstance(numeric_value, bool) or not isinstance(numeric_value, (int, float)):
                raise PredictionValidationError(f"{name} must be numeric")
            if not 0.0 <= float(numeric_value) <= 1.0:
                raise PredictionValidationError(f"{name} must be within [0, 1]")
        for name, percentage in (
            ("uncertainty_low_pct", self.uncertainty_low_pct),
            ("uncertainty_high_pct", self.uncertainty_high_pct),
        ):
            if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
                raise PredictionValidationError(f"{name} must be numeric")
            if not 0.0 < float(percentage) <= 100.0:
                raise PredictionValidationError(f"{name} must be within (0, 100]")
        if self.max_disagreement >= self.abstain_disagreement:
            raise PredictionValidationError(
                "max_disagreement must be below abstain_disagreement"
            )
        if self.uncertainty_low_pct >= self.uncertainty_high_pct:
            raise PredictionValidationError(
                "uncertainty_low_pct must be below uncertainty_high_pct"
            )
        for name in ("default_model_weight", "degraded_model_weight"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PredictionValidationError(f"{name} must be numeric")
            if not 0.0 < float(value) <= 10.0:
                raise PredictionValidationError(f"{name} must be within (0, 10]")
        if self.automatic_promotion_allowed:
            raise PredictionValidationError(
                "automatic model promotion is forbidden; promotion requires an "
                "explicit governed action"
            )
        if self.allow_stale_reuse:
            raise PredictionValidationError(
                "stale observations may not be reused as current predictions"
            )

    def max_age_seconds(self, family: ForecastFamily | str, stage: ConfirmationStage | str) -> int:
        """The freshness window for one family and lifecycle stage."""
        try:
            chosen_family = ForecastFamily(family)
            chosen_stage = ConfirmationStage(stage)
        except ValueError as exc:
            raise PredictionValidationError(f"unknown freshness key: {exc}") from exc
        windows = {
            ForecastFamily.INTRADAY: {
                ConfirmationStage.INITIAL: 600,
                ConfirmationStage.FIRST: 300,
                ConfirmationStage.FINAL: 120,
            },
            ForecastFamily.SWING: {
                ConfirmationStage.INITIAL: 5 * 86_400,
                ConfirmationStage.FIRST: 3 * 86_400,
                ConfirmationStage.FINAL: 2 * 86_400,
            },
            ForecastFamily.LONG_TERM: {
                ConfirmationStage.INITIAL: 14 * 86_400,
                ConfirmationStage.FIRST: 10 * 86_400,
                ConfirmationStage.FINAL: 7 * 86_400,
            },
        }
        return windows[chosen_family][chosen_stage]

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_lead_minutes": self.initial_lead_minutes,
            "first_confirmation_lead_minutes": self.first_confirmation_lead_minutes,
            "final_confirmation_lead_minutes": self.final_confirmation_lead_minutes,
            "stage_tolerance_seconds": self.stage_tolerance_seconds,
            "max_models": self.max_models,
            "min_models": self.min_models,
            "max_disagreement": self.max_disagreement,
            "abstain_disagreement": self.abstain_disagreement,
            "min_direction_probability": self.min_direction_probability,
            "uncertainty_low_pct": self.uncertainty_low_pct,
            "uncertainty_high_pct": self.uncertainty_high_pct,
            "default_model_weight": self.default_model_weight,
            "degraded_model_weight": self.degraded_model_weight,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "allow_stale_reuse": self.allow_stale_reuse,
        }


@dataclass(frozen=True, slots=True)
class AcquisitionPolicy:
    """The explicit low-latency path, separate from the 15-minute refresh."""

    general_refresh_floor_seconds: int = 900
    intraday_min_interval_seconds: int = 30
    intraday_max_requests_per_minute: int = 20
    discovery_batch_cap: int = 20
    max_concurrent_fetches: int = 2
    active_position_reserved_slots: int = 8
    forecast_candidate_reserved_slots: int = 8
    watchlist_reserved_slots: int = 4
    request_timeout_seconds: int = 10
    max_retries: int = 2
    failover_enabled: bool = True
    fair_rotation_preserved: bool = True

    def __post_init__(self) -> None:
        if self.general_refresh_floor_seconds != 900:
            raise PredictionValidationError(
                "the general market-data refresh floor is 900 seconds and may "
                "not be weakened by the prediction path"
            )
        if not 30 <= self.intraday_min_interval_seconds <= 3600:
            raise PredictionValidationError(
                "intraday_min_interval_seconds may not go below the 30-second floor"
            )
        if not 1 <= self.intraday_max_requests_per_minute <= 20:
            raise PredictionValidationError(
                "intraday_max_requests_per_minute may not exceed the existing 20/minute limit"
            )
        if not 1 <= self.discovery_batch_cap <= 20:
            raise PredictionValidationError(
                "the discovery batch cap may not exceed 20 instruments"
            )
        if not 1 <= self.max_concurrent_fetches <= 2:
            raise PredictionValidationError(
                "max_concurrent_fetches may not exceed the existing limit of two"
            )
        if not 0 <= self.max_retries <= 5:
            raise PredictionValidationError("max_retries must be 0..5")
        if not self.fair_rotation_preserved:
            raise PredictionValidationError(
                "the prediction path may not disable provider fair rotation"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "general_refresh_floor_seconds": self.general_refresh_floor_seconds,
            "intraday_min_interval_seconds": self.intraday_min_interval_seconds,
            "intraday_max_requests_per_minute": self.intraday_max_requests_per_minute,
            "discovery_batch_cap": self.discovery_batch_cap,
            "max_concurrent_fetches": self.max_concurrent_fetches,
            "active_position_reserved_slots": self.active_position_reserved_slots,
            "forecast_candidate_reserved_slots": self.forecast_candidate_reserved_slots,
            "watchlist_reserved_slots": self.watchlist_reserved_slots,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "failover_enabled": self.failover_enabled,
            "fair_rotation_preserved": self.fair_rotation_preserved,
            "paths": [item.value for item in DataPath],
            "priorities": [item.value for item in RequestPriority],
        }


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise PredictionValidationError(f"{name} must be a decimal, not bool")
    if isinstance(value, float):
        raise PredictionValidationError(
            f"{name} must be Decimal or a decimal string, not a binary float"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PredictionValidationError(f"{name} is not a decimal: {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise PredictionValidationError(f"{name} must be finite and positive")
    return result


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PredictionValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise PredictionValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PredictionValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _probability(value: object | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PredictionValidationError(f"{name} must be numeric, not bool")
    if not isinstance(value, (int, float, str)):
        raise PredictionValidationError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PredictionValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise PredictionValidationError(f"{name} must be within [0, 1]")
    return number


def _clean(value: str) -> str:
    return str(value or "").strip()


def prediction_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PredictionRequest:
    request_id: str
    instrument_id: str
    exchange: str
    horizon: ForecastHorizon
    target_at: datetime
    created_at: datetime
    portfolio_id: str = ""
    strategy_id: str = ""
    actor: str = ""
    notes: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError(
                "instrument_id must be MIC:SYMBOL, e.g. XNSE:RELIANCE"
            )
        if not _clean(self.exchange):
            raise PredictionValidationError("exchange is required")
        try:
            horizon = ForecastHorizon(self.horizon)
        except ValueError as exc:
            raise PredictionValidationError(f"unknown horizon {self.horizon!r}") from exc
        target = _aware(self.target_at, "target_at")
        created = _aware(self.created_at, "created_at")
        if target <= created:
            raise PredictionValidationError("target_at must be after created_at")
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "target_at", target)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "request_id", _clean(self.request_id) or prediction_id(
            "prq_", self.instrument_id, horizon.value, target.isoformat(), created.isoformat()
        ))
        object.__setattr__(self, "exchange", _clean(self.exchange).upper())
        object.__setattr__(self, "portfolio_id", _clean(self.portfolio_id))
        object.__setattr__(self, "strategy_id", _clean(self.strategy_id))
        object.__setattr__(self, "actor", _clean(self.actor))
        object.__setattr__(self, "notes", _clean(self.notes))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon.value,
            "target_at": self.target_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "portfolio_id": self.portfolio_id,
            "strategy_id": self.strategy_id,
            "actor": self.actor,
            "notes": self.notes,
            "metadata": dict(self.metadata),
            "is_prediction_request": True,
        }


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    """The exact market observation and data-quality evidence used by a forecast."""

    snapshot_id: str
    instrument_id: str
    exchange: str
    provider: str
    source_observed_at: datetime
    received_at: datetime
    resolution: DataInterval
    reference_price: Decimal
    currency: str
    quality: DataQuality
    freshness: Freshness
    usable: bool
    provider_conflict: bool
    missing_observation: bool
    rate_limited: bool
    provider_health: str
    history_bars: int
    history_sufficient: bool
    reasons: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("snapshot instrument_id is not canonical")
        price = _decimal(self.reference_price, "reference_price")
        observed = _aware(self.source_observed_at, "source_observed_at")
        received = _aware(self.received_at, "received_at")
        if observed > received + timedelta(seconds=300):
            raise PredictionValidationError(
                "source_observed_at is in the future beyond clock skew"
            )
        try:
            resolution = DataInterval(self.resolution)
            quality = DataQuality(self.quality)
            freshness = Freshness(self.freshness)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid snapshot enum: {exc}") from exc
        object.__setattr__(self, "snapshot_id", _clean(self.snapshot_id) or prediction_id(
            "pds_", self.instrument_id, self.provider, resolution.value, observed.isoformat()
        ))
        object.__setattr__(self, "exchange", _clean(self.exchange).upper())
        object.__setattr__(self, "reference_price", price)
        object.__setattr__(self, "source_observed_at", observed)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "freshness", freshness)
        object.__setattr__(self, "provider", _clean(self.provider).lower())
        object.__setattr__(self, "currency", _clean(self.currency).upper())
        object.__setattr__(self, "provider_health", _clean(self.provider_health))
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(str(item) for item in self.reasons)))
        object.__setattr__(self, "provenance", dict(self.provenance))
        object.__setattr__(self, "evidence", tuple(dict(item) for item in self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "provider": self.provider,
            "source_observed_at": self.source_observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "resolution": self.resolution.value,
            "reference_price": format(self.reference_price, "f"),
            "currency": self.currency,
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "usable": self.usable,
            "provider_conflict": self.provider_conflict,
            "missing_observation": self.missing_observation,
            "rate_limited": self.rate_limited,
            "provider_health": self.provider_health,
            "history_bars": self.history_bars,
            "history_sufficient": self.history_sufficient,
            "reasons": list(self.reasons),
            "provenance": dict(self.provenance),
            "evidence": [dict(item) for item in self.evidence],
            "contract_version": DATA_CONTRACT_VERSION,
            "is_market_observation": True,
            "is_prediction": False,
        }


@dataclass(frozen=True, slots=True)
class PredictionEvidenceBundle:
    """The evidence set a model may cite for one forecast.

    The bundle is a reference container, not a second data-acquisition path.
    Every item carries its own source and receipt timestamps; the market
    snapshot is mandatory because no forecast may exist without a market
    reference price.
    """

    bundle_id: str
    instrument_id: str
    market_snapshot: MarketDataSnapshot
    feature_vector: FeatureVector | None = None
    fundamentals: tuple[FundamentalSnapshot, ...] = ()
    news_events: tuple[NewsEventEvidence, ...] = ()
    datasets: tuple[HistoricalMarketDataset, ...] = ()
    assembled_at: datetime | None = None

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("evidence bundle instrument_id is not canonical")
        if self.market_snapshot.instrument_id != self.instrument_id:
            raise PredictionValidationError("evidence bundle contains another instrument")
        if (
            self.feature_vector is not None
            and self.feature_vector.instrument_id != self.instrument_id
        ):
            raise PredictionValidationError("feature evidence belongs to another instrument")
        for dataset in self.datasets:
            if dataset.instrument_id != self.instrument_id:
                raise PredictionValidationError("dataset evidence belongs to another instrument")
        for fundamental in self.fundamentals:
            if fundamental.instrument_id != self.instrument_id:
                raise PredictionValidationError("fundamental evidence belongs to another instrument")
        for news_event in self.news_events:
            if self.instrument_id not in news_event.instrument_ids:
                raise PredictionValidationError("news evidence belongs to another instrument")
        object.__setattr__(self, "assembled_at", _aware(self.assembled_at, "assembled_at") if self.assembled_at else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "instrument_id": self.instrument_id,
            "market_snapshot": self.market_snapshot.to_dict(),
            "feature_vector": self.feature_vector.to_dict() if self.feature_vector else None,
            "fundamentals": [item.to_dict() for item in self.fundamentals],
            "news_events": [item.to_dict() for item in self.news_events],
            "datasets": [item.to_dict() for item in self.datasets],
            "assembled_at": self.assembled_at.isoformat() if self.assembled_at else None,
            "contract_version": "prediction.evidence-bundle.1.0",
            "reads_existing_authorities": True,
            "second_data_acquisition_authority": False,
        }


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    model_id: str
    version: str
    family: ModelFamily
    state: ModelState
    supported_horizons: tuple[ForecastHorizon, ...]
    calibration: CalibrationStatus
    weight: float = 1.0
    metrics: Mapping[str, Any] = field(default_factory=dict)
    trained_at: datetime | None = None
    updated_at: datetime | None = None
    notes: str = ""
    feature_set_version: str = "unspecified"
    dataset_version: str = "unspecified"

    def __post_init__(self) -> None:
        model_id = _clean(self.model_id)
        if not _MODEL_ID.match(model_id):
            raise PredictionValidationError(f"invalid model_id {self.model_id!r}")
        if not _clean(self.version):
            raise PredictionValidationError("model version is required")
        try:
            family = ModelFamily(self.family)
            state = ModelState(self.state)
            calibration = CalibrationStatus(self.calibration)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid model metadata enum: {exc}") from exc
        horizons = tuple(ForecastHorizon(item) for item in self.supported_horizons)
        if not horizons:
            raise PredictionValidationError("a model must support at least one horizon")
        if len(set(horizons)) != len(horizons):
            raise PredictionValidationError("model horizons must be unique")
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise PredictionValidationError("model weight must be numeric")
        if not 0.0 < float(self.weight) <= 10.0:
            raise PredictionValidationError("model weight must be within (0, 10]")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "version", _clean(self.version))
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "supported_horizons", horizons)
        object.__setattr__(self, "calibration", calibration)
        object.__setattr__(self, "weight", float(self.weight))
        object.__setattr__(self, "metrics", dict(self.metrics))
        object.__setattr__(self, "trained_at", _aware(self.trained_at, "trained_at") if self.trained_at else None)
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at") if self.updated_at else None)
        object.__setattr__(self, "notes", _clean(self.notes))
        object.__setattr__(
            self,
            "feature_set_version",
            _clean(self.feature_set_version) or "unspecified",
        )
        object.__setattr__(
            self,
            "dataset_version",
            _clean(self.dataset_version) or "unspecified",
        )

    def supports(self, horizon: ForecastHorizon | str) -> bool:
        try:
            return ForecastHorizon(horizon) in self.supported_horizons
        except ValueError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "family": self.family.value,
            "state": self.state.value,
            "supported_horizons": [item.value for item in self.supported_horizons],
            "calibration": self.calibration.value,
            "weight": self.weight,
            "metrics": dict(self.metrics),
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "notes": self.notes,
            "feature_set_version": self.feature_set_version,
            "dataset_version": self.dataset_version,
            "automatic_promotion": False,
        }


@dataclass(frozen=True, slots=True)
class ModelPrediction:
    """One model's contribution, before the ensemble decides whether it is eligible."""

    model_id: str
    model_version: str
    family: ModelFamily
    instrument_id: str
    horizon: ForecastHorizon
    generated_at: datetime
    data_cutoff: datetime
    data_snapshot_id: str
    reference_price: Decimal
    predicted_return: float
    lower_return: float
    upper_return: float
    p_up: float | None = None
    p_down: float | None = None
    p_neutral: float | None = None
    calibration: CalibrationStatus = CalibrationStatus.UNCALIBRATED
    valid: bool = True
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    feature_set_version: str = "unspecified"
    dataset_version: str = "unspecified"

    def __post_init__(self) -> None:
        if not _MODEL_ID.match(_clean(self.model_id)):
            raise PredictionValidationError(f"invalid model_id {self.model_id!r}")
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("prediction instrument_id is not canonical")
        try:
            family = ModelFamily(self.family)
            horizon = ForecastHorizon(self.horizon)
            calibration = CalibrationStatus(self.calibration)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid prediction enum: {exc}") from exc
        for name, value in (
            ("predicted_return", self.predicted_return),
            ("lower_return", self.lower_return),
            ("upper_return", self.upper_return),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PredictionValidationError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise PredictionValidationError(f"{name} must be finite")
        if not self.lower_return <= self.predicted_return <= self.upper_return:
            raise PredictionValidationError(
                "the point prediction must lie inside its interval"
            )
        p_up = _probability(self.p_up, "p_up")
        p_down = _probability(self.p_down, "p_down")
        p_neutral = _probability(self.p_neutral, "p_neutral")
        supplied = [value for value in (p_up, p_down, p_neutral) if value is not None]
        if supplied and len(supplied) != 3:
            raise PredictionValidationError(
                "directional probabilities must be all present or all absent"
            )
        if supplied and abs(sum(supplied) - 1.0) > _PROBABILITY_TOLERANCE:
            raise PredictionValidationError(
                f"directional probabilities must sum to 1 (got {sum(supplied):.9f})"
            )
        generated = _aware(self.generated_at, "generated_at")
        cutoff = _aware(self.data_cutoff, "data_cutoff")
        if cutoff > generated:
            raise PredictionValidationError("data_cutoff may not follow generated_at")
        object.__setattr__(self, "model_id", _clean(self.model_id))
        object.__setattr__(self, "model_version", _clean(self.model_version))
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "data_cutoff", cutoff)
        object.__setattr__(self, "data_snapshot_id", _clean(self.data_snapshot_id))
        object.__setattr__(self, "reference_price", _decimal(self.reference_price, "reference_price"))
        object.__setattr__(self, "predicted_return", float(self.predicted_return))
        object.__setattr__(self, "lower_return", float(self.lower_return))
        object.__setattr__(self, "upper_return", float(self.upper_return))
        object.__setattr__(self, "p_up", p_up)
        object.__setattr__(self, "p_down", p_down)
        object.__setattr__(self, "p_neutral", p_neutral)
        object.__setattr__(self, "calibration", calibration)
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(
            self,
            "feature_set_version",
            _clean(self.feature_set_version) or "unspecified",
        )
        object.__setattr__(
            self,
            "dataset_version",
            _clean(self.dataset_version) or "unspecified",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "family": self.family.value,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "generated_at": self.generated_at.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "data_snapshot_id": self.data_snapshot_id,
            "reference_price": format(self.reference_price, "f"),
            "predicted_return": round(self.predicted_return, 8),
            "lower_return": round(self.lower_return, 8),
            "upper_return": round(self.upper_return, 8),
            "p_up": self.p_up,
            "p_down": self.p_down,
            "p_neutral": self.p_neutral,
            "calibration": self.calibration.value,
            "valid": self.valid,
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
            "feature_set_version": self.feature_set_version,
            "dataset_version": self.dataset_version,
        }


@dataclass(frozen=True, slots=True)
class ForecastOutput:
    """The versioned forecast contract (not a market observation)."""

    forecast_id: str
    lifecycle_id: str
    lifecycle_stage: ConfirmationStage
    instrument_id: str
    exchange: str
    currency: str
    reference_price: Decimal
    reference_at: datetime
    predicted_price: Decimal
    predicted_return: float
    direction: Direction
    lower_return: float
    upper_return: float
    lower_price: Decimal
    upper_price: Decimal
    uncertainty: PredictionUncertainty
    uncertainty_width: float
    p_up: float | None
    p_down: float | None
    p_neutral: float | None
    probability_status: ProbabilityStatus
    calibration_status: CalibrationStatus
    confidence: float | None
    ensemble_id: str
    model_id: str
    model_version: str
    member_versions: tuple[str, ...]
    forecast_horizon: ForecastHorizon
    target_at: datetime
    generated_at: datetime
    data_observed_at: datetime
    data_received_at: datetime
    data_resolution: DataInterval
    data_freshness: Freshness
    data_quality: DataQuality
    data_snapshot_id: str
    validity: ForecastValidity
    validity_conditions: tuple[str, ...]
    disagreement: float
    digest: str
    notes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    feature_set_version: str = "unspecified"
    dataset_version: str = "unspecified"
    contract_version: str = FORECAST_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise PredictionValidationError("forecast instrument_id is not canonical")
        reference = _decimal(self.reference_price, "reference_price")
        predicted = _decimal(self.predicted_price, "predicted_price")
        lower_price = _decimal(self.lower_price, "lower_price")
        upper_price = _decimal(self.upper_price, "upper_price")
        if not lower_price <= predicted <= upper_price:
            raise PredictionValidationError("predicted price must lie inside its interval")
        for name, value in (
            ("predicted_return", self.predicted_return),
            ("lower_return", self.lower_return),
            ("upper_return", self.upper_return),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PredictionValidationError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise PredictionValidationError(f"{name} must be finite")
        if not self.lower_return <= self.predicted_return <= self.upper_return:
            raise PredictionValidationError("predicted return must lie inside its interval")
        generated = _aware(self.generated_at, "generated_at")
        target = _aware(self.target_at, "target_at")
        observed = _aware(self.data_observed_at, "data_observed_at")
        received = _aware(self.data_received_at, "data_received_at")
        reference_at = _aware(self.reference_at, "reference_at")
        try:
            stage = ConfirmationStage(self.lifecycle_stage)
            direction = Direction(self.direction)
            horizon = ForecastHorizon(self.forecast_horizon)
            resolution = DataInterval(self.data_resolution)
            data_freshness = Freshness(self.data_freshness)
            data_quality = DataQuality(self.data_quality)
            probability_status = ProbabilityStatus(self.probability_status)
            calibration_status = CalibrationStatus(self.calibration_status)
            validity = ForecastValidity(self.validity)
            uncertainty = PredictionUncertainty(self.uncertainty)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid forecast enum: {exc}") from exc
        if target <= generated:
            raise PredictionValidationError("target_at must be after generated_at")
        if observed > generated + timedelta(seconds=300):
            raise PredictionValidationError("data_observed_at may not follow generation")
        if reference_at > generated + timedelta(seconds=300):
            raise PredictionValidationError("reference_at may not follow generation")
        if received > generated + timedelta(seconds=300):
            raise PredictionValidationError("data_received_at may not follow generation")
        p_values = [_probability(item, name) for name, item in (
            ("p_up", self.p_up), ("p_down", self.p_down), ("p_neutral", self.p_neutral)
        )]
        supplied = [value for value in p_values if value is not None]
        if supplied and len(supplied) != 3:
            raise PredictionValidationError("forecast probabilities must be all present or all absent")
        if supplied and abs(sum(supplied) - 1.0) > _PROBABILITY_TOLERANCE:
            raise PredictionValidationError("forecast probabilities must sum to 1")
        if probability_status is ProbabilityStatus.UNAVAILABLE and supplied:
            raise PredictionValidationError("unavailable probability status cannot carry values")
        if probability_status is not ProbabilityStatus.CALIBRATED and supplied:
            raise PredictionValidationError(
                "uncalibrated or unavailable probabilities must be withheld"
            )
        if probability_status is ProbabilityStatus.CALIBRATED and not supplied:
            raise PredictionValidationError(
                "calibrated probability status requires all three values"
            )
        if not 0.0 <= float(self.disagreement) <= 1.0:
            raise PredictionValidationError("disagreement must be within [0, 1]")
        if not math.isfinite(float(self.uncertainty_width)) or float(self.uncertainty_width) < 0:
            raise PredictionValidationError("uncertainty_width must be finite and non-negative")
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise PredictionValidationError("confidence must be within [0, 1]")
        object.__setattr__(self, "reference_price", reference)
        object.__setattr__(self, "predicted_price", predicted)
        object.__setattr__(self, "lower_price", lower_price)
        object.__setattr__(self, "upper_price", upper_price)
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "target_at", target)
        object.__setattr__(self, "data_observed_at", observed)
        object.__setattr__(self, "data_received_at", received)
        object.__setattr__(self, "reference_at", reference_at)
        object.__setattr__(self, "lifecycle_stage", stage)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "forecast_horizon", horizon)
        object.__setattr__(self, "data_resolution", resolution)
        object.__setattr__(self, "data_freshness", data_freshness)
        object.__setattr__(self, "data_quality", data_quality)
        object.__setattr__(self, "probability_status", probability_status)
        object.__setattr__(self, "calibration_status", calibration_status)
        object.__setattr__(self, "validity", validity)
        object.__setattr__(self, "uncertainty", uncertainty)
        object.__setattr__(self, "p_up", p_values[0])
        object.__setattr__(self, "p_down", p_values[1])
        object.__setattr__(self, "p_neutral", p_values[2])
        object.__setattr__(self, "validity_conditions", tuple(str(item) for item in self.validity_conditions))
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(
            self,
            "feature_set_version",
            _clean(self.feature_set_version) or "unspecified",
        )
        object.__setattr__(
            self,
            "dataset_version",
            _clean(self.dataset_version) or "unspecified",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "lifecycle_id": self.lifecycle_id,
            "lifecycle_stage": self.lifecycle_stage.value,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "currency": self.currency,
            "reference_price": format(self.reference_price, "f"),
            "reference_price_timestamp": self.reference_at.isoformat(),
            "predicted_price": format(self.predicted_price, "f"),
            "predicted_return": round(self.predicted_return, 8),
            "predicted_direction": self.direction.value,
            "lower_return": round(self.lower_return, 8),
            "upper_return": round(self.upper_return, 8),
            "lower_price": format(self.lower_price, "f"),
            "upper_price": format(self.upper_price, "f"),
            "forecast_uncertainty": self.uncertainty.value,
            "uncertainty_width": round(self.uncertainty_width, 8),
            "p_up": self.p_up,
            "p_down": self.p_down,
            "p_neutral": self.p_neutral,
            "probability_status": self.probability_status.value,
            "calibration_status": self.calibration_status.value,
            "confidence": self.confidence,
            "ensemble_id": self.ensemble_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "member_versions": list(self.member_versions),
            "feature_set_version": self.feature_set_version,
            "dataset_version": self.dataset_version,
            "forecast_horizon": self.forecast_horizon.value,
            "forecast_horizon_seconds": int(
                HORIZON_SPECS[self.forecast_horizon].duration.total_seconds()
            ),
            "forecast_target_timestamp": self.target_at.isoformat(),
            "forecast_generation_timestamp": self.generated_at.isoformat(),
            "underlying_data_timestamp": self.data_observed_at.isoformat(),
            "data_received_timestamp": self.data_received_at.isoformat(),
            "data_resolution": self.data_resolution.value,
            "data_freshness_status": self.data_freshness.value,
            "data_quality": self.data_quality.value,
            "data_snapshot_id": self.data_snapshot_id,
            "forecast_validity": self.validity.value,
            "forecast_validity_conditions": list(self.validity_conditions),
            "ensemble_disagreement": round(self.disagreement, 8),
            "digest": self.digest,
            "notes": list(self.notes),
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
            "contract_version": self.contract_version,
            "is_forecast": True,
            "is_market_observation": False,
            "is_price_prediction": False,
            "is_investment_advice": False,
            "guaranteed": False,
            "authorizes_action": False,
            "risk_authority": RISK_AUTHORITY,
            "execution_authority": EXECUTION_AUTHORITY,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": BROKER_CONNECTED,
            "automatic_promotion": AUTOMATIC_PROMOTION,
        }


#: The public name for the forecast response contract.  ``ForecastOutput`` is
#: the implementation type; callers may import either without a second shape.
PredictionResponse = ForecastOutput


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    ensemble_id: str
    version: str
    status: str
    output: ForecastOutput | None
    eligible_models: tuple[str, ...]
    excluded_models: tuple[Mapping[str, Any], ...]
    weights: Mapping[str, float]
    disagreement: float
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ensemble_id": self.ensemble_id,
            "version": self.version,
            "status": self.status,
            "eligible_models": list(self.eligible_models),
            "excluded_models": [dict(item) for item in self.excluded_models],
            "weights": dict(self.weights),
            "disagreement": round(self.disagreement, 8),
            "reasons": list(self.reasons),
            "forecast": self.output.to_dict() if self.output else None,
            "abstained": self.output is None,
        }


@dataclass(frozen=True, slots=True)
class LifecycleStageRecord:
    stage_id: str
    lifecycle_id: str
    stage: ConfirmationStage
    scheduled_at: datetime
    recorded_at: datetime
    status: StageStatus
    forecast_id: str = ""
    previous_forecast_id: str = ""
    data_observed_at: datetime | None = None
    data_freshness: Freshness | None = None
    data_resolution: DataInterval | None = None
    changed_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            stage = ConfirmationStage(self.stage)
            status = StageStatus(self.status)
            freshness = Freshness(self.data_freshness) if self.data_freshness else None
            resolution = DataInterval(self.data_resolution) if self.data_resolution else None
        except ValueError as exc:
            raise PredictionValidationError(f"invalid stage enum: {exc}") from exc
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "data_freshness", freshness)
        object.__setattr__(self, "data_resolution", resolution)
        object.__setattr__(self, "scheduled_at", _aware(self.scheduled_at, "scheduled_at"))
        object.__setattr__(self, "recorded_at", _aware(self.recorded_at, "recorded_at"))
        object.__setattr__(self, "data_observed_at", _aware(self.data_observed_at, "data_observed_at") if self.data_observed_at else None)
        object.__setattr__(self, "changed_fields", tuple(str(item) for item in self.changed_fields))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "lifecycle_id": self.lifecycle_id,
            "stage": self.stage.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "status": self.status.value,
            "forecast_id": self.forecast_id,
            "previous_forecast_id": self.previous_forecast_id,
            "data_observed_at": (
                self.data_observed_at.isoformat() if self.data_observed_at else None
            ),
            "data_freshness": self.data_freshness.value if self.data_freshness else None,
            "data_resolution": self.data_resolution.value if self.data_resolution else None,
            "changed_fields": list(self.changed_fields),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ForecastLifecycle:
    lifecycle_id: str
    request_id: str
    instrument_id: str
    exchange: str
    horizon: ForecastHorizon
    target_at: datetime
    initial_at: datetime
    first_confirmation_at: datetime
    final_confirmation_at: datetime
    status: LifecycleStatus
    created_at: datetime
    updated_at: datetime
    initial_forecast_id: str = ""
    first_forecast_id: str = ""
    final_forecast_id: str = ""
    evaluation_ids: tuple[str, ...] = ()
    invalidation_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "horizon", ForecastHorizon(self.horizon))
        for name in ("target_at", "initial_at", "first_confirmation_at", "final_confirmation_at", "created_at", "updated_at"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        object.__setattr__(self, "status", LifecycleStatus(self.status))
        object.__setattr__(self, "evaluation_ids", tuple(str(item) for item in self.evaluation_ids))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "request_id": self.request_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon.value,
            "target_at": self.target_at.isoformat(),
            "initial_at": self.initial_at.isoformat(),
            "first_confirmation_at": self.first_confirmation_at.isoformat(),
            "final_confirmation_at": self.final_confirmation_at.isoformat(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "initial_forecast_id": self.initial_forecast_id,
            "first_forecast_id": self.first_forecast_id,
            "final_forecast_id": self.final_forecast_id,
            "evaluation_ids": list(self.evaluation_ids),
            "invalidation_reason": self.invalidation_reason,
            "metadata": dict(self.metadata),
            "contract_version": LIFECYCLE_CONTRACT_VERSION,
            "initial_prediction_preserved": True,
            "confirmation_never_overwrites": True,
        }


@dataclass(frozen=True, slots=True)
class ForecastEvaluation:
    evaluation_id: str
    lifecycle_id: str
    forecast_id: str
    stage: ConfirmationStage
    evaluated_at: datetime
    target_at: datetime
    reference_price: Decimal
    actual_price: Decimal
    actual_return: float
    predicted_return: float
    predicted_direction: Direction
    actual_direction: Direction
    direction_correct: bool
    error: float
    absolute_error: float
    inside_interval: bool
    brier: float | None
    log_loss: float | None
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        try:
            stage = ConfirmationStage(self.stage)
            predicted_direction = Direction(self.predicted_direction)
            actual_direction = Direction(self.actual_direction)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid evaluation enum: {exc}") from exc
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "predicted_direction", predicted_direction)
        object.__setattr__(self, "actual_direction", actual_direction)
        object.__setattr__(self, "evaluated_at", _aware(self.evaluated_at, "evaluated_at"))
        object.__setattr__(self, "target_at", _aware(self.target_at, "target_at"))
        object.__setattr__(self, "reference_price", _decimal(self.reference_price, "reference_price"))
        object.__setattr__(self, "actual_price", _decimal(self.actual_price, "actual_price"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "lifecycle_id": self.lifecycle_id,
            "forecast_id": self.forecast_id,
            "stage": self.stage.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "target_at": self.target_at.isoformat(),
            "reference_price": format(self.reference_price, "f"),
            "actual_price": format(self.actual_price, "f"),
            "actual_return": round(self.actual_return, 8),
            "predicted_return": round(self.predicted_return, 8),
            "predicted_direction": self.predicted_direction.value,
            "actual_direction": self.actual_direction.value,
            "direction_correct": self.direction_correct,
            "error": round(self.error, 8),
            "absolute_error": round(self.absolute_error, 8),
            "inside_interval": self.inside_interval,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "status": self.status,
            "reason": self.reason,
            "is_evaluation": True,
            "is_market_observation": False,
        }


@dataclass(frozen=True, slots=True)
class DecisionForecastEvidence:
    """The only bridge into the existing autonomous decision pipeline.

    It carries forecast evidence and explicitly says that it does not authorize
    an action.  NM-08 and NM-10 still decide and execute respectively.
    """

    evidence_id: str
    lifecycle_id: str
    forecast_id: str
    instrument_id: str
    portfolio_id: str
    direction: Direction
    expected_return: float
    uncertainty: PredictionUncertainty
    p_up: float | None
    p_down: float | None
    p_neutral: float | None
    reference_price: Decimal
    reference_at: datetime
    target_at: datetime
    generated_at: datetime
    data_observed_at: datetime
    data_freshness: Freshness
    final_confirmation: bool
    eligible: bool
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            direction = Direction(self.direction)
            uncertainty = PredictionUncertainty(self.uncertainty)
            freshness = Freshness(self.data_freshness)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid evidence enum: {exc}") from exc
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "uncertainty", uncertainty)
        object.__setattr__(self, "data_freshness", freshness)
        object.__setattr__(self, "reference_price", _decimal(self.reference_price, "reference_price"))
        for name in ("reference_at", "target_at", "generated_at", "data_observed_at"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def authorizes_action(self) -> bool:
        """Decision evidence is never permission to act."""
        return False

    @property
    def risk_authority(self) -> str:
        return RISK_AUTHORITY

    @property
    def execution_authority(self) -> str:
        return EXECUTION_AUTHORITY

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "lifecycle_id": self.lifecycle_id,
            "forecast_id": self.forecast_id,
            "instrument_id": self.instrument_id,
            "portfolio_id": self.portfolio_id,
            "direction": self.direction.value,
            "expected_return": round(self.expected_return, 8),
            "uncertainty": self.uncertainty.value,
            "p_up": self.p_up,
            "p_down": self.p_down,
            "p_neutral": self.p_neutral,
            "reference_price": format(self.reference_price, "f"),
            "reference_price_timestamp": self.reference_at.isoformat(),
            "target_at": self.target_at.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "data_observed_at": self.data_observed_at.isoformat(),
            "data_freshness": self.data_freshness.value,
            "final_confirmation": self.final_confirmation,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
            "authorizes_action": False,
            "is_forecast": True,
            "is_market_observation": False,
            "is_price_prediction": False,
            "is_investment_advice": False,
            "is_trading_recommendation": False,
            "risk_authority": RISK_AUTHORITY,
            "execution_authority": EXECUTION_AUTHORITY,
            "decision_authority": "existing autonomous trading pipeline",
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": BROKER_CONNECTED,
        }


def direction_for_return(
    predicted_return: float, neutral_band_pct: float
) -> Direction:
    """Classify a return without pretending an uncertain model is directional."""
    if not math.isfinite(predicted_return):
        return Direction.INVALID
    band = abs(float(neutral_band_pct)) / 100.0
    if predicted_return > band:
        return Direction.UP
    if predicted_return < -band:
        return Direction.DOWN
    return Direction.NEUTRAL


def uncertainty_for_width(width: float, policy: PredictionPolicy) -> PredictionUncertainty:
    if not math.isfinite(width) or width < 0:
        return PredictionUncertainty.UNKNOWN
    if width <= policy.uncertainty_low_pct / 100.0:
        return PredictionUncertainty.LOW
    if width >= policy.uncertainty_high_pct / 100.0:
        return PredictionUncertainty.HIGH
    return PredictionUncertainty.MEDIUM


def learning_boundary_contract() -> dict[str, Any]:
    """The advisory-only RL boundary; no algorithm or promotion is implemented."""
    return {
        "contract_version": LEARNING_CONTRACT_VERSION,
        "phase": PI_PHASE,
        "advisory_only": True,
        "automatic_promotion": False,
        "human_authorization_required": True,
        "risk_policy_mutation": False,
        "recommended_action_is_not_a_fill": True,
        "experience_fields": [
            "experience_id",
            "decision_at",
            "state",
            "observation",
            "action",
            "reward",
            "penalty",
            "episode_id",
            "policy_id",
            "policy_version",
            "risk_decision_id",
            "forecast_lifecycle_id",
            "outcome",
            "evaluation",
        ],
        "reward_components": [
            "realized_pnl",
            "risk_adjusted_return",
            "drawdown",
            "transaction_costs",
            "slippage",
            "capital_utilization",
            "decision_quality",
            "unnecessary_trading",
            "avoidable_loss",
            "risk_compliance",
        ],
        "evaluation_environments": ["historical_replay", "paper_trading"],
        "counterfactual_rule": (
            "A profitable outcome does not make a decision good, and a losing "
            "outcome does not make it irrational; score only information available "
            "at decision time."
        ),
        "known_limits": [
            "No RL algorithm, environment, reward implementation or training run is implemented in PI-01.",
            "No policy may modify NM-08 risk limits or promote itself.",
        ],
    }
