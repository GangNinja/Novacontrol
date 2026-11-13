"""Independent validation, anomaly flagging and multi-provider reconciliation.

Three principles shape this module:

* **Validation is independent of a provider.** An adapter may parse a response
  perfectly and still return a price that fails here. The engine never treats
  "the adapter returned something" as "the data is valid".
* **An anomaly is flagged, not deleted.** A large move or volume spike is
  retained with its reason and a quality class. The engine decides whether the
  observation is usable; the store keeps every observation regardless.
* **Disagreement is never averaged.** When two providers disagree beyond the
  configured tolerance, the reconciled result is a CONFLICT with no selected
  price. Averaging two different measurements would invent a third one.

Structural checks (negative prices, high < low, future timestamps) live in the
models' constructors so no code path can bypass them. This module adds the
cross-record checks: jump, volume anomaly, stale quality and disagreement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any, ClassVar

from novacontrol.novamarket.contracts import ConfigurationError
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
    PriceObservation,
    ReconciliationResult,
    ReconciliationStatus,
    worst_quality,
)


@dataclass(frozen=True, slots=True)
class QualitySettings:
    """Configurable anomaly thresholds.

    These are ratios, not price levels: a 20% move warns, a 50% move is suspect
    and needs evaluation. They are deliberately not "reject anything unusual" —
    a genuine market movement must survive, with a warning attached.
    """

    jump_warning_ratio: float = 0.20
    jump_suspect_ratio: float = 0.50
    volume_warning_multiple: float = 10.0
    volume_suspect_multiple: float = 50.0
    reconciliation_tolerance_ratio: float = 0.005
    reconciliation_window_seconds: int = 300
    volume_median_window: int = 20

    # Class-level table: building a default instance here recursed forever.
    LIMITS: ClassVar[Mapping[str, tuple[float, float]]] = {
        "jump_warning_ratio": (0.0, 10.0),
        "jump_suspect_ratio": (0.0, 100.0),
        "volume_warning_multiple": (1.0, 10_000.0),
        "volume_suspect_multiple": (1.0, 1_000_000.0),
        "reconciliation_tolerance_ratio": (0.0, 1.0),
        "reconciliation_window_seconds": (1, 86_400),
        "volume_median_window": (3, 500),
    }

    def __post_init__(self) -> None:
        for name, (low, high) in self.LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigurationError(f"quality.{name} must be a number")
            if not low <= float(value) <= high:
                raise ConfigurationError(
                    f"quality.{name} must be between {low} and {high}, got {value}"
                )
        if self.jump_suspect_ratio < self.jump_warning_ratio:
            raise ConfigurationError(
                "quality.jump_suspect_ratio must be >= jump_warning_ratio"
            )
        if self.volume_suspect_multiple < self.volume_warning_multiple:
            raise ConfigurationError(
                "quality.volume_suspect_multiple must be >= volume_warning_multiple"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> QualitySettings:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigurationError("quality must be an object")
        defaults = cls()
        allowed = set(cls.LIMITS)
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigurationError(
                f"unknown quality field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        return cls(
            **{
                name: data.get(name, getattr(defaults, name))
                for name in allowed
            }
        )

    def to_dict(self) -> dict[str, float | int]:
        return {name: getattr(self, name) for name in self.LIMITS}

    @staticmethod
    def schema() -> dict[str, Any]:
        return {
            "fields": {
                name: {"min": low, "max": high}
                for name, (low, high) in QualitySettings.LIMITS.items()
            },
            "rules": (
                "A move >= jump_warning_ratio is WARNING; >= jump_suspect_ratio is "
                "SUSPECT and is not used automatically. Volume >= "
                "volume_warning_multiple of the recent median is WARNING; >= "
                "volume_suspect_multiple is SUSPECT. Reconciliation tolerance is "
                "relative; disagreement beyond it is CONFLICT, never an average."
            ),
        }


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """A quality verdict and the flags behind it."""

    quality: DataQuality
    reasons: tuple[str, ...]
    flags: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.quality in (
            DataQuality.VALID,
            DataQuality.WARNING,
            DataQuality.STALE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality.value,
            "usable": self.usable,
            "reasons": list(self.reasons),
            "flags": list(self.flags),
        }


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def assess_quote(
    observation: PriceObservation,
    *,
    previous: PriceObservation | None,
    freshness: Freshness | None,
    settings: QualitySettings | None = None,
) -> QualityAssessment:
    """Cross-record checks for a spot observation.

    ``previous`` is the newest earlier usable observation for the same
    instrument and currency. A jump is measured against it only when the two are
    in the same currency; comparing INR with USD would manufacture an anomaly.
    """
    policy = settings or QualitySettings()
    quality = observation.quality
    reasons = list(observation.reasons)
    flags: list[str] = []

    if quality in (DataQuality.INVALID, DataQuality.UNAVAILABLE):
        return QualityAssessment(quality, _unique(reasons), ("structural",))

    if freshness is Freshness.STALE:
        quality = worst_quality(quality, DataQuality.STALE)
        reasons.append("observation is stale for its exchange and interval")
        flags.append("stale")

    if (
        previous is not None
        and previous.usable
        and previous.currency == observation.currency
        and previous.price > 0
        and observation.observed_at > previous.observed_at
    ):
        ratio = abs(observation.price - previous.price) / previous.price
        percentage = float(ratio) * 100.0
        if ratio >= Decimal(str(policy.jump_suspect_ratio)):
            quality = worst_quality(quality, DataQuality.SUSPECT)
            reasons.append(
                f"price moved {percentage:.2f}% from the previous observation "
                f"({previous.price} -> {observation.price} {observation.currency}); "
                "flagged for evaluation, not used automatically"
            )
            flags.append("price_jump_suspect")
        elif ratio >= Decimal(str(policy.jump_warning_ratio)):
            quality = worst_quality(quality, DataQuality.WARNING)
            reasons.append(
                f"price moved {percentage:.2f}% from the previous observation "
                f"({previous.price} -> {observation.price} {observation.currency})"
            )
            flags.append("price_jump_warning")

    return QualityAssessment(quality, _unique(reasons), _unique(flags))


def assess_bar(
    bar: OhlcvBar,
    *,
    previous_bars: Sequence[OhlcvBar] = (),
    freshness: Freshness | None,
    settings: QualitySettings | None = None,
) -> QualityAssessment:
    """Cross-record checks for an OHLCV bar."""
    policy = settings or QualitySettings()
    quality = bar.quality
    reasons = list(bar.reasons)
    flags: list[str] = []

    if quality in (DataQuality.INVALID, DataQuality.UNAVAILABLE):
        return QualityAssessment(quality, _unique(reasons), ("structural",))

    if freshness is Freshness.STALE:
        quality = worst_quality(quality, DataQuality.STALE)
        reasons.append("bar is stale for its exchange and interval")
        flags.append("stale")

    comparable = [
        item
        for item in previous_bars
        if item.instrument_id == bar.instrument_id
        and item.provider == bar.provider
        and item.interval is bar.interval
        and item.volume is not None
        and item.observed_at < bar.observed_at
    ]
    comparable.sort(key=lambda item: item.observed_at)
    if bar.volume is not None and len(comparable) >= 3:
        window = comparable[-policy.volume_median_window :]
        typical = median(item.volume for item in window if item.volume is not None)
        if typical > 0:
            multiple = float(bar.volume / typical)
            if multiple >= policy.volume_suspect_multiple:
                quality = worst_quality(quality, DataQuality.SUSPECT)
                reasons.append(
                    f"volume is {multiple:.1f}x the recent median; flagged for "
                    "evaluation"
                )
                flags.append("volume_suspect")
            elif multiple >= policy.volume_warning_multiple:
                quality = worst_quality(quality, DataQuality.WARNING)
                reasons.append(
                    f"volume is {multiple:.1f}x the recent median"
                )
                flags.append("volume_warning")

    return QualityAssessment(quality, _unique(reasons), _unique(flags))


def reconcile(
    observations: Sequence[PriceObservation],
    *,
    instrument: Instrument,
    priority: Sequence[str] = (),
    settings: QualitySettings | None = None,
) -> ReconciliationResult:
    """Resolve the latest price across providers without inventing one.

    Older observations outside the window are retained but not compared: two
    prices from different sessions are not a disagreement. Prices inside the
    window must agree within the relative tolerance; if they do not, the result
    is CONFLICT and ``selected`` is ``None``.
    """
    policy = settings or QualitySettings()
    relevant = tuple(
        item for item in observations if item.instrument_id == instrument.instrument_id
    )
    if not relevant:
        return ReconciliationResult(
            instrument_id=instrument.instrument_id,
            status=ReconciliationStatus.UNAVAILABLE,
            selected=None,
            reasons=("no observation is stored for this instrument",),
        )

    ordered_priority = {name: index for index, name in enumerate(priority)}
    ordered = sorted(
        relevant,
        key=lambda item: (
            item.observed_at,
            -ordered_priority.get(item.provider, len(ordered_priority)),
        ),
        reverse=True,
    )
    usable = [item for item in ordered if item.usable]
    if not usable:
        reasons: list[str] = []
        for item in ordered:
            reasons.extend(item.reasons)
        return ReconciliationResult(
            instrument_id=instrument.instrument_id,
            status=ReconciliationStatus.UNAVAILABLE,
            selected=None,
            observations=tuple(ordered),
            reasons=_unique(reasons) or ("every stored observation is unusable",),
        )

    newest = usable[0]
    window = [
        item
        for item in usable
        if abs((newest.observed_at - item.observed_at).total_seconds())
        <= policy.reconciliation_window_seconds
    ]
    if len(window) == 1:
        note = "single provider supplied the latest observation"
        if len(usable) > 1:
            note += "; older observations are outside the reconciliation window"
        return ReconciliationResult(
            instrument_id=instrument.instrument_id,
            status=ReconciliationStatus.OK,
            selected=newest,
            observations=tuple(ordered),
            reasons=(note,),
        )

    currencies = sorted({item.currency for item in window})
    if len(currencies) > 1:
        return ReconciliationResult(
            instrument_id=instrument.instrument_id,
            status=ReconciliationStatus.CONFLICT,
            selected=None,
            observations=tuple(ordered),
            reasons=(
                "providers disagree on currency: " + ", ".join(currencies),
            ),
        )

    prices = [item.price for item in window]
    lowest, highest = min(prices), max(prices)
    if lowest <= 0:
        return ReconciliationResult(
            instrument_id=instrument.instrument_id,
            status=ReconciliationStatus.UNAVAILABLE,
            selected=None,
            observations=tuple(ordered),
            reasons=("a provider returned a non-positive price",),
        )
    spread = (highest - lowest) / lowest
    if spread > Decimal(str(policy.reconciliation_tolerance_ratio)):
        detail = "; ".join(
            f"{item.provider}={item.price} {item.currency} at "
            f"{item.observed_at.isoformat()}"
            for item in window
        )
        return ReconciliationResult(
            instrument_id=instrument.instrument_id,
            status=ReconciliationStatus.CONFLICT,
            selected=None,
            observations=tuple(ordered),
            reasons=(
                f"provider prices differ by {float(spread) * 100:.3f}%, above the "
                f"{policy.reconciliation_tolerance_ratio * 100:.3f}% tolerance",
                detail,
                "the observations are preserved; no price was averaged",
            ),
        )

    selected = min(
        window,
        key=lambda item: ordered_priority.get(item.provider, len(ordered_priority)),
    )
    return ReconciliationResult(
        instrument_id=instrument.instrument_id,
        status=ReconciliationStatus.OK,
        selected=selected,
        observations=tuple(ordered),
        reasons=(
            f"{len(window)} providers agree within "
            f"{policy.reconciliation_tolerance_ratio * 100:.3f}%; selected "
            f"{selected.provider} by configured priority",
        ),
    )


def detect_daily_gaps(
    instrument: Instrument,
    bars: Sequence[OhlcvBar],
    *,
    calendar: Any | None = None,
) -> tuple[str, ...]:
    """Report missing trading sessions between the first and last daily bar.

    This is diagnostic, not a rejection: a provider's holiday calendar and ours
    can differ, which is exactly why the gap is reported with the missing dates
    rather than silently deleting a bar.
    """
    if calendar is None:
        return ()
    daily = sorted(
        (
            item
            for item in bars
            if item.instrument_id == instrument.instrument_id
            and item.interval is DataInterval.DAY_1
        ),
        key=lambda item: item.observed_at,
    )
    if len(daily) < 2:
        return ()
    local_days = {calendar.local(item.observed_at).date() for item in daily}
    start, end = min(local_days), max(local_days)
    missing: list[str] = []
    from datetime import timedelta

    day = start
    while day <= end:
        if calendar.is_trading_day(day) and day not in local_days:
            missing.append(day.isoformat())
        day += timedelta(days=1)
    if not missing:
        return ()
    return (
        f"{len(missing)} trading session(s) have no daily bar between {start} and "
        f"{end}: {', '.join(missing[:10])}"
        + ("..." if len(missing) > 10 else ""),
    )


__all__ = [
    "QualityAssessment",
    "QualitySettings",
    "assess_bar",
    "assess_quote",
    "detect_daily_gaps",
    "reconcile",
]
