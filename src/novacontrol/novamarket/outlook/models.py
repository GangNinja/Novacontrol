"""PI-10 swing and long-term outlook contracts.

PI-10 adds horizon profiles and context evidence over the existing prediction
stack.  It does not add a forecast engine or a second store: every numeric
forecast remains a PI-01 ``ForecastOutput`` produced by PI-05–PI-08 and
persisted by the existing prediction/PI-09 tables.

The target semantics are explicit:

* swing forecasts target 1, 3 or 5 verified trading sessions;
* long-term forecasts target 30 or 90 elapsed calendar days, then the first
  verified session close at or after that date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from novacontrol.novamarket.features.models import FEATURE_SET_VERSION, FeatureProfileName
from novacontrol.novamarket.prediction.models import (
    ForecastFamily,
    ForecastHorizon,
    HORIZON_SPECS,
    PredictionValidationError,
    _aware,
)

PI10_PHASE = "PI-10"
OUTLOOK_CONTRACT_VERSION = "pi10.outlook.1.0"
HORIZON_PROFILE_VERSION = "pi10-horizon-profiles-1.0"
CONTEXT_CONTRACT_VERSION = "pi10.context.1.0"


class TargetBasis(StrEnum):
    TRADING_SESSIONS = "trading_sessions"
    CALENDAR_DAYS = "calendar_days"


class OutlookStatus(StrEnum):
    GENERATED = "generated"
    ABSTAINED = "abstained"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HorizonOutlookProfile:
    """One explicit forecast horizon and the evidence it may consume."""

    horizon: ForecastHorizon
    family: ForecastFamily
    label: str
    target_basis: TargetBasis
    target_count: int
    feature_profile: FeatureProfileName
    objective: str
    required_inputs: tuple[str, ...]
    optional_context: tuple[str, ...]
    confirmation_policy: str
    order: int

    def __post_init__(self) -> None:
        try:
            horizon = ForecastHorizon(self.horizon)
            family = ForecastFamily(self.family)
            basis = TargetBasis(self.target_basis)
            feature_profile = FeatureProfileName(self.feature_profile)
        except ValueError as exc:
            raise PredictionValidationError(f"invalid PI-10 horizon profile: {exc}") from exc
        if family is not (ForecastFamily.SWING if horizon in _SWING else ForecastFamily.LONG_TERM):
            raise PredictionValidationError(
                f"{horizon.value} is not in the {family.value} horizon family"
            )
        expected_basis = (
            TargetBasis.TRADING_SESSIONS
            if family is ForecastFamily.SWING
            else TargetBasis.CALENDAR_DAYS
        )
        if basis is not expected_basis:
            raise PredictionValidationError(
                f"{horizon.value} must use {expected_basis.value} target semantics"
            )
        if self.target_count < 1:
            raise PredictionValidationError("a horizon target count must be positive")
        if not self.required_inputs:
            raise PredictionValidationError("a horizon requires at least validated market bars")
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "target_basis", basis)
        object.__setattr__(self, "feature_profile", feature_profile)
        object.__setattr__(self, "required_inputs", tuple(self.required_inputs))
        object.__setattr__(self, "optional_context", tuple(self.optional_context))

    @property
    def duration_seconds(self) -> int:
        return int(HORIZON_SPECS[self.horizon].duration.total_seconds())

    @property
    def model_step_hint(self) -> int:
        """The existing PI-05 daily step convention, for lineage only."""
        return 1 if self.horizon is ForecastHorizon.DAY_1 else self.target_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "family": self.family.value,
            "label": self.label,
            "target_basis": self.target_basis.value,
            "target_count": self.target_count,
            "target_rule": (
                f"{self.target_count} verified trading session(s)"
                if self.target_basis is TargetBasis.TRADING_SESSIONS
                else f"{self.target_count} calendar day(s), then the first verified session close"
            ),
            "duration_seconds": self.duration_seconds,
            "feature_profile": self.feature_profile.value,
            "feature_set_version": FEATURE_SET_VERSION,
            "objective": self.objective,
            "required_inputs": list(self.required_inputs),
            "optional_context": list(self.optional_context),
            "confirmation_policy": self.confirmation_policy,
            "model_families": [
                "statistical",
                "xgboost",
                "lightgbm",
                "lstm",
                "ensemble",
            ],
            "probabilities": (
                "withheld unless every contributing model publishes calibrated "
                "directional probabilities"
            ),
            "order": self.order,
            "profile_version": HORIZON_PROFILE_VERSION,
        }


_SWING = frozenset(
    {ForecastHorizon.DAY_1, ForecastHorizon.DAYS_3, ForecastHorizon.DAYS_5}
)

HORIZON_OUTLOOK_PROFILES: dict[ForecastHorizon, HorizonOutlookProfile] = {
    ForecastHorizon.DAY_1: HorizonOutlookProfile(
        ForecastHorizon.DAY_1,
        ForecastFamily.SWING,
        "Next-session swing",
        TargetBasis.TRADING_SESSIONS,
        1,
        FeatureProfileName.SWING,
        "Next verified session price, return and direction.",
        ("validated daily bars",),
        ("fundamentals", "sector", "benchmark", "news", "corporate events", "regime"),
        "Immediate initial forecast; T-60m confirmation; T-1m final.",
        1,
    ),
    ForecastHorizon.DAYS_3: HorizonOutlookProfile(
        ForecastHorizon.DAYS_3,
        ForecastFamily.SWING,
        "Three-session swing",
        TargetBasis.TRADING_SESSIONS,
        3,
        FeatureProfileName.SWING,
        "Three verified trading sessions ahead, net of weekends and holidays.",
        ("validated daily bars",),
        ("fundamentals", "sector", "benchmark", "news", "corporate events", "regime"),
        "Immediate initial; one-session-ahead confirmation; T-1m final.",
        2,
    ),
    ForecastHorizon.DAYS_5: HorizonOutlookProfile(
        ForecastHorizon.DAYS_5,
        ForecastFamily.SWING,
        "One-week swing",
        TargetBasis.TRADING_SESSIONS,
        5,
        FeatureProfileName.SWING,
        "Five verified trading sessions ahead with event and sector context.",
        ("validated daily bars",),
        ("fundamentals", "sector", "benchmark", "news", "corporate events", "regime"),
        "Immediate initial; two-session-ahead confirmation; T-1m final.",
        3,
    ),
    ForecastHorizon.DAYS_30: HorizonOutlookProfile(
        ForecastHorizon.DAYS_30,
        ForecastFamily.LONG_TERM,
        "One-month outlook",
        TargetBasis.CALENDAR_DAYS,
        30,
        FeatureProfileName.LONG_TERM,
        "Thirty elapsed calendar days, evaluated at the first verified session close.",
        ("validated daily bars",),
        ("point-in-time fundamentals", "sector", "benchmark", "news", "corporate events", "regime"),
        "Immediate initial; seven-calendar-day confirmation; two-calendar-day final.",
        4,
    ),
    ForecastHorizon.DAYS_90: HorizonOutlookProfile(
        ForecastHorizon.DAYS_90,
        ForecastFamily.LONG_TERM,
        "Quarter outlook",
        TargetBasis.CALENDAR_DAYS,
        90,
        FeatureProfileName.LONG_TERM,
        "Ninety elapsed calendar days, evaluated at the first verified session close.",
        ("validated daily bars",),
        ("point-in-time fundamentals", "sector", "benchmark", "news", "corporate events", "regime"),
        "Immediate initial; fourteen-calendar-day confirmation; three-calendar-day final.",
        5,
    ),
}

PI10_HORIZONS: tuple[ForecastHorizon, ...] = (
    ForecastHorizon.DAY_1,
    ForecastHorizon.DAYS_3,
    ForecastHorizon.DAYS_5,
    ForecastHorizon.DAYS_30,
    ForecastHorizon.DAYS_90,
)


def outlook_profile(horizon: ForecastHorizon | str) -> HorizonOutlookProfile:
    try:
        return HORIZON_OUTLOOK_PROFILES[ForecastHorizon(horizon)]
    except (ValueError, KeyError) as exc:
        known = ", ".join(item.value for item in PI10_HORIZONS)
        raise PredictionValidationError(
            f"PI-10 supports only swing/long horizons ({known}), not {horizon!r}"
        ) from exc


def validate_outlook_profiles() -> tuple[str, ...]:
    """Structural problems with the five profiles, or ``()`` when distinct."""
    problems: list[str] = []
    if set(HORIZON_OUTLOOK_PROFILES) != set(PI10_HORIZONS):
        problems.append("exactly one PI-10 profile is required for each supported horizon")
    orders = [HORIZON_OUTLOOK_PROFILES[item].order for item in PI10_HORIZONS]
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        problems.append("PI-10 horizon ordering must be unique and increasing")
    bases = {
        HORIZON_OUTLOOK_PROFILES[item].target_basis for item in PI10_HORIZONS
    }
    if bases != {TargetBasis.TRADING_SESSIONS, TargetBasis.CALENDAR_DAYS}:
        problems.append("both trading-session and calendar-day target semantics are required")
    for horizon in PI10_HORIZONS:
        profile = HORIZON_OUTLOOK_PROFILES[horizon]
        if profile.horizon is not horizon:
            problems.append(f"{horizon.value} profile points at another horizon")
        if profile.target_count != (
            1 if horizon is ForecastHorizon.DAY_1 else int(horizon.value.rstrip("d"))
        ):
            problems.append(f"{horizon.value} has an inconsistent target count")
    return tuple(problems)


@dataclass(frozen=True, slots=True)
class ContextSeries:
    """A bounded, read-only sector/benchmark/peer observation summary."""

    role: str
    instrument_id: str
    available: bool
    bars: int
    first_at: datetime | None = None
    last_at: datetime | None = None
    return_1: float | None = None
    return_20: float | None = None
    volatility_20: float | None = None
    sma_20: float | None = None
    trend: str = "unavailable"
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("first_at", "last_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "instrument_id": self.instrument_id,
            "available": self.available,
            "bars": self.bars,
            "first_at": self.first_at.isoformat() if self.first_at else None,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "return_1": self.return_1,
            "return_20": self.return_20,
            "volatility_20": self.volatility_20,
            "sma_20": self.sma_20,
            "trend": self.trend,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class HorizonContext:
    """Point-in-time context shared by all five PI-10 horizons."""

    instrument_id: str
    as_of: datetime
    instrument: ContextSeries | None = None
    sector: ContextSeries | None = None
    benchmark: ContextSeries | None = None
    peers: tuple[ContextSeries, ...] = ()
    fundamentals: tuple[Mapping[str, Any], ...] = ()
    events: tuple[Mapping[str, Any], ...] = ()
    sector_relative_return_20: float | None = None
    benchmark_relative_return_20: float | None = None
    notes: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "peers", tuple(self.peers))
        object.__setattr__(self, "fundamentals", tuple(dict(item) for item in self.fundamentals))
        object.__setattr__(self, "events", tuple(dict(item) for item in self.events))
        object.__setattr__(self, "notes", tuple(dict.fromkeys(str(item) for item in self.notes)))
        object.__setattr__(self, "counts", {str(k): int(v) for k, v in self.counts.items()})

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": PI10_PHASE,
            "contract_version": CONTEXT_CONTRACT_VERSION,
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "point_in_time": True,
            "instrument": self.instrument.to_dict() if self.instrument else None,
            "sector": self.sector.to_dict() if self.sector else None,
            "benchmark": self.benchmark.to_dict() if self.benchmark else None,
            "peers": [item.to_dict() for item in self.peers],
            "fundamentals": [dict(item) for item in self.fundamentals],
            "events": [dict(item) for item in self.events],
            "sector_relative_return_20": self.sector_relative_return_20,
            "benchmark_relative_return_20": self.benchmark_relative_return_20,
            "notes": list(self.notes),
            "counts": dict(self.counts),
            "missing_context_is_explicit": True,
            "fabricated_values": False,
        }


__all__ = [
    "CONTEXT_CONTRACT_VERSION",
    "ContextSeries",
    "HORIZON_OUTLOOK_PROFILES",
    "HORIZON_PROFILE_VERSION",
    "HorizonContext",
    "HorizonOutlookProfile",
    "OUTLOOK_CONTRACT_VERSION",
    "OutlookStatus",
    "PI10_HORIZONS",
    "PI10_PHASE",
    "TargetBasis",
    "outlook_profile",
    "validate_outlook_profiles",
]
