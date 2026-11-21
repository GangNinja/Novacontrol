"""PI-11 evaluation contracts.

Forecast evaluation is deliberately separate from trading performance.  These
types describe predictions and their realised target observations only; they
never carry an order, fill, portfolio or ledger mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

PI11_PHASE = "PI-11"
REPORT_CONTRACT_VERSION = "pi11-evaluation-1.0"
RECORD_ORIGINS = {
    "historical_operational",
    "research_reconstruction",
    "synthetic_fixture",
}
DIRECTIONS = {"up", "down", "neutral"}
NON_DIRECTIONAL = {"uncertain", "abstained", "invalid", "unknown"}


class EvaluationError(ValueError):
    """A configuration or evidence error that makes evaluation unavailable."""


class EvaluationNotFoundError(EvaluationError):
    """The requested persisted evaluation does not exist."""


def _utc(value: object, name: str, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max if end_of_day else time.min)
    elif value in (None, ""):
        raise EvaluationError(f"{name} is required")
    else:
        text = str(value).strip()
        if not text:
            raise EvaluationError(f"{name} is required")
        try:
            if len(text) == 10:
                day = date.fromisoformat(text)
                parsed = datetime.combine(day, time.max if end_of_day else time.min)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvaluationError(f"{name} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvaluationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise EvaluationError(f"{name} must be numeric")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise EvaluationError(f"{name} must be finite")
    return number


def _price(value: object, name: str) -> float:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EvaluationError(f"{name} is not a decimal price") from exc
    if not number.is_finite() or number <= 0:
        raise EvaluationError(f"{name} must be finite and positive")
    return float(number)


def _direction(value: object, name: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "upward": "up",
        "bullish": "up",
        "positive": "up",
        "downward": "down",
        "bearish": "down",
        "negative": "down",
        "sideways": "neutral",
        "flat": "neutral",
    }
    text = aliases.get(text, text)
    if text in DIRECTIONS or text in NON_DIRECTIONAL:
        return text
    raise EvaluationError(f"{name} is not a forecast direction: {value!r}")


def _sequence(value: object) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise EvaluationError("expected a list or comma-separated string")


def config_hash(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(config), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_payload(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def report_hash(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    for key in ("created_at", "evaluation_id", "report_hash", "verification"):
        body.pop(key, None)
    return hashlib.sha256(canonical_payload(body).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """One reproducible forecast-evaluation request.

    ``sources`` selects existing forecast stores; it does not create records.
    ``record_origin`` is used only for explicit caller-supplied reconstruction
    evidence, and is reported verbatim so a reconstructed backtest can never be
    mistaken for an operational forecast.
    """

    name: str = "forecast evaluation"
    sources: tuple[str, ...] = ("lifecycle", "nm06")
    instruments: tuple[str, ...] = ()
    horizons: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    start: datetime | None = None
    end: datetime | None = None
    as_of: datetime | None = None
    neutral_threshold: float = 0.005
    interval_confidence: float = 0.80
    min_samples: int = 20
    small_sample_size: int = 10
    walk_forward_folds: int = 0
    max_records: int = 5000
    include_invalid: bool = False
    direction_policy: str = "exclude_uncertain"
    neutral_policy: str = "exact"
    # Conservative fallback only; explicit records must still carry their own
    # declared origin.  It is never used to upgrade missing provenance.
    record_origin: str = "synthetic_fixture"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name or "").strip() or "forecast evaluation")
        sources = tuple(dict.fromkeys(item.lower() for item in _sequence(self.sources)))
        if not sources:
            raise EvaluationError("at least one evaluation source is required")
        unsupported = sorted(set(sources) - {"lifecycle", "nm06", "records"})
        if unsupported:
            raise EvaluationError(
                "unsupported evaluation source(s): " + ", ".join(unsupported)
            )
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "instruments", tuple(dict.fromkeys(_sequence(self.instruments))))
        object.__setattr__(self, "horizons", tuple(dict.fromkeys(_sequence(self.horizons))))
        object.__setattr__(self, "models", tuple(dict.fromkeys(_sequence(self.models))))
        object.__setattr__(self, "stages", tuple(dict.fromkeys(_sequence(self.stages))))
        for field_name in ("start", "end", "as_of"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _utc(value, field_name, end_of_day=field_name == "end"),
                )
        if self.start is not None and self.end is not None and self.start > self.end:
            raise EvaluationError("start must not follow end")
        if self.as_of is None:
            object.__setattr__(self, "as_of", datetime.now(UTC))
        object.__setattr__(self, "neutral_threshold", _number(self.neutral_threshold, "neutral_threshold"))
        if not 0.0 <= self.neutral_threshold < 1.0:
            raise EvaluationError("neutral_threshold must be within [0, 1)")
        object.__setattr__(self, "interval_confidence", _number(self.interval_confidence, "interval_confidence"))
        if not 0.5 <= self.interval_confidence < 1.0:
            raise EvaluationError("interval_confidence must be within [0.5, 1)")
        for field_name in ("min_samples", "small_sample_size", "walk_forward_folds", "max_records"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EvaluationError(f"{field_name} must be a non-negative integer")
        if self.min_samples < 2:
            raise EvaluationError("min_samples must be at least 2")
        if self.small_sample_size < 2:
            raise EvaluationError("small_sample_size must be at least 2")
        if self.walk_forward_folds > 10:
            raise EvaluationError("walk_forward_folds must be at most 10")
        if not 2 <= self.max_records <= 20_000:
            raise EvaluationError("max_records must be within [2, 20000]")
        if self.direction_policy not in {"exclude_uncertain", "count_uncertain_incorrect"}:
            raise EvaluationError(
                "direction_policy must be exclude_uncertain or count_uncertain_incorrect"
            )
        if self.neutral_policy not in {"exact", "exclude"}:
            raise EvaluationError("neutral_policy must be exact or exclude")
        if self.record_origin not in RECORD_ORIGINS:
            raise EvaluationError(
                "record_origin must be historical_operational, research_reconstruction "
                "or synthetic_fixture"
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "EvaluationConfig":
        values = dict(payload)
        if "horizon" in values and "horizons" not in values:
            values["horizons"] = values.pop("horizon")
        if "instrument_id" in values and "instruments" not in values:
            values["instruments"] = values.pop("instrument_id")
        if "model_id" in values and "models" not in values:
            values["models"] = values.pop("model_id")
        if "stage" in values and "stages" not in values:
            values["stages"] = values.pop("stage")
        if "records" in values:
            values.pop("records")
        if "sources" not in values or values["sources"] is None:
            values["sources"] = ("lifecycle", "nm06")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise EvaluationError(
                "unknown evaluation configuration field(s): " + ", ".join(unknown)
            )
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sources": list(self.sources),
            "instruments": list(self.instruments),
            "horizons": list(self.horizons),
            "models": list(self.models),
            "stages": list(self.stages),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "neutral_threshold": self.neutral_threshold,
            "interval_confidence": self.interval_confidence,
            "min_samples": self.min_samples,
            "small_sample_size": self.small_sample_size,
            "walk_forward_folds": self.walk_forward_folds,
            "max_records": self.max_records,
            "include_invalid": self.include_invalid,
            "direction_policy": self.direction_policy,
            "neutral_policy": self.neutral_policy,
            "record_origin": self.record_origin,
            "metadata": dict(self.metadata),
        }

    @property
    def hash(self) -> str:
        return config_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One forecast and the realised target observation used to score it.

    Prices are kept as floats for numerical metrics; the source identity and
    timestamps are kept verbatim.  Nothing here is inferred from a later price.
    """

    forecast_id: str
    source: str
    instrument_id: str
    horizon: str
    generated_at: datetime
    target_at: datetime
    reference_at: datetime
    actual_observed_at: datetime
    reference_price: float
    predicted_price: float
    actual_price: float
    predicted_return: float
    actual_return: float
    predicted_direction: str
    model_id: str = ""
    model_version: str = ""
    ensemble_id: str = ""
    dataset_version: str = ""
    feature_set_version: str = ""
    cycle_id: str = ""
    lifecycle_id: str = ""
    stage: str = ""
    p_up: float | None = None
    p_down: float | None = None
    p_neutral: float | None = None
    probability_status: str = ""
    interval_lower: float | None = None
    interval_upper: float | None = None
    interval_width: float | None = None
    inside_interval: bool | None = None
    uncertainty: str = ""
    disagreement: float | None = None
    regime: str = "unknown"
    data_quality: str = ""
    data_freshness: str = ""
    validity: str = ""
    # Explicit records must declare an origin through the service; this low-level
    # default is deliberately non-operational so omitted provenance can never
    # turn caller-supplied evidence into a historical claim.
    origin: str = "synthetic_fixture"
    reconstructed: bool = False
    model_trained_at: datetime | None = None
    information_cutoff: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            "forecast_id": self.forecast_id,
            "source": self.source,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "predicted_direction": self.predicted_direction,
        }
        for name, value in required.items():
            if not str(value or "").strip():
                raise EvaluationError(f"evaluation record requires {name}")
        for name in ("generated_at", "target_at", "reference_at", "actual_observed_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), f"record.{name}"))
        for name in ("reference_price", "predicted_price", "actual_price"):
            object.__setattr__(self, name, _price(getattr(self, name), f"record.{name}"))
        for name in ("predicted_return", "actual_return"):
            object.__setattr__(self, name, _number(getattr(self, name), f"record.{name}"))
        object.__setattr__(
            self,
            "predicted_direction",
            _direction(self.predicted_direction, "record.predicted_direction"),
        )
        if self.target_at <= self.generated_at:
            raise EvaluationError("a forecast target must follow its generation time")
        if self.reference_at > self.generated_at + timedelta(minutes=5):
            raise EvaluationError("forecast reference data follows generation")
        if self.actual_observed_at < self.target_at:
            raise EvaluationError("actual observation precedes the target timestamp")
        if self.origin not in RECORD_ORIGINS:
            raise EvaluationError(f"unknown record origin {self.origin!r}")
        object.__setattr__(self, "origin", self.origin)
        object.__setattr__(self, "reconstructed", self.origin == "research_reconstruction")
        if self.reconstructed and self.model_trained_at is None:
            raise EvaluationError(
                "a reconstructed record must state its model training timestamp"
            )
        if self.reconstructed and self.information_cutoff is None:
            raise EvaluationError(
                "a reconstructed record must state its information cutoff"
            )
        if self.model_trained_at is not None:
            object.__setattr__(self, "model_trained_at", _utc(self.model_trained_at, "model_trained_at"))
        if self.information_cutoff is not None:
            object.__setattr__(self, "information_cutoff", _utc(self.information_cutoff, "information_cutoff"))
        if self.model_trained_at is not None:
            if self.model_trained_at > self.generated_at:
                raise EvaluationError(
                    "a forecast may not use a model trained after generation"
                )
        if self.information_cutoff is not None:
            if self.information_cutoff > self.generated_at:
                raise EvaluationError(
                    "a forecast may not use information available after generation"
                )
        p_values = (self.p_up, self.p_down, self.p_neutral)
        supplied = [value for value in p_values if value is not None]
        if supplied and len(supplied) != 3:
            raise EvaluationError("probability values must be all present or all absent")
        if supplied:
            cleaned = tuple(_number(value, "probability") for value in p_values)
            if any(value < 0.0 or value > 1.0 for value in cleaned):
                raise EvaluationError("probabilities must be within [0, 1]")
            if abs(sum(cleaned) - 1.0) > 1e-6:
                raise EvaluationError("probabilities must sum to 1")
            object.__setattr__(self, "p_up", cleaned[0])
            object.__setattr__(self, "p_down", cleaned[1])
            object.__setattr__(self, "p_neutral", cleaned[2])
        for name in ("interval_lower", "interval_upper", "interval_width"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _number(value, f"record.{name}"))
        if self.interval_lower is not None and self.interval_upper is not None:
            if self.interval_lower > self.interval_upper:
                raise EvaluationError("interval lower bound exceeds upper bound")
            if self.interval_width is None:
                object.__setattr__(
                    self, "interval_width", self.interval_upper - self.interval_lower
                )
        if self.disagreement is not None:
            disagreement = _number(self.disagreement, "record.disagreement")
            if disagreement < 0.0:
                raise EvaluationError("record.disagreement must not be negative")
            object.__setattr__(self, "disagreement", disagreement)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def actual_direction(self) -> str:
        # Computed by the evaluator with the configured neutral threshold.
        return "unknown"

    @property
    def information_available_at(self) -> datetime:
        return self.information_cutoff or self.generated_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecast_id": self.forecast_id,
            "source": self.source,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "generated_at": self.generated_at.isoformat(),
            "target_at": self.target_at.isoformat(),
            "reference_at": self.reference_at.isoformat(),
            "actual_observed_at": self.actual_observed_at.isoformat(),
            "reference_price": self.reference_price,
            "predicted_price": self.predicted_price,
            "actual_price": self.actual_price,
            "predicted_return": self.predicted_return,
            "actual_return": self.actual_return,
            "predicted_direction": self.predicted_direction,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "ensemble_id": self.ensemble_id,
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "cycle_id": self.cycle_id,
            "lifecycle_id": self.lifecycle_id,
            "stage": self.stage,
            "p_up": self.p_up,
            "p_down": self.p_down,
            "p_neutral": self.p_neutral,
            "probability_status": self.probability_status,
            "interval_lower": self.interval_lower,
            "interval_upper": self.interval_upper,
            "interval_width": self.interval_width,
            "inside_interval": self.inside_interval,
            "uncertainty": self.uncertainty,
            "disagreement": self.disagreement,
            "regime": self.regime,
            "data_quality": self.data_quality,
            "data_freshness": self.data_freshness,
            "validity": self.validity,
            "origin": self.origin,
            "reconstructed": self.reconstructed,
            "model_trained_at": self.model_trained_at.isoformat() if self.model_trained_at else None,
            "information_cutoff": self.information_cutoff.isoformat() if self.information_cutoff else None,
            "metadata": dict(self.metadata),
        }


def record_from_mapping(payload: Mapping[str, Any]) -> EvaluationRecord:
    """Read caller-supplied research evidence with explicit provenance fields."""

    values = dict(payload)
    aliases = {
        "created_at": "generated_at",
        "observed_at": "actual_observed_at",
        "reference_price_at": "reference_at",
        "forecast_horizon": "horizon",
        "direction": "predicted_direction",
        "predicted_direction_name": "predicted_direction",
        "lower_return": "interval_lower",
        "upper_return": "interval_upper",
        "model_version_id": "model_version",
        "ensemble_version": "ensemble_id",
        "backend_at": "information_cutoff",
    }
    for source, target in aliases.items():
        if source in values and target not in values:
            values[target] = values.pop(source)
    values.setdefault("source", "records")
    values.setdefault("origin", "synthetic_fixture")
    if values.get("reconstructed") and values.get("origin") == "historical_operational":
        raise EvaluationError(
            "reconstructed=true requires an explicit non-operational origin"
        )
    allowed = set(EvaluationRecord.__dataclass_fields__)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise EvaluationError(
            "unknown evaluation record field(s): " + ", ".join(unknown)
        )
    return EvaluationRecord(**values)


def record_sample(records: Sequence[EvaluationRecord], limit: int = 25) -> list[dict[str, Any]]:
    return [item.to_dict() for item in records[: max(0, limit)]]


__all__ = [
    "DIRECTIONS",
    "EvaluationConfig",
    "EvaluationError",
    "EvaluationNotFoundError",
    "EvaluationRecord",
    "NON_DIRECTIONAL",
    "PI11_PHASE",
    "RECORD_ORIGINS",
    "REPORT_CONTRACT_VERSION",
    "canonical_payload",
    "config_hash",
    "record_from_mapping",
    "record_sample",
    "report_hash",
]
