"""PI-02 historical-market dataset contracts.

The dataset engine is an additive layer over the ONE market-data authority
(``MarketDataStore`` / ``MarketDataEngine``).  A dataset version never copies
price rows: it references the ``bar_id`` values already retained in
``market_bars`` and stores only the selection, coverage, quality and provenance
metadata needed to reproduce that exact view.

The contracts here deliberately keep four things separate that are easy to
conflate:

* the provider's observation (``OhlcvBar`` in ``marketdata.models``) and the
  dataset's *selection* of it (``DatasetMember``);
* raw provider prices and adjusted series (``price_basis`` plus
  ``CorporateAction`` records — an adjusted value is never written back as a
  market observation);
* a missing observation and a legitimate market closure (``GapKind``);
* dataset construction and leakage-safe training-dataset construction
  (``TrainingSample`` / ``TrainingDataset``).

A version is immutable once written.  A rebuild with different membership gets
a new version (or an explicit refusal when a caller pins a version label).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, cast

from novacontrol.novamarket.contracts import MarketDataError
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.prediction.models import (
    ForecastHorizon,
    HORIZON_SPECS,
    HistoricalMarketDataset,
)

PI02_PHASE = "PI-02"
DATASET_CONTRACT_VERSION = "prediction.dataset-engine.1.0"
DATASET_NORMALIZATION_VERSION = "novamarket.normalization.1"
TRAINING_CONTRACT_VERSION = "prediction.training-dataset.1.0"
PI02_SCHEMA_VERSION = 1

#: A build may cover at most this many instruments.  It mirrors the existing
#: twenty-instrument discovery batch cap rather than inventing a larger one.
MAX_INSTRUMENTS_PER_BUILD = 20
#: Ten years is the deepest declared free-provider history (Yahoo).
MAX_BUILD_RANGE = timedelta(days=3_660)
#: Defensive ceiling for a single instrument's membership.  1-minute bars for
#: ten years would be far larger than a free provider can supply; refusing at
#: the boundary keeps a malformed response from exhausting memory.
MAX_BARS_PER_INSTRUMENT = 250_000

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")


class DatasetError(MarketDataError):
    """Base class for a dataset-engine refusal."""


class DatasetValidationError(DatasetError, ValueError):
    """A dataset contract or request is malformed."""


class DatasetNotFoundError(DatasetError, LookupError):
    """The requested dataset version does not exist."""


class DatasetStateError(DatasetError):
    """An immutable version exists with different content."""


class DatasetLeakageError(DatasetError):
    """A training dataset failed a look-ahead/leakage assertion."""


class DatasetQuality(StrEnum):
    """Dataset-level quality; distinct from a single bar's ``DataQuality``."""

    VALID = "valid"
    VALID_WITH_WARNINGS = "valid_with_warnings"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"
    UNVERIFIED = "unverified"
    PROVIDER_CONFLICTED = "provider_conflicted"

    @property
    def usable_for_training(self) -> bool:
        return self in (
            DatasetQuality.VALID,
            DatasetQuality.VALID_WITH_WARNINGS,
        )


_QUALITY_SEVERITY: dict[DatasetQuality, int] = {
    DatasetQuality.VALID: 0,
    DatasetQuality.VALID_WITH_WARNINGS: 1,
    DatasetQuality.UNVERIFIED: 2,
    DatasetQuality.PROVIDER_CONFLICTED: 3,
    DatasetQuality.INCOMPLETE: 4,
    DatasetQuality.INVALID: 5,
}


def worst_dataset_quality(*values: DatasetQuality) -> DatasetQuality:
    return max(values, key=lambda item: _QUALITY_SEVERITY[item], default=DatasetQuality.VALID)


class GapKind(StrEnum):
    """Why an expected observation is absent.

    The kinds are not interchangeable.  A weekend is not a provider failure, a
    provider outage is not a trading halt, and an unverified calendar is not a
    holiday.
    """

    WEEKEND = "weekend"
    EXCHANGE_HOLIDAY = "exchange_holiday"
    MARKET_CLOSURE = "market_closure"
    TRADING_HALT = "trading_halt"
    SUSPENDED_INSTRUMENT = "suspended_instrument"
    NO_TRADE_INTERVAL = "no_trade_interval"
    MISSING_PROVIDER_OBSERVATION = "missing_provider_observation"
    PROVIDER_OUTAGE = "provider_outage"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    OUTSIDE_CALENDAR_COVERAGE = "outside_calendar_coverage"

    @property
    def explains_absence(self) -> bool:
        """Whether this kind is a market fact rather than a data defect."""
        return self in (
            GapKind.WEEKEND,
            GapKind.EXCHANGE_HOLIDAY,
            GapKind.MARKET_CLOSURE,
            GapKind.TRADING_HALT,
            GapKind.SUSPENDED_INSTRUMENT,
            GapKind.NO_TRADE_INTERVAL,
        )


class AnnotationKind(StrEnum):
    """A caller-supplied market fact used to classify missing observations."""

    TRADING_HALT = "trading_halt"
    SUSPENDED_INSTRUMENT = "suspended_instrument"
    MARKET_CLOSURE = "market_closure"
    NO_TRADE_INTERVAL = "no_trade_interval"
    PROVIDER_OUTAGE = "provider_outage"


_ANNOTATION_GAP: dict[AnnotationKind, GapKind] = {
    AnnotationKind.TRADING_HALT: GapKind.TRADING_HALT,
    AnnotationKind.SUSPENDED_INSTRUMENT: GapKind.SUSPENDED_INSTRUMENT,
    AnnotationKind.MARKET_CLOSURE: GapKind.MARKET_CLOSURE,
    AnnotationKind.NO_TRADE_INTERVAL: GapKind.NO_TRADE_INTERVAL,
    AnnotationKind.PROVIDER_OUTAGE: GapKind.PROVIDER_OUTAGE,
}


class AdjustmentBasis(StrEnum):
    """How a returned series relates to raw exchange prices."""

    RAW = "raw"
    PROVIDER_ADJUSTED = "provider_adjusted"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN_ADJUSTED = "total_return_adjusted"


class CorporateActionKind(StrEnum):
    """Historical corporate actions the engine can record and, if verified, apply."""

    SPLIT = "split"
    BONUS = "bonus"
    RIGHTS = "rights"
    DIVIDEND = "dividend"
    SYMBOL_CHANGE = "symbol_change"
    MERGER = "merger"
    DELISTING = "delisting"
    OTHER = "other"

    @property
    def price_relevant(self) -> bool:
        return self in (
            CorporateActionKind.SPLIT,
            CorporateActionKind.BONUS,
            CorporateActionKind.RIGHTS,
            CorporateActionKind.DIVIDEND,
        )


class SplitName(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


def _clean(value: object) -> str:
    return str(value or "").strip()


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DatasetValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DatasetValidationError(
            f"{field_name} must be timezone-aware; a naive timestamp cannot be "
            "ordered against market observations"
        )
    return value.astimezone(UTC)


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise DatasetValidationError(f"{field_name} must not be a boolean")
    if isinstance(value, float):
        raise DatasetValidationError(
            f"{field_name} must be Decimal/int/decimal-string; binary floats are refused"
        )
    try:
        result = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError) as exc:
        raise DatasetValidationError(f"{field_name} is not decimal: {value!r}") from exc
    if not result.is_finite():
        raise DatasetValidationError(f"{field_name} must be finite")
    return result


def _ratio(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise DatasetValidationError(f"{field_name} must be a number")
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise DatasetValidationError(f"{field_name} must be a number") from exc
    if not 0.0 <= number <= 1.0:
        raise DatasetValidationError(f"{field_name} must be between 0 and 1")
    return number


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class DatasetBuildRequest:
    """What to build; the canonical configuration is part of the version identity."""

    dataset_id: str
    instruments: tuple[str, ...]
    resolution: DataInterval | str
    start_at: datetime
    end_at: datetime
    exchange: str = ""
    providers: tuple[str, ...] = ()
    version: str = ""
    fetch: bool = True
    allow_partial: bool = False
    # A build requests a complete range by default.  Routine updates can opt
    # into fetching only from the latest stored observation so they do not
    # re-download years of history from a free provider.
    incremental: bool = False
    min_coverage_ratio: float = 0.9
    adjustment_basis: AdjustmentBasis | str = AdjustmentBasis.RAW
    description: str = ""
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        dataset_id = _clean(self.dataset_id).lower()
        if not _SLUG.match(dataset_id):
            raise DatasetValidationError(
                "dataset_id must be a 3..64 character slug such as 'nse-daily-core'"
            )
        raw_instruments = self.instruments
        if isinstance(raw_instruments, str) or not isinstance(raw_instruments, (tuple, list)):
            raise DatasetValidationError("instruments must be a list of instrument ids")
        instruments: list[str] = []
        seen: set[str] = set()
        for value in raw_instruments:
            instrument_id = _clean(value)
            if not _INSTRUMENT_ID.match(instrument_id):
                raise DatasetValidationError(
                    f"instrument {value!r} is not canonical (expected MIC:SYMBOL)"
                )
            if instrument_id in seen:
                raise DatasetValidationError(f"duplicate instrument: {instrument_id}")
            seen.add(instrument_id)
            instruments.append(instrument_id)
        if not instruments:
            raise DatasetValidationError("a dataset needs at least one instrument")
        if len(instruments) > MAX_INSTRUMENTS_PER_BUILD:
            raise DatasetValidationError(
                f"at most {MAX_INSTRUMENTS_PER_BUILD} instruments may be built at once"
            )
        try:
            resolution = DataInterval(self.resolution)
        except ValueError as exc:
            raise DatasetValidationError(f"unknown resolution: {self.resolution!r}") from exc
        if resolution is DataInterval.SPOT:
            raise DatasetValidationError("a dataset resolution cannot be SPOT")
        start = _aware(self.start_at, "start_at")
        end = _aware(self.end_at, "end_at")
        if end < start:
            raise DatasetValidationError("end_at precedes start_at")
        if end - start > MAX_BUILD_RANGE:
            raise DatasetValidationError(
                "a dataset build is limited to ten years (free-provider depth)"
            )
        version = _clean(self.version)
        if version and not _VERSION.match(version):
            raise DatasetValidationError(
                "version must be 1..32 alphanumeric/._- characters"
            )
        if isinstance(self.providers, str) or not isinstance(
            self.providers, (tuple, list)
        ):
            raise DatasetValidationError("providers must be a list of provider names")
        providers: list[str] = []
        for value in self.providers:
            name = _clean(value).lower()
            if not name:
                raise DatasetValidationError("provider names may not be blank")
            if name in providers:
                raise DatasetValidationError(f"duplicate provider: {name}")
            providers.append(name)
        if (
            not isinstance(self.fetch, bool)
            or not isinstance(self.allow_partial, bool)
            or not isinstance(self.incremental, bool)
        ):
            raise DatasetValidationError(
                "fetch, allow_partial and incremental must be booleans"
            )
        if isinstance(self.tags, str) or not isinstance(self.tags, (tuple, list)):
            raise DatasetValidationError("tags must be a list of strings")
        tags: list[str] = []
        for value in self.tags:
            tag = _clean(value)
            if tag and tag not in tags:
                tags.append(tag)
        object.__setattr__(self, "dataset_id", dataset_id)
        object.__setattr__(self, "instruments", tuple(instruments))
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        object.__setattr__(self, "exchange", _clean(self.exchange).upper())
        object.__setattr__(self, "providers", tuple(providers))
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "min_coverage_ratio", _ratio(self.min_coverage_ratio, "min_coverage_ratio"))
        try:
            basis = AdjustmentBasis(self.adjustment_basis)
        except ValueError as exc:
            raise DatasetValidationError(
                f"unknown adjustment basis: {self.adjustment_basis!r}"
            ) from exc
        object.__setattr__(self, "adjustment_basis", basis)
        object.__setattr__(self, "description", _clean(self.description))
        object.__setattr__(self, "tags", tuple(tags))

    def config(self) -> dict[str, Any]:
        """The reproducible part of the request (``fetch`` is operational)."""
        return {
            "dataset_id": self.dataset_id,
            "instruments": list(self.instruments),
            "exchange": self.exchange,
            "resolution": cast(DataInterval, self.resolution).value,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "providers": list(self.providers),
            "allow_partial": self.allow_partial,
            "min_coverage_ratio": self.min_coverage_ratio,
            "adjustment_basis": cast(AdjustmentBasis, self.adjustment_basis).value,
            "description": self.description,
            "tags": list(self.tags),
            "normalization_version": DATASET_NORMALIZATION_VERSION,
        }

    @property
    def config_digest(self) -> str:
        return _digest(self.config())


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Measured coverage, never an advertised promise."""

    requested_start: datetime
    requested_end: datetime
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    bar_count: int
    expected_bars: int | None
    covered_sessions: int
    expected_sessions: int | None
    coverage_ratio: float | None
    calendar_verified: bool
    coverage_limited: bool
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("requested_start", "requested_end"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        for name in ("first_observed_at", "last_observed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        if self.bar_count < 0:
            raise DatasetValidationError("bar_count must be non-negative")
        if self.expected_bars is not None and self.expected_bars < 0:
            raise DatasetValidationError("expected_bars must be non-negative")
        if self.coverage_ratio is not None and not 0.0 <= self.coverage_ratio <= 1.0:
            raise DatasetValidationError("coverage_ratio must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "first_observed_at": (
                self.first_observed_at.isoformat() if self.first_observed_at else None
            ),
            "last_observed_at": (
                self.last_observed_at.isoformat() if self.last_observed_at else None
            ),
            "bar_count": self.bar_count,
            "expected_bars": self.expected_bars,
            "covered_sessions": self.covered_sessions,
            "expected_sessions": self.expected_sessions,
            "coverage_ratio": (
                round(self.coverage_ratio, 6)
                if self.coverage_ratio is not None
                else None
            ),
            "calendar_verified": self.calendar_verified,
            "coverage_limited": self.coverage_limited,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """Counts and reasons from validating one instrument's observations."""

    bars_considered: int = 0
    bars_selected: int = 0
    bars_excluded_invalid: int = 0
    bars_excluded_suspect: int = 0
    bars_excluded_partial: int = 0
    bars_selected_partial: int = 0
    bars_missing_volume: int = 0
    duplicate_timestamps: int = 0
    out_of_order: int = 0
    boundary_misaligned: int = 0
    non_trading_day_observations: int = 0
    provider_conflicts: int = 0
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "bars_considered": self.bars_considered,
            "bars_selected": self.bars_selected,
            "bars_excluded_invalid": self.bars_excluded_invalid,
            "bars_excluded_suspect": self.bars_excluded_suspect,
            "bars_excluded_partial": self.bars_excluded_partial,
            "bars_selected_partial": self.bars_selected_partial,
            "bars_missing_volume": self.bars_missing_volume,
            "duplicate_timestamps": self.duplicate_timestamps,
            "out_of_order": self.out_of_order,
            "boundary_misaligned": self.boundary_misaligned,
            "non_trading_day_observations": self.non_trading_day_observations,
            "provider_conflicts": self.provider_conflicts,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class GapRecord:
    """One classified region of absent expected observations."""

    instrument_id: str
    kind: GapKind
    start_at: datetime
    end_at: datetime
    count: int
    detail: str = ""
    verified: bool = True

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise DatasetValidationError("gap instrument_id is not canonical")
        start = _aware(self.start_at, "start_at")
        end = _aware(self.end_at, "end_at")
        if end < start:
            raise DatasetValidationError("a gap ends before it starts")
        if self.count < 1:
            raise DatasetValidationError("a gap must cover at least one observation")
        object.__setattr__(self, "kind", GapKind(self.kind))
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "kind": self.kind.value,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "count": self.count,
            "detail": self.detail,
            "verified": self.verified,
            "explains_absence": self.kind.explains_absence,
        }


@dataclass(frozen=True, slots=True)
class MissingSummary:
    """Missing-data accounting; never a forward-filled price series."""

    total_missing: int = 0
    by_kind: Mapping[str, int] = field(default_factory=dict)
    gaps: tuple[GapRecord, ...] = ()
    forward_filled: bool = False
    imputed: bool = False
    imputation_method: str = ""

    def __post_init__(self) -> None:
        if self.forward_filled or self.imputed:
            raise DatasetValidationError(
                "PI-02 never forward-fills or imputes trusted observations; "
                "specify an explicit model-specific imputation in a later phase"
            )
        cleaned: dict[str, int] = {}
        for kind, count in self.by_kind.items():
            if int(count) > 0:
                cleaned[str(kind)] = int(count)
        object.__setattr__(self, "by_kind", cleaned)
        object.__setattr__(self, "gaps", tuple(self.gaps))

    def to_dict(self, *, include_gaps: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "total_missing": self.total_missing,
            "by_kind": dict(self.by_kind),
            "forward_filled": False,
            "imputed": False,
            "imputation_method": "",
        }
        if include_gaps:
            payload["gaps"] = [gap.to_dict() for gap in self.gaps]
        return payload


@dataclass(frozen=True, slots=True)
class DatasetSegment:
    """One instrument's membership summary inside a dataset version."""

    instrument_id: str
    exchange: str
    mic: str
    resolution: DataInterval
    bar_count: int
    first_at: datetime | None
    last_at: datetime | None
    providers: tuple[str, ...]
    quality: DatasetQuality
    coverage: CoverageSummary
    validation: ValidationSummary
    missing: MissingSummary
    membership_digest: str
    exchange_timezone: str = ""
    currency: str = ""
    volume_unit: str = "provider"

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise DatasetValidationError("segment instrument_id is not canonical")
        if self.bar_count < 0:
            raise DatasetValidationError("segment bar_count must be non-negative")
        for name in ("first_at", "last_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        object.__setattr__(self, "resolution", DataInterval(self.resolution))
        object.__setattr__(self, "quality", DatasetQuality(self.quality))
        object.__setattr__(self, "providers", tuple(sorted(set(self.providers))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "mic": self.mic,
            "exchange_timezone": self.exchange_timezone,
            "currency": self.currency,
            "volume_unit": self.volume_unit,
            "resolution": self.resolution.value,
            "bar_count": self.bar_count,
            "first_at": self.first_at.isoformat() if self.first_at else None,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "providers": list(self.providers),
            "quality": self.quality.value,
            "coverage": self.coverage.to_dict(),
            "validation": self.validation.to_dict(),
            "missing": self.missing.to_dict(),
            "membership_digest": self.membership_digest,
        }


@dataclass(frozen=True, slots=True)
class DatasetMember:
    """An exact reference to one stored bar; no price bytes are duplicated."""

    dataset_id: str
    version: str
    instrument_id: str
    exchange: str
    ordinal: int
    bar_id: str
    provider: str
    observed_at: datetime
    quality: DataQuality

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise DatasetValidationError("member ordinal must be non-negative")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        object.__setattr__(self, "quality", DataQuality(self.quality))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "ordinal": self.ordinal,
            "bar_id": self.bar_id,
            "provider": self.provider,
            "observed_at": self.observed_at.isoformat(),
            "quality": self.quality.value,
        }


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    """Why one observation won when providers disagreed at a timestamp."""

    instrument_id: str
    observed_at: datetime
    selected_bar_id: str
    selected_provider: str
    alternatives: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "observed_at": self.observed_at.isoformat(),
            "selected_bar_id": self.selected_bar_id,
            "selected_provider": self.selected_provider,
            "alternatives": list(self.alternatives),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """Immutable dataset metadata; the public, reproducible dataset identity."""

    dataset_id: str
    version: str
    created_at: datetime
    resolution: DataInterval
    start_at: datetime
    end_at: datetime
    instruments: tuple[str, ...]
    exchanges: tuple[str, ...]
    providers: tuple[str, ...]
    normalization_version: str
    price_basis: AdjustmentBasis
    quality: DatasetQuality
    complete: bool
    coverage: CoverageSummary
    validation: ValidationSummary
    missing: MissingSummary
    segments: tuple[DatasetSegment, ...]
    config: Mapping[str, Any]
    acquisition: tuple[Mapping[str, Any], ...] = ()
    corporate_actions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    content_digest: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.dataset_id) or not _clean(self.version):
            raise DatasetValidationError("dataset id and version are required")
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "resolution", DataInterval(self.resolution))
        object.__setattr__(self, "start_at", _aware(self.start_at, "start_at"))
        object.__setattr__(self, "end_at", _aware(self.end_at, "end_at"))
        object.__setattr__(self, "quality", DatasetQuality(self.quality))
        object.__setattr__(self, "price_basis", AdjustmentBasis(self.price_basis))
        object.__setattr__(self, "instruments", tuple(self.instruments))
        object.__setattr__(self, "exchanges", tuple(sorted(set(self.exchanges))))
        object.__setattr__(self, "providers", tuple(sorted(set(self.providers))))
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "acquisition", tuple(dict(item) for item in self.acquisition))
        object.__setattr__(self, "corporate_actions", tuple(self.corporate_actions))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        if not self.content_digest:
            object.__setattr__(self, "content_digest", self._compute_digest())

    def _compute_digest(self) -> str:
        return _digest(
            {
                "dataset_id": self.dataset_id,
                "resolution": self.resolution.value,
                "start_at": self.start_at.isoformat(),
                "end_at": self.end_at.isoformat(),
                "normalization_version": self.normalization_version,
                "price_basis": self.price_basis.value,
                "quality": self.quality.value,
                "complete": self.complete,
                "coverage": self.coverage.to_dict(),
                "validation": self.validation.to_dict(),
                "config": dict(self.config),
                "segments": [
                    {
                        "instrument_id": segment.instrument_id,
                        "exchange": segment.exchange,
                        "mic": segment.mic,
                        "currency": segment.currency,
                        "exchange_timezone": segment.exchange_timezone,
                        "volume_unit": segment.volume_unit,
                        "bar_count": segment.bar_count,
                        "quality": segment.quality.value,
                        "membership_digest": segment.membership_digest,
                        "first_at": segment.first_at.isoformat() if segment.first_at else None,
                        "last_at": segment.last_at.isoformat() if segment.last_at else None,
                    }
                    for segment in self.segments
                ],
                "missing": self.missing.to_dict(include_gaps=False),
                "corporate_actions": list(self.corporate_actions),
            }
        )

    @property
    def bar_count(self) -> int:
        return sum(segment.bar_count for segment in self.segments)

    def segment(self, instrument_id: str) -> DatasetSegment | None:
        return next(
            (item for item in self.segments if item.instrument_id == instrument_id),
            None,
        )

    def to_dict(self, *, include_segments: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "resolution": self.resolution.value,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "instruments": list(self.instruments),
            "exchanges": list(self.exchanges),
            "providers": list(self.providers),
            "normalization_version": self.normalization_version,
            "price_basis": self.price_basis.value,
            "quality": self.quality.value,
            "usable_for_training": self.quality.usable_for_training,
            "complete": self.complete,
            "bar_count": self.bar_count,
            "coverage": self.coverage.to_dict(),
            "validation": self.validation.to_dict(),
            "missing": self.missing.to_dict(),
            "acquisition": [dict(item) for item in self.acquisition],
            "corporate_actions": list(self.corporate_actions),
            "limitations": list(self.limitations),
            "config": dict(self.config),
            "content_digest": self.content_digest,
            "contract_version": DATASET_CONTRACT_VERSION,
            "phase": PI02_PHASE,
        }
        if include_segments:
            payload["segments"] = [segment.to_dict() for segment in self.segments]
        return payload

    def as_prediction_datasets(self) -> tuple[HistoricalMarketDataset, ...]:
        """Map each instrument segment onto the PI-01 reference contract."""
        result: list[HistoricalMarketDataset] = []
        for segment in self.segments:
            if segment.bar_count == 0 or segment.first_at is None or segment.last_at is None:
                continue
            result.append(
                HistoricalMarketDataset(
                    dataset_id=self.dataset_id,
                    version=self.version,
                    instrument_id=segment.instrument_id,
                    resolution=self.resolution,
                    start_at=segment.first_at,
                    end_at=segment.last_at,
                    bar_count=segment.bar_count,
                    source_snapshot_ids=(f"dataset:{self.dataset_id}:{self.version}",),
                    quality=DataQuality.WARNING
                    if segment.quality is DatasetQuality.VALID_WITH_WARNINGS
                    else DataQuality.VALID
                    if segment.quality is DatasetQuality.VALID
                    else DataQuality.SUSPECT
                    if segment.quality is DatasetQuality.PROVIDER_CONFLICTED
                    else DataQuality.INVALID
                    if segment.quality is DatasetQuality.INVALID
                    else DataQuality.UNAVAILABLE,
                    provenance={
                        "dataset_version": self.version,
                        "content_digest": self.content_digest,
                        "normalization_version": self.normalization_version,
                        "price_basis": self.price_basis.value,
                        "exchange": segment.exchange,
                        "currency": segment.currency,
                        "exchange_timezone": segment.exchange_timezone,
                        "volume_unit": segment.volume_unit,
                        "providers": list(segment.providers),
                        "coverage_ratio": segment.coverage.coverage_ratio,
                    },
                )
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class MemberPage:
    items: tuple[DatasetMember, ...]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.has_more,
            "next_offset": self.offset + len(self.items) if self.has_more else None,
        }


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """A corporate action with explicit source, verification and price factor.

    ``price_factor`` multiplies prices observed strictly BEFORE ``effective_at``
    (a 2-for-1 split is 0.5).  It is applied only when ``verified`` and the
    factor is present; otherwise the action is recorded as a limitation and no
    adjusted price is produced.  The engine never derives a factor from an
    unexplained price jump.
    """

    instrument_id: str
    kind: CorporateActionKind | str
    effective_at: datetime
    announced_at: datetime | None
    source: str
    verified: bool = False
    price_factor: Decimal | None = None
    amount: Decimal | None = None
    currency: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    action_id: str = ""

    def __post_init__(self) -> None:
        instrument_id = _clean(self.instrument_id)
        if not _INSTRUMENT_ID.match(instrument_id):
            raise DatasetValidationError("corporate action instrument_id is not canonical")
        try:
            kind = CorporateActionKind(self.kind)
        except ValueError as exc:
            raise DatasetValidationError(
                f"unknown corporate action kind: {self.kind!r}"
            ) from exc
        effective = _aware(self.effective_at, "effective_at")
        announced = (
            _aware(self.announced_at, "announced_at")
            if self.announced_at is not None
            else None
        )
        factor = (
            _decimal(self.price_factor, "price_factor")
            if self.price_factor is not None
            else None
        )
        if factor is not None and factor <= 0:
            raise DatasetValidationError("price_factor must be positive")
        amount = _decimal(self.amount, "amount") if self.amount is not None else None
        if amount is not None and amount < 0:
            raise DatasetValidationError("amount must not be negative")
        source = _clean(self.source)
        if not source:
            raise DatasetValidationError("a corporate action needs a source")
        if not isinstance(self.verified, bool):
            raise DatasetValidationError("verified must be a boolean")
        action_id = _clean(self.action_id) or "ca_" + _digest(
            {
                "instrument_id": instrument_id,
                "kind": kind.value,
                "effective_at": effective.isoformat(),
                "source": source.lower(),
            }
        )
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "announced_at", announced)
        object.__setattr__(self, "price_factor", factor)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", _clean(self.currency).upper())
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "action_id", action_id)

    @property
    def applicable(self) -> bool:
        kind = cast(CorporateActionKind, self.kind)
        return self.verified and self.price_factor is not None and kind.price_relevant

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "instrument_id": self.instrument_id,
            "kind": cast(CorporateActionKind, self.kind).value,
            "effective_at": self.effective_at.isoformat(),
            "announced_at": (
                self.announced_at.isoformat() if self.announced_at else None
            ),
            "source": self.source,
            "verified": self.verified,
            "applicable": self.applicable,
            "price_factor": (
                format(self.price_factor, "f") if self.price_factor is not None else None
            ),
            "amount": format(self.amount, "f") if self.amount is not None else None,
            "currency": self.currency,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MarketAnnotation:
    """A caller-supplied, timestamped market fact used for gap classification."""

    instrument_id: str
    kind: AnnotationKind | str
    start_at: datetime
    end_at: datetime
    source: str
    verified: bool = False
    detail: str = ""
    annotation_id: str = ""

    def __post_init__(self) -> None:
        instrument_id = _clean(self.instrument_id)
        if not _INSTRUMENT_ID.match(instrument_id):
            raise DatasetValidationError("annotation instrument_id is not canonical")
        try:
            kind = AnnotationKind(self.kind)
        except ValueError as exc:
            raise DatasetValidationError(f"unknown annotation kind: {self.kind!r}") from exc
        start = _aware(self.start_at, "start_at")
        end = _aware(self.end_at, "end_at")
        if end < start:
            raise DatasetValidationError("an annotation ends before it starts")
        source = _clean(self.source)
        if not source:
            raise DatasetValidationError("an annotation needs a source")
        annotation_id = _clean(self.annotation_id) or "an_" + _digest(
            {
                "instrument_id": instrument_id,
                "kind": kind.value,
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "source": source.lower(),
            }
        )
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "detail", _clean(self.detail))
        object.__setattr__(self, "annotation_id", annotation_id)

    @property
    def gap_kind(self) -> GapKind:
        return _ANNOTATION_GAP[cast(AnnotationKind, self.kind)]

    def covers(self, start: datetime, end: datetime) -> bool:
        return self.start_at <= end and start <= self.end_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "instrument_id": self.instrument_id,
            "kind": cast(AnnotationKind, self.kind).value,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "source": self.source,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """One leakage-checked (features -> target) training example."""

    instrument_id: str
    split: SplitName | str
    horizon: ForecastHorizon | str
    decision_at: datetime
    target_at: datetime
    decision_bar_id: str
    target_bar_id: str
    window_start_at: datetime
    window_end_at: datetime
    decision_close: Decimal
    target_close: Decimal
    window_bar_ids: tuple[str, ...]
    target_return: Decimal | None = None
    digest_value: str = ""

    def __post_init__(self) -> None:
        if not _INSTRUMENT_ID.match(_clean(self.instrument_id)):
            raise DatasetValidationError("training sample instrument_id is not canonical")
        try:
            split = SplitName(self.split)
        except ValueError as exc:
            raise DatasetValidationError(f"unknown split: {self.split!r}") from exc
        try:
            horizon = ForecastHorizon(self.horizon)
        except ValueError as exc:
            raise DatasetValidationError(f"unknown horizon: {self.horizon!r}") from exc
        decision = _aware(self.decision_at, "decision_at")
        target = _aware(self.target_at, "target_at")
        window_start = _aware(self.window_start_at, "window_start_at")
        window_end = _aware(self.window_end_at, "window_end_at")
        if target <= decision:
            raise DatasetValidationError(
                "a training target must occur strictly after the decision timestamp"
            )
        if window_end > decision:
            raise DatasetValidationError(
                "training features must occur at or before the decision timestamp"
            )
        if window_start > window_end:
            raise DatasetValidationError("the feature window starts after it ends")
        if not self.decision_bar_id or not self.target_bar_id:
            raise DatasetValidationError("a sample must reference decision and target bars")
        if not self.window_bar_ids:
            raise DatasetValidationError("a sample must reference its feature window bars")
        close = _decimal(self.decision_close, "decision_close")
        target_close = _decimal(self.target_close, "target_close")
        if close <= 0 or target_close <= 0:
            raise DatasetValidationError("training sample prices must be positive")
        computed = (target_close / close) - Decimal(1)
        if self.target_return is not None:
            provided = _decimal(self.target_return, "target_return")
            if abs(provided - computed) > Decimal("0.0000000001"):
                raise DatasetValidationError(
                    "target_return does not match target_close/decision_close"
                )
        digest_value = _clean(self.digest_value) or _digest(
            {
                "instrument_id": self.instrument_id,
                "split": split.value,
                "horizon": horizon.value,
                "decision_at": decision.isoformat(),
                "target_at": target.isoformat(),
                "decision_bar_id": self.decision_bar_id,
                "target_bar_id": self.target_bar_id,
                "window_start_at": window_start.isoformat(),
                "window_end_at": window_end.isoformat(),
                "window_bar_ids": list(self.window_bar_ids),
                "decision_close": format(close, "f"),
                "target_close": format(target_close, "f"),
            }
        )
        object.__setattr__(self, "split", split)
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "target_at", target)
        object.__setattr__(self, "window_start_at", window_start)
        object.__setattr__(self, "window_end_at", window_end)
        object.__setattr__(self, "decision_close", close)
        object.__setattr__(self, "target_close", target_close)
        object.__setattr__(self, "window_bar_ids", tuple(self.window_bar_ids))
        object.__setattr__(self, "target_return", computed)
        object.__setattr__(self, "digest_value", digest_value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "split": cast(SplitName, self.split).value,
            "horizon": cast(ForecastHorizon, self.horizon).value,
            "decision_at": self.decision_at.isoformat(),
            "target_at": self.target_at.isoformat(),
            "decision_bar_id": self.decision_bar_id,
            "target_bar_id": self.target_bar_id,
            "window_start_at": self.window_start_at.isoformat(),
            "window_end_at": self.window_end_at.isoformat(),
            "window_bar_ids": list(self.window_bar_ids),
            "decision_close": format(self.decision_close, "f"),
            "target_close": format(self.target_close, "f"),
            "target_return": format(self.target_return or Decimal(0), "f"),
            "digest": self.digest_value,
            "features_at_or_before_decision": True,
            "target_after_decision": True,
        }


@dataclass(frozen=True, slots=True)
class SplitSummary:
    split: SplitName
    start_at: datetime | None
    end_at: datetime | None
    sample_count: int
    instruments: tuple[str, ...] = ()
    purged_target_overlaps: int = 0
    digest_value: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "split", SplitName(self.split))
        for name in ("start_at", "end_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        if self.sample_count < 0:
            raise DatasetValidationError("split sample_count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "split": self.split.value,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "sample_count": self.sample_count,
            "instruments": list(self.instruments),
            "purged_target_overlaps": self.purged_target_overlaps,
            "digest": self.digest_value,
        }


@dataclass(frozen=True, slots=True)
class TrainingDataset:
    """A chronological, leakage-checked training set over one dataset version."""

    training_id: str
    dataset_id: str
    dataset_version: str
    horizon: ForecastHorizon
    resolution: DataInterval
    created_at: datetime
    window_bars: int
    splits: Mapping[SplitName, SplitSummary]
    sample_count: int
    leakage_checks: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    content_digest: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.training_id) or not _clean(self.dataset_id):
            raise DatasetValidationError("training dataset id and dataset id are required")
        object.__setattr__(self, "horizon", ForecastHorizon(self.horizon))
        object.__setattr__(self, "resolution", DataInterval(self.resolution))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        if self.window_bars < 2:
            raise DatasetValidationError("window_bars must be at least 2")
        if self.sample_count < 0:
            raise DatasetValidationError("sample_count must be non-negative")
        object.__setattr__(self, "splits", dict(self.splits))
        object.__setattr__(self, "leakage_checks", tuple(self.leakage_checks))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        if not self.content_digest:
            object.__setattr__(
                self,
                "content_digest",
                _digest(
                    {
                        "training_id": self.training_id,
                        "dataset_id": self.dataset_id,
                        "dataset_version": self.dataset_version,
                        "horizon": self.horizon.value,
                        "resolution": self.resolution.value,
                        "window_bars": self.window_bars,
                        "splits": {
                            split.value: summary.digest_value
                            for split, summary in self.splits.items()
                        },
                        "sample_count": self.sample_count,
                    }
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "training_id": self.training_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "horizon": self.horizon.value,
            "resolution": self.resolution.value,
            "created_at": self.created_at.isoformat(),
            "window_bars": self.window_bars,
            "sample_count": self.sample_count,
            "splits": {
                split.value: summary.to_dict() for split, summary in self.splits.items()
            },
            "leakage_checks": list(self.leakage_checks),
            "limitations": list(self.limitations),
            "content_digest": self.content_digest,
            "contract_version": TRAINING_CONTRACT_VERSION,
            "chronological": True,
            "test_used_for_training": False,
        }

    def split(self, name: SplitName | str) -> SplitSummary | None:
        try:
            return self.splits.get(SplitName(name))
        except ValueError:
            return None


def identity_for(instrument: Instrument) -> dict[str, str]:
    """The canonical identity fields a dataset segment always carries."""
    return {
        "instrument_id": instrument.instrument_id,
        "exchange": instrument.exchange,
        "mic": instrument.mic,
        "currency": instrument.currency,
        "timezone": instrument.timezone,
    }


def membership_digest(bars: Sequence[OhlcvBar]) -> str:
    """Digest of the exact selected observations, in chronological order."""
    return _digest(
        {
            "bars": [
                {
                    "bar_id": bar.bar_id,
                    "provider": bar.provider,
                    "observed_at": bar.observed_at.isoformat(),
                    "open": format(bar.open, "f"),
                    "high": format(bar.high, "f"),
                    "low": format(bar.low, "f"),
                    "close": format(bar.close, "f"),
                    "volume": None if bar.volume is None else format(bar.volume, "f"),
                    "quality": bar.quality.value,
                    "partial": bar.partial,
                }
                for bar in bars
            ]
        }
    )


def interval_duration(resolution: DataInterval | str) -> timedelta:
    """The nominal duration of one bar interval."""
    interval = DataInterval(resolution)
    return {
        DataInterval.MINUTE_1: timedelta(minutes=1),
        DataInterval.MINUTE_5: timedelta(minutes=5),
        DataInterval.MINUTE_15: timedelta(minutes=15),
        DataInterval.MINUTE_30: timedelta(minutes=30),
        DataInterval.HOUR_1: timedelta(hours=1),
        DataInterval.DAY_1: timedelta(days=1),
        DataInterval.WEEK_1: timedelta(days=7),
        DataInterval.MONTH_1: timedelta(days=30),
    }[interval]


def resolution_rank(resolution: DataInterval | str) -> int:
    """Coarse-to-fine rank; shared with the PI-01 horizon compatibility rules."""
    return {
        DataInterval.MONTH_1: 8,
        DataInterval.WEEK_1: 7,
        DataInterval.DAY_1: 6,
        DataInterval.HOUR_1: 5,
        DataInterval.MINUTE_30: 4,
        DataInterval.MINUTE_15: 3,
        DataInterval.MINUTE_5: 2,
        DataInterval.MINUTE_1: 1,
        DataInterval.SPOT: 0,
    }[DataInterval(resolution)]


def horizon_supports(horizon: ForecastHorizon | str, resolution: DataInterval | str) -> bool:
    spec = HORIZON_SPECS[ForecastHorizon(horizon)]
    return spec.accepts_resolution(resolution)


__all__ = [
    "AdjustmentBasis",
    "AnnotationKind",
    "CorporateAction",
    "CorporateActionKind",
    "CoverageSummary",
    "DATASET_CONTRACT_VERSION",
    "DATASET_NORMALIZATION_VERSION",
    "DatasetBuildRequest",
    "DatasetError",
    "DatasetLeakageError",
    "DatasetMember",
    "DatasetNotFoundError",
    "DatasetQuality",
    "DatasetSegment",
    "DatasetStateError",
    "DatasetValidationError",
    "DatasetVersion",
    "GapKind",
    "GapRecord",
    "MAX_BARS_PER_INSTRUMENT",
    "MAX_BUILD_RANGE",
    "MAX_INSTRUMENTS_PER_BUILD",
    "MarketAnnotation",
    "MemberPage",
    "MissingSummary",
    "PI02_PHASE",
    "PI02_SCHEMA_VERSION",
    "SelectionRecord",
    "SplitName",
    "SplitSummary",
    "TRAINING_CONTRACT_VERSION",
    "TrainingDataset",
    "TrainingSample",
    "ValidationSummary",
    "horizon_supports",
    "identity_for",
    "interval_duration",
    "membership_digest",
    "resolution_rank",
    "worst_dataset_quality",
]
