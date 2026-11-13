"""Reproducible feature engineering over NM-02 daily bars (NM-06).

Every feature here is a pure function of bars whose ``observed_at`` is at or
before the cutoff. The builder never looks at a later bar, never reads a quote
that arrived after the cutoff, and has no random component — the digest of the
exact inputs is stored with the feature set so a forecast can be reproduced.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from novacontrol.novamarket.forecasting.models import (
    FEATURE_SET_VERSION,
    ForecastPolicy,
    Regime,
)


@dataclass(frozen=True, slots=True)
class PriceBar:
    """The only bar fields features and models may use."""

    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None

    def to_payload(self) -> list[Any]:
        return [
            self.observed_at.isoformat(),
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
        ]


@dataclass(frozen=True, slots=True)
class FeatureSet:
    instrument_id: str
    cutoff: datetime
    feature_set_version: str
    values: dict[str, float]
    observation_count: int
    first_observed_at: datetime
    last_observed_at: datetime
    last_close: float
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "cutoff": self.cutoff.isoformat(),
            "feature_set_version": self.feature_set_version,
            "values": {name: round(value, 8) for name, value in self.values.items()},
            "observation_count": self.observation_count,
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "last_close": round(self.last_close, 6),
            "digest": self.digest,
        }


def _usable_bars(bars: Iterable[PriceBar], cutoff: datetime) -> list[PriceBar]:
    """Only completed bars at or before the cutoff, one per timestamp."""
    chosen: dict[str, PriceBar] = {}
    for bar in bars:
        if bar.observed_at > cutoff:
            continue
        if not all(
            math.isfinite(value) and value > 0
            for value in (bar.open, bar.high, bar.low, bar.close)
        ):
            continue
        if bar.high < bar.low or not bar.low <= bar.close <= bar.high:
            continue
        chosen[bar.observed_at.isoformat()] = bar
    return [chosen[key] for key in sorted(chosen)]


def _returns(closes: Sequence[float]) -> list[float]:
    return [
        (current / previous) - 1.0
        for previous, current in zip(closes, closes[1:])
        if previous > 0
    ]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def _sma(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return _mean(values[-window:])


def _ema(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    alpha = 2.0 / (window + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(closes: Sequence[float], window: int = 14) -> float | None:
    if len(closes) < window + 1:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    recent = changes[-window:]
    gains = sum(change for change in recent if change > 0) / window
    losses = sum(-change for change in recent if change < 0) / window
    if losses == 0:
        return 100.0
    relative = gains / losses
    return 100.0 - (100.0 / (1.0 + relative))


def _atr(bars: Sequence[PriceBar], window: int = 14) -> float | None:
    if len(bars) < window + 1:
        return None
    ranges: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return _mean(ranges[-window:])


def _trend_slope(closes: Sequence[float], window: int = 20) -> float:
    if len(closes) < window:
        return 0.0
    values = closes[-window:]
    count = len(values)
    mean_index = (count - 1) / 2.0
    mean_value = _mean(values)
    covariance = sum(
        (index - mean_index) * (value - mean_value)
        for index, value in enumerate(values)
    )
    variance = sum((index - mean_index) ** 2 for index in range(count))
    if variance == 0 or mean_value == 0:
        return 0.0
    return (covariance / variance) * count / mean_value


def _relative_volume(bars: Sequence[PriceBar]) -> float:
    volumes = [bar.volume for bar in bars if bar.volume is not None]
    if len(volumes) < 21:
        return 1.0
    short = _mean(volumes[-5:])
    long = _mean(volumes[-20:])
    if long <= 0:
        return 1.0
    return short / long


def compute_features(
    bars: Iterable[PriceBar],
    *,
    instrument_id: str,
    cutoff: datetime,
    policy: ForecastPolicy,
) -> FeatureSet | None:
    """Build the feature set, or ``None`` when history is insufficient."""
    usable = _usable_bars(bars, cutoff)
    if len(usable) < policy.min_history_bars:
        return None
    closes = [bar.close for bar in usable]
    returns = _returns(closes)
    last = closes[-1]
    values: dict[str, float] = {
        "last_close": last,
        "ret_1": (closes[-1] / closes[-2]) - 1.0 if len(closes) >= 2 else 0.0,
        "ret_5": (closes[-1] / closes[-6]) - 1.0 if len(closes) >= 6 else 0.0,
        "ret_21": (closes[-1] / closes[-22]) - 1.0 if len(closes) >= 22 else 0.0,
        "vol_21": _stdev(returns[-21:]) * math.sqrt(252.0),
        "vol_63": _stdev(returns[-63:]) * math.sqrt(252.0),
        "atr_14_pct": (_atr(usable, 14) or 0.0) / last,
        "range_20_pct": _mean(
            [(bar.high - bar.low) / bar.close for bar in usable[-20:]]
        ),
        "rel_volume": _relative_volume(usable),
        "drawdown_60": (
            (last / max(closes[-60:])) - 1.0 if len(closes) >= 2 else 0.0
        ),
        "trend_slope_20": _trend_slope(closes, 20),
        "momentum_10": (closes[-1] / closes[-11]) - 1.0 if len(closes) >= 11 else 0.0,
    }
    sma20 = _sma(closes, 20)
    sma60 = _sma(closes, 60)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    rsi = _rsi(closes, 14)
    values["sma_20_gap"] = (last / sma20) - 1.0 if sma20 else 0.0
    values["sma_60_gap"] = (last / sma60) - 1.0 if sma60 else 0.0
    values["ema_12_26"] = ((ema12 - ema26) / last) if ema12 and ema26 else 0.0
    values["rsi_14"] = rsi if rsi is not None else 50.0
    digest_payload = {
        "feature_set_version": FEATURE_SET_VERSION,
        "instrument_id": instrument_id,
        "cutoff": cutoff.isoformat(),
        "policy": policy.to_dict(),
        "bars": [bar.to_payload() for bar in usable],
    }
    canonical = json.dumps(
        digest_payload, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return FeatureSet(
        instrument_id=instrument_id,
        cutoff=cutoff,
        feature_set_version=FEATURE_SET_VERSION,
        values=values,
        observation_count=len(usable),
        first_observed_at=usable[0].observed_at,
        last_observed_at=usable[-1].observed_at,
        last_close=last,
        digest=digest,
    )


def classify_regime(features: FeatureSet) -> Regime:
    """A measurable regime label; UNCERTAIN when the evidence is mixed."""
    values = features.values
    vol = values.get("vol_21", 0.0)
    vol_long = values.get("vol_63", 0.0)
    gap = values.get("sma_20_gap", 0.0)
    slope = values.get("trend_slope_20", 0.0)
    if vol > max(vol_long * 1.5, 0.025):
        return Regime.HIGH_VOLATILITY
    if gap > 0.02 and slope > 0:
        return Regime.TRENDING_UP
    if gap < -0.02 and slope < 0:
        return Regime.TRENDING_DOWN
    if vol < 0.012:
        return Regime.LOW_VOLATILITY
    if abs(gap) < 0.015:
        return Regime.SIDEWAYS
    return Regime.UNCERTAIN


def closes_of(bars: Sequence[PriceBar]) -> list[float]:
    return [bar.close for bar in bars]


__all__ = [
    "FeatureSet",
    "PriceBar",
    "classify_regime",
    "closes_of",
    "compute_features",
]
