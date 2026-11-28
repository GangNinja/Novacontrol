"""Immutable PI-04 contracts for features, regimes and preprocessing.

These models are deliberately separate from the PI-01 prediction contracts:
PI-04 needs to distinguish a genuine zero from a missing, unavailable, invalid
or imputed value, while a prediction vector may only contain finite numbers.
``FeatureSet.to_prediction_vector`` is the explicit bridge between the two.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from novacontrol.novamarket.prediction.models import (
    FeatureDefinition,
    FeatureVector,
    _aware,
    _clean,
)

FEATURE_SCHEMA_VERSION = 1
FEATURE_CONTRACT_VERSION = "pi04.features.1.0"
FEATURE_SET_VERSION = "pi04-feature-set-1.0"
REGIME_METHOD_VERSION = "pi04-regime-1.0"
PREPROCESSING_VERSION = "pi04-preprocess-1.0"
SELECTION_VERSION = "pi04-selection-1.0"
SUPPORTED_RESOLUTIONS = (
    "1m",
    "5m",
    "15m",
    "20m",
    "30m",
    "1h",
    "1d",
    "1wk",
    "1mo",
)

_INSTRUMENT_ID = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,63}$")


class FeatureValidationError(ValueError):
    """A PI-04 contract was violated before it could be persisted."""


class FeatureFamily(StrEnum):
    PRICE = "price"
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    VOLUME = "volume"
    STRUCTURE = "structure"
    MULTI_TIMEFRAME = "multi_timeframe"
    CONTEXT = "context"
    FUNDAMENTAL = "fundamental"
    NEWS_EVENT = "news_event"


class FeatureStatus(StrEnum):
    VALUE = "value"
    ZERO = "zero"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    IMPUTED = "imputed"
    PROVISIONAL = "provisional"


class FeatureQuality(StrEnum):
    VALID = "valid"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    INVALID = "invalid"


class FeatureProfileName(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    LONG_TERM = "long_term"


class RegimeLabel(StrEnum):
    STRONG_UP = "strong_up"
    WEAK_UP = "weak_up"
    STRONG_DOWN = "strong_down"
    WEAK_DOWN = "weak_down"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    POTENTIAL_REVERSAL = "potential_reversal"
    BREAKOUT = "breakout"
    BREAKDOWN = "breakdown"
    UNCERTAIN = "uncertain"


class PreprocessingMethod(StrEnum):
    STANDARD = "standard"
    ROBUST = "robust"
    MINMAX = "minmax"
    L2 = "l2"


class MissingPolicy(StrEnum):
    ERROR = "error"
    DROP = "drop"
    MEAN = "mean"
    MEDIAN = "median"
    ZERO = "zero"


class SelectionIssue(StrEnum):
    HIGHLY_CORRELATED = "highly_correlated"
    ZERO_VARIANCE = "zero_variance"
    LOW_VARIANCE = "low_variance"
    EXCESSIVE_MISSINGNESS = "excessive_missingness"
    UNSTABLE = "unstable"
    LEAKAGE = "leakage"


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_instrument(value: str) -> str:
    cleaned = _clean(value).upper()
    if not _INSTRUMENT_ID.match(cleaned):
        raise FeatureValidationError(f"instrument_id {value!r} is not canonical")
    return cleaned


def _canonical_resolution(value: str) -> str:
    cleaned = _clean(value).lower()
    aliases = {
        "1min": "1m",
        "minute": "1m",
        "1minute": "1m",
        "5min": "5m",
        "15min": "15m",
        "20min": "20m",
        "30min": "30m",
        "60m": "1h",
        "hour": "1h",
        "hourly": "1h",
        "daily": "1d",
        "day": "1d",
        "weekly": "1wk",
        "week": "1wk",
        "monthly": "1mo",
        "month": "1mo",
    }
    return aliases.get(cleaned, cleaned)


def _family(value: FeatureFamily | str) -> FeatureFamily:
    try:
        return FeatureFamily(value)
    except ValueError as exc:
        raise FeatureValidationError(f"unsupported feature family {value!r}") from exc


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """A versioned definition fixed before any values are generated."""

    name: str
    family: FeatureFamily
    description: str
    formula: str
    version: str = FEATURE_SET_VERSION
    dtype: str = "float"
    lookback: str = ""
    profiles: tuple[FeatureProfileName, ...] = (
        FeatureProfileName.INTRADAY,
        FeatureProfileName.SWING,
        FeatureProfileName.LONG_TERM,
    )
    provisional: bool = False
    source: str = "marketdata"

    def __post_init__(self) -> None:
        if not _clean(self.name) or not _clean(self.formula):
            raise FeatureValidationError("feature name and formula are required")
        object.__setattr__(self, "family", _family(self.family))
        object.__setattr__(
            self,
            "profiles",
            tuple(FeatureProfileName(item) for item in self.profiles),
        )

    def definition(self) -> FeatureDefinition:
        return FeatureDefinition(
            name=self.name,
            version=self.version,
            dtype=self.dtype,
            description=self.description,
            source=self.source,
            lookback=self.lookback,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": str(self.family),
            "description": self.description,
            "formula": self.formula,
            "version": self.version,
            "dtype": self.dtype,
            "lookback": self.lookback,
            "profiles": [str(item) for item in self.profiles],
            "provisional": self.provisional,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class FeatureRequest:
    """What to build; acquisition remains in the existing data layer."""

    instrument_id: str
    as_of: datetime
    resolution: str = "1d"
    profile: FeatureProfileName | str = FeatureProfileName.SWING
    exchange: str = ""
    timeframes: tuple[str, ...] = ()
    include_context: bool = True
    include_fundamentals: bool = True
    include_news: bool = True
    dataset_id: str = ""
    dataset_version: str = ""
    max_bars: int = 500

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _canonical_instrument(self.instrument_id))
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "resolution", _canonical_resolution(self.resolution))
        object.__setattr__(self, "profile", FeatureProfileName(self.profile))
        object.__setattr__(self, "exchange", _clean(self.exchange).upper())
        object.__setattr__(
            self,
            "timeframes",
            tuple(
                dict.fromkeys(
                    _canonical_resolution(item) for item in self.timeframes if _clean(item)
                )
            ),
        )
        if self.resolution not in SUPPORTED_RESOLUTIONS:
            raise FeatureValidationError(f"unsupported resolution {self.resolution!r}")
        unsupported = [item for item in self.timeframes if item not in SUPPORTED_RESOLUTIONS]
        if unsupported:
            raise FeatureValidationError(f"unsupported timeframes {unsupported!r}")
        if self.max_bars < 30:
            raise FeatureValidationError("max_bars must provide at least 30 observations")
        if self.max_bars > 20_000:
            raise FeatureValidationError("max_bars exceeds the bounded-memory ceiling")
        if bool(self.dataset_id) != bool(self.dataset_version):
            raise FeatureValidationError(
                "dataset_id and dataset_version must be supplied together"
            )

    @property
    def requested_timeframes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.resolution,) + self.timeframes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "resolution": self.resolution,
            "profile": str(self.profile),
            "exchange": self.exchange,
            "timeframes": list(self.timeframes),
            "include_context": self.include_context,
            "include_fundamentals": self.include_fundamentals,
            "include_news": self.include_news,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "max_bars": self.max_bars,
        }


@dataclass(frozen=True, slots=True)
class FeatureSet:
    """A reproducible point-in-time feature row with explicit value states."""

    feature_set_id: str
    version: str
    instrument_id: str
    as_of: datetime
    resolution: str
    profile: FeatureProfileName
    values: Mapping[str, float]
    statuses: Mapping[str, FeatureStatus | str]
    reasons: Mapping[str, str] = field(default_factory=dict)
    imputed: Mapping[str, float] = field(default_factory=dict)
    exchange: str = ""
    feature_names: tuple[str, ...] = ()
    timeframes: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    timeframe_statuses: Mapping[str, Mapping[str, FeatureStatus | str]] = field(
        default_factory=dict
    )
    source_first_at: datetime | None = None
    source_last_at: datetime | None = None
    retrieved_at: datetime | None = None
    generated_at: datetime | None = None
    data_snapshot_ids: tuple[str, ...] = ()
    dataset_versions: tuple[str, ...] = ()
    expected_count: int = 0
    completeness: float = 0.0
    quality: FeatureQuality = FeatureQuality.PARTIAL
    lineage: Mapping[str, Any] = field(default_factory=dict)
    digest_value: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.feature_set_id) or not _clean(self.version):
            raise FeatureValidationError("feature set id and version are required")
        object.__setattr__(self, "instrument_id", _canonical_instrument(self.instrument_id))
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "profile", FeatureProfileName(self.profile))
        object.__setattr__(self, "resolution", _clean(self.resolution).lower())
        if not self.feature_names and self.values:
            object.__setattr__(self, "feature_names", tuple(sorted(self.values)))
        if self.expected_count < 0:
            raise FeatureValidationError("expected_count cannot be negative")
        if not 0.0 <= float(self.completeness) <= 1.0:
            raise FeatureValidationError("completeness must be within [0, 1]")
        cleaned_values: dict[str, float] = {}
        for name, value in self.values.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise FeatureValidationError(f"feature {name!r} is not numeric") from exc
            if not math.isfinite(number):
                raise FeatureValidationError(f"feature {name!r} is not finite")
            cleaned_values[str(name)] = number
        known = set(self.feature_names) | set(cleaned_values)
        statuses: dict[str, FeatureStatus] = {}
        for name, status in self.statuses.items():
            try:
                statuses[str(name)] = FeatureStatus(status)
            except ValueError as exc:
                raise FeatureValidationError(
                    f"unsupported feature status {status!r} for {name!r}"
                ) from exc
        for name in known:
            statuses.setdefault(
                name, FeatureStatus.VALUE if name in cleaned_values else FeatureStatus.MISSING
            )
        if set(self.imputed) - set(cleaned_values):
            raise FeatureValidationError("an imputed value must also be a feature value")
        if self.source_last_at is not None:
            last = _aware(self.source_last_at, "source_last_at")
            if last > self.as_of:
                raise FeatureValidationError("source data is newer than the feature cutoff")
            object.__setattr__(self, "source_last_at", last)
        if self.source_first_at is not None:
            object.__setattr__(
                self, "source_first_at", _aware(self.source_first_at, "source_first_at")
            )
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", _aware(self.retrieved_at, "retrieved_at"))
        if self.generated_at is not None:
            object.__setattr__(self, "generated_at", _aware(self.generated_at, "generated_at"))
            if self.generated_at < self.as_of:
                raise FeatureValidationError("generated_at precedes the feature cutoff")
        object.__setattr__(self, "values", cleaned_values)
        object.__setattr__(self, "statuses", statuses)
        object.__setattr__(self, "reasons", {str(k): str(v) for k, v in self.reasons.items()})
        object.__setattr__(self, "imputed", {str(k): float(v) for k, v in self.imputed.items()})
        object.__setattr__(
            self,
            "timeframes",
            {str(k): {str(n): float(v) for n, v in row.items()} for k, row in self.timeframes.items()},
        )
        object.__setattr__(
            self,
            "timeframe_statuses",
            {
                str(k): {str(n): FeatureStatus(v) for n, v in row.items()}
                for k, row in self.timeframe_statuses.items()
            },
        )
        object.__setattr__(self, "feature_names", tuple(str(n) for n in self.feature_names))
        object.__setattr__(
            self, "dataset_versions", tuple(str(v) for v in self.dataset_versions)
        )
        object.__setattr__(
            self, "data_snapshot_ids", tuple(str(v) for v in self.data_snapshot_ids)
        )
        object.__setattr__(self, "lineage", dict(self.lineage))
        object.__setattr__(self, "quality", FeatureQuality(self.quality))
        if not self.digest_value:
            object.__setattr__(
                self,
                "digest_value",
                _digest(
                    {
                        "feature_set_id": self.feature_set_id,
                        "version": self.version,
                        "instrument_id": self.instrument_id,
                        "as_of": self.as_of.isoformat(),
                        "resolution": self.resolution,
                        "profile": str(self.profile),
                        "values": self.values,
                        "statuses": {k: str(v) for k, v in statuses.items()},
                        "reasons": self.reasons,
                        "imputed": self.imputed,
                        "timeframes": self.timeframes,
                        "dataset_versions": self.dataset_versions,
                        "data_snapshot_ids": self.data_snapshot_ids,
                        "lineage": self.lineage,
                    }
                ),
            )

    @property
    def available_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    @property
    def missing_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for name, status in self.statuses.items() if status is FeatureStatus.MISSING)
        )

    @property
    def unavailable_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name for name, status in self.statuses.items() if status is FeatureStatus.UNAVAILABLE
            )
        )

    @property
    def invalid_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for name, status in self.statuses.items() if status is FeatureStatus.INVALID)
        )

    def value(self, name: str, default: float | None = None) -> float | None:
        return self.values.get(name, default)

    def to_prediction_vector(self, *, include_imputed: bool = False) -> FeatureVector:
        allowed = {FeatureStatus.VALUE, FeatureStatus.ZERO}
        if include_imputed:
            allowed.add(FeatureStatus.IMPUTED)
        selected = {
            name: value
            for name, value in self.values.items()
            if self.statuses.get(name) in allowed
        }
        if not selected:
            raise FeatureValidationError(
                "no finite feature values are available for a prediction vector"
            )
        return FeatureVector(
            feature_set_id=self.feature_set_id,
            version=self.version,
            instrument_id=self.instrument_id,
            as_of=self.as_of,
            values=selected,
            data_snapshot_ids=self.data_snapshot_ids,
            lineage={
                "status_counts": json.dumps(self.status_counts(), sort_keys=True),
                "quality": str(self.quality),
                "digest": self.digest_value,
                "resolution": self.resolution,
                "profile": str(self.profile),
                "dataset_versions": "|".join(self.dataset_versions),
                **{str(k): str(v) for k, v in self.lineage.items()},
            },
            digest_value=self.digest_value,
        )

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for status in self.statuses.values():
            counts[str(status)] = counts.get(str(status), 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_set_id": self.feature_set_id,
            "version": self.version,
            "contract_version": FEATURE_CONTRACT_VERSION,
            "schema_version": FEATURE_SCHEMA_VERSION,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "as_of": self.as_of.isoformat(),
            "resolution": self.resolution,
            "profile": str(self.profile),
            "values": dict(sorted(self.values.items())),
            "statuses": {k: str(v) for k, v in sorted(self.statuses.items())},
            "reasons": dict(sorted(self.reasons.items())),
            "imputed": dict(sorted(self.imputed.items())),
            "feature_names": list(self.feature_names),
            "timeframes": {
                key: dict(sorted(row.items())) for key, row in sorted(self.timeframes.items())
            },
            "timeframe_statuses": {
                key: {n: str(v) for n, v in sorted(row.items())}
                for key, row in sorted(self.timeframe_statuses.items())
            },
            "source_first_at": self.source_first_at.isoformat() if self.source_first_at else None,
            "source_last_at": self.source_last_at.isoformat() if self.source_last_at else None,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "data_snapshot_ids": list(self.data_snapshot_ids),
            "dataset_versions": list(self.dataset_versions),
            "expected_count": self.expected_count,
            "completeness": self.completeness,
            "quality": str(self.quality),
            "lineage": dict(self.lineage),
            "digest_value": self.digest_value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FeatureSet":
        def parsed(value: Any) -> datetime | None:
            return datetime.fromisoformat(str(value)) if value else None

        return cls(
            feature_set_id=str(payload["feature_set_id"]),
            version=str(payload["version"]),
            instrument_id=str(payload["instrument_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            resolution=str(payload.get("resolution", "1d")),
            profile=FeatureProfileName(payload.get("profile", "swing")),
            values=dict(payload.get("values", {})),
            statuses=dict(payload.get("statuses", {})),
            reasons=dict(payload.get("reasons", {})),
            imputed=dict(payload.get("imputed", {})),
            exchange=str(payload.get("exchange", "")),
            feature_names=tuple(payload.get("feature_names", ())),
            timeframes=dict(payload.get("timeframes", {})),
            timeframe_statuses=dict(payload.get("timeframe_statuses", {})),
            source_first_at=parsed(payload.get("source_first_at")),
            source_last_at=parsed(payload.get("source_last_at")),
            retrieved_at=parsed(payload.get("retrieved_at")),
            generated_at=parsed(payload.get("generated_at")),
            data_snapshot_ids=tuple(payload.get("data_snapshot_ids", ())),
            dataset_versions=tuple(payload.get("dataset_versions", ())),
            expected_count=int(payload.get("expected_count", 0)),
            completeness=float(payload.get("completeness", 0.0)),
            quality=FeatureQuality(payload.get("quality", "partial")),
            lineage=dict(payload.get("lineage", {})),
            digest_value=str(payload.get("digest_value", "")),
        )


@dataclass(frozen=True, slots=True)
class RegimeTransition:
    previous: RegimeLabel
    current: RegimeLabel
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"previous": str(self.previous), "current": str(self.current), "changed": self.changed}


@dataclass(frozen=True, slots=True)
class RegimeResult:
    regime_id: str
    feature_set_id: str
    feature_set_version: str
    instrument_id: str
    as_of: datetime
    resolution: str
    primary: RegimeLabel
    secondary: tuple[RegimeLabel, ...] = ()
    confidence: float | None = None
    confidence_calibrated: bool = False
    supporting_features: tuple[str, ...] = ()
    evidence: Mapping[str, float] = field(default_factory=dict)
    feature_quality: FeatureQuality = FeatureQuality.PARTIAL
    data_quality: str = "partial"
    transition: RegimeTransition | None = None
    method: str = REGIME_METHOD_VERSION
    exchange: str = ""
    generated_at: datetime | None = None
    reasons: tuple[str, ...] = ()
    parameters: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _clean(self.regime_id) or not _clean(self.feature_set_id):
            raise FeatureValidationError("regime and feature-set identifiers are required")
        object.__setattr__(self, "instrument_id", _canonical_instrument(self.instrument_id))
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "primary", RegimeLabel(self.primary))
        object.__setattr__(
            self, "secondary", tuple(RegimeLabel(item) for item in self.secondary)
        )
        object.__setattr__(self, "feature_quality", FeatureQuality(self.feature_quality))
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise FeatureValidationError("regime confidence must be within [0, 1]")
        if self.confidence is not None and not self.confidence_calibrated:
            raise FeatureValidationError(
                "an uncalibrated detector must not report a regime confidence"
            )
        object.__setattr__(self, "evidence", {str(k): float(v) for k, v in self.evidence.items()})
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(
            self,
            "parameters",
            {str(key): float(value) for key, value in self.parameters.items()},
        )
        if self.generated_at is not None:
            object.__setattr__(self, "generated_at", _aware(self.generated_at, "generated_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_id": self.regime_id,
            "feature_set_id": self.feature_set_id,
            "feature_set_version": self.feature_set_version,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "as_of": self.as_of.isoformat(),
            "resolution": self.resolution,
            "primary_regime": str(self.primary),
            "secondary_conditions": [str(item) for item in self.secondary],
            "confidence": self.confidence,
            "confidence_calibrated": self.confidence_calibrated,
            "supporting_features": list(self.supporting_features),
            "evidence": dict(sorted(self.evidence.items())),
            "feature_quality": str(self.feature_quality),
            "data_quality": self.data_quality,
            "transition": self.transition.to_dict() if self.transition else None,
            "method": self.method,
            "method_version": self.method,
            "parameters": dict(sorted(self.parameters.items())),
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "reasons": list(self.reasons),
            "contract_version": FEATURE_CONTRACT_VERSION,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegimeResult":
        transition = payload.get("transition") or None
        return cls(
            regime_id=str(payload["regime_id"]),
            feature_set_id=str(payload["feature_set_id"]),
            feature_set_version=str(payload["feature_set_version"]),
            instrument_id=str(payload["instrument_id"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            resolution=str(payload.get("resolution", "1d")),
            primary=RegimeLabel(payload["primary_regime"]),
            secondary=tuple(payload.get("secondary_conditions", ())),
            confidence=payload.get("confidence"),
            confidence_calibrated=bool(payload.get("confidence_calibrated", False)),
            supporting_features=tuple(payload.get("supporting_features", ())),
            evidence=dict(payload.get("evidence", {})),
            feature_quality=FeatureQuality(payload.get("feature_quality", "partial")),
            data_quality=str(payload.get("data_quality", "partial")),
            transition=(
                RegimeTransition(
                    previous=RegimeLabel(transition["previous"]),
                    current=RegimeLabel(transition["current"]),
                    changed=bool(transition["changed"]),
                )
                if transition
                else None
            ),
            method=str(payload.get("method", REGIME_METHOD_VERSION)),
            exchange=str(payload.get("exchange", "")),
            generated_at=(
                datetime.fromisoformat(str(payload["generated_at"]))
                if payload.get("generated_at")
                else None
            ),
            reasons=tuple(payload.get("reasons", ())),
            parameters=dict(payload.get("parameters", {})),
        )


@dataclass(frozen=True, slots=True)
class FundamentalFeatureResult:
    values: Mapping[str, float]
    statuses: Mapping[str, FeatureStatus]
    reasons: Mapping[str, str]
    provenance: Mapping[str, Any]
    selected_snapshot_id: str = ""
    available_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "statuses": {k: str(v) for k, v in sorted(self.statuses.items())},
            "reasons": dict(sorted(self.reasons.items())),
            "provenance": dict(self.provenance),
            "selected_snapshot_id": self.selected_snapshot_id,
            "available_at": self.available_at.isoformat() if self.available_at else None,
        }


@dataclass(frozen=True, slots=True)
class NewsFeatureResult:
    values: Mapping[str, float]
    statuses: Mapping[str, FeatureStatus]
    reasons: Mapping[str, str]
    provenance: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "statuses": {k: str(v) for k, v in sorted(self.statuses.items())},
            "reasons": dict(sorted(self.reasons.items())),
            "provenance": [dict(row) for row in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class PreprocessingFit:
    preprocessing_id: str
    method: PreprocessingMethod
    feature_names: tuple[str, ...]
    parameters: Mapping[str, Any]
    training_digest: str
    version: str = PREPROCESSING_VERSION
    fitted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not _clean(self.preprocessing_id) or not _clean(self.training_digest):
            raise FeatureValidationError("preprocessing id and training digest are required")
        object.__setattr__(self, "method", PreprocessingMethod(self.method))
        if not self.parameters:
            raise FeatureValidationError("preprocessing fit needs parameters")
        if self.fitted_at is not None:
            object.__setattr__(self, "fitted_at", _aware(self.fitted_at, "fitted_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "preprocessing_id": self.preprocessing_id,
            "method": str(self.method),
            "feature_names": list(self.feature_names),
            "parameters": dict(self.parameters),
            "training_digest": self.training_digest,
            "version": self.version,
            "fitted_at": self.fitted_at.isoformat() if self.fitted_at else None,
        }


@dataclass(frozen=True, slots=True)
class TransformResult:
    values: Mapping[str, float]
    statuses: Mapping[str, FeatureStatus]
    imputed: Mapping[str, float]
    outliers: tuple[str, ...]
    completeness: float
    preprocessing_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "statuses": {k: str(v) for k, v in sorted(self.statuses.items())},
            "imputed": dict(sorted(self.imputed.items())),
            "outliers": list(self.outliers),
            "completeness": self.completeness,
            "preprocessing_id": self.preprocessing_id,
        }


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    feature: str
    issue: SelectionIssue
    detail: str
    retained: bool = True
    related_to: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "issue": str(self.issue),
            "detail": self.detail,
            "retained": self.retained,
            "related_to": self.related_to,
        }


@dataclass(frozen=True, slots=True)
class SelectionReport:
    report_id: str
    version: str
    feature_names: tuple[str, ...]
    decisions: tuple[SelectionDecision, ...]
    correlation_threshold: float
    missing_threshold: float
    observations: int
    leakage_safe: bool
    generated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "version": self.version,
            "feature_names": list(self.feature_names),
            "decisions": [item.to_dict() for item in self.decisions],
            "correlation_threshold": self.correlation_threshold,
            "missing_threshold": self.missing_threshold,
            "observations": self.observations,
            "leakage_safe": self.leakage_safe,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "automatic_deletion": False,
        }


@dataclass(frozen=True, slots=True)
class FeatureGenerationResult:
    feature_set: FeatureSet
    regime: RegimeResult
    persisted: bool = False
    duplicate: bool = False
    store_error: str = ""


__all__ = [
    "FEATURE_CONTRACT_VERSION",
    "FEATURE_SCHEMA_VERSION",
    "FEATURE_SET_VERSION",
    "PREPROCESSING_VERSION",
    "REGIME_METHOD_VERSION",
    "SELECTION_VERSION",
    "FeatureFamily",
    "FeatureGenerationResult",
    "FeatureProfileName",
    "FeatureQuality",
    "FeatureRequest",
    "FeatureSet",
    "FeatureSpec",
    "FeatureStatus",
    "FeatureValidationError",
    "FundamentalFeatureResult",
    "MissingPolicy",
    "NewsFeatureResult",
    "PreprocessingFit",
    "PreprocessingMethod",
    "RegimeLabel",
    "RegimeResult",
    "RegimeTransition",
    "SelectionDecision",
    "SelectionIssue",
    "SelectionReport",
    "TransformResult",
]
