"""Deterministic market-regime detection from PI-04 features.

The detector reports one primary regime plus simultaneous secondary
conditions.  It never turns a regime into a price prediction, and it never
reports an uncalibrated confidence: ``confidence`` stays ``None`` until a
separate calibration phase has measured it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from novacontrol.novamarket.features.models import (
    REGIME_METHOD_VERSION,
    FeatureQuality,
    FeatureSet,
    RegimeLabel,
    RegimeResult,
    RegimeTransition,
)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Classification:
    """One label decision and the evidence that produced it."""

    primary: RegimeLabel
    secondary: tuple[RegimeLabel, ...]
    evidence: Mapping[str, float]
    supporting: tuple[str, ...]
    reasons: tuple[str, ...]


class RegimeDetector:
    """A transparent threshold model with explicit uncertain output."""

    def __init__(
        self,
        *,
        strong_trend_score: int = 4,
        weak_trend_score: int = 2,
        strong_adx: float = 25.0,
        trend_adx: float = 20.0,
        high_volatility_range_expansion: float = 1.25,
        low_volatility_range_expansion: float = 0.75,
        high_bollinger_width: float = 0.10,
        low_bollinger_width: float = 0.025,
    ) -> None:
        self.strong_trend_score = int(strong_trend_score)
        self.weak_trend_score = int(weak_trend_score)
        self.strong_adx = float(strong_adx)
        self.trend_adx = float(trend_adx)
        self.high_volatility_range_expansion = float(high_volatility_range_expansion)
        self.low_volatility_range_expansion = float(low_volatility_range_expansion)
        self.high_bollinger_width = float(high_bollinger_width)
        self.low_bollinger_width = float(low_bollinger_width)

    def detect(
        self,
        feature_set: FeatureSet,
        *,
        previous: RegimeResult | RegimeLabel | str | None = None,
        exchange: str = "",
        generated_at: datetime | None = None,
    ) -> RegimeResult:
        values = feature_set.values
        quality = feature_set.quality
        reasons: list[str] = []
        if (
            isinstance(previous, RegimeResult)
            and previous.as_of >= feature_set.as_of
        ):
            previous = None
            reasons.append("a previous regime at or after the feature cutoff was ignored")
        if quality is FeatureQuality.INVALID or not values:
            return self._result(
                feature_set,
                RegimeLabel.UNCERTAIN,
                (),
                {},
                (),
                quality,
                "invalid",
                previous,
                exchange,
                generated_at,
                ("no usable feature values were generated",),
            )
        if quality is FeatureQuality.INSUFFICIENT:
            return self._result(
                feature_set,
                RegimeLabel.UNCERTAIN,
                (),
                {},
                (),
                quality,
                "insufficient",
                previous,
                exchange,
                generated_at,
                ("feature completeness is insufficient for a regime classification",),
            )

        classification = self._classify(values)
        data_quality = str(quality)
        return self._result(
            feature_set,
            classification.primary,
            classification.secondary,
            classification.evidence,
            classification.supporting,
            quality,
            data_quality,
            previous,
            exchange,
            generated_at,
            tuple((*reasons, *classification.reasons)),
        )

    def _classify(self, values: Mapping[str, Any]) -> _Classification:
        """The label decision itself, over one feature snapshot's values.

        Every threshold read here is a declared detector parameter, and this is
        the only place a regime label is decided; ``classify_regime`` exposes the
        same decision to other subsystems so none of them restates it.
        """
        reasons: list[str] = []
        close = _value(values, "close")
        sma20 = _value(values, "sma_20")
        sma50 = _value(values, "sma_50")
        ema50 = _value(values, "ema_50")
        rsi = _value(values, "rsi_14")
        adx = _value(values, "adx_14")
        plus_di = _value(values, "plus_di_14")
        minus_di = _value(values, "minus_di_14")
        return_5 = _value(values, "return_5")
        return_20 = _value(values, "return_20")
        bollinger_position = _value(values, "bollinger_position")
        range_expansion = _value(values, "range_expansion_5_20")
        bollinger_width = _value(values, "bollinger_width_pct")
        volatility_change = _value(values, "volatility_change_5_20")
        breakout = _value(values, "breakout_20")
        breakdown = _value(values, "breakdown_20")
        range_bound = _value(values, "range_bound_20")
        mtf_agreement = _value(values, "mtf_trend_agreement")
        mtf_alignment = _value(values, "mtf_alignment")
        relative_volume = _value(values, "relative_volume_20")

        trend_votes: list[int] = []
        support: list[str] = []
        if close is not None and sma20 not in (None, 0):
            vote = 1 if close > sma20 else -1 if close < sma20 else 0
            if vote:
                trend_votes.append(vote)
                support.append("sma_20")
        if sma20 is not None and sma50 is not None and sma50 != 0:
            vote = 1 if sma20 > sma50 else -1 if sma20 < sma50 else 0
            if vote:
                trend_votes.append(vote)
                support.append("sma_50")
        if sma50 is not None and ema50 is not None and sma50 != ema50:
            vote = 1 if sma50 > ema50 else -1
            trend_votes.append(vote)
            support.append("ema_50")
        if plus_di is not None and minus_di is not None:
            if plus_di > minus_di:
                trend_votes.append(1)
                support.append("plus_di_14")
            elif minus_di > plus_di:
                trend_votes.append(-1)
                support.append("minus_di_14")
        if rsi is not None:
            if rsi >= 55:
                trend_votes.append(1)
                support.append("rsi_14")
            elif rsi <= 45:
                trend_votes.append(-1)
                support.append("rsi_14")
        if return_20 is not None:
            if return_20 > 0:
                trend_votes.append(1)
                support.append("return_20")
            elif return_20 < 0:
                trend_votes.append(-1)
                support.append("return_20")
        if mtf_alignment is not None:
            if mtf_alignment >= 0.75:
                trend_votes.append(1)
                support.append("mtf_alignment")
            elif mtf_alignment <= 0.25:
                trend_votes.append(-1)
                support.append("mtf_alignment")

        score = sum(trend_votes) if trend_votes else 0
        if mtf_agreement is not None and mtf_agreement < 0.35 and abs(score) <= 2:
            score = 0
            reasons.append("cross-timeframe agreement is too weak for a directional trend")
            support.append("mtf_trend_agreement")
        if adx is not None:
            support.append("adx_14")
        high_volatility = self._high_volatility(
            range_expansion, bollinger_width, volatility_change
        )
        low_volatility = self._low_volatility(
            range_expansion, bollinger_width, volatility_change
        )
        potential_reversal = self._potential_reversal(
            rsi, bollinger_position, return_5, return_20, breakout, breakdown
        )
        if breakout is not None and breakout > 0:
            support.append("breakout_20")
        if breakdown is not None and breakdown > 0:
            support.append("breakdown_20")
        if relative_volume is not None:
            support.append("relative_volume_20")

        trend_strength = abs(score)
        directional = score > 0 or score < 0
        strong = (
            directional
            and trend_strength >= self.strong_trend_score
            and adx is not None
            and adx >= self.strong_adx
        )
        weak = (
            directional
            and trend_strength >= self.weak_trend_score
            and (adx is not None and adx >= self.trend_adx or not strong)
        )
        if strong and score > 0:
            primary = RegimeLabel.STRONG_UP
        elif strong and score < 0:
            primary = RegimeLabel.STRONG_DOWN
        elif weak and score > 0:
            primary = RegimeLabel.WEAK_UP
        elif weak and score < 0:
            primary = RegimeLabel.WEAK_DOWN
        elif breakout is not None and breakout > 0:
            primary = RegimeLabel.BREAKOUT
        elif breakdown is not None and breakdown > 0:
            primary = RegimeLabel.BREAKDOWN
        elif potential_reversal:
            primary = RegimeLabel.POTENTIAL_REVERSAL
        elif high_volatility and abs(score) <= 1:
            primary = RegimeLabel.HIGH_VOLATILITY
        elif low_volatility and abs(score) <= 1:
            primary = RegimeLabel.LOW_VOLATILITY
        elif abs(score) <= 1 and (adx is None or adx < self.trend_adx):
            primary = RegimeLabel.SIDEWAYS
        else:
            primary = RegimeLabel.UNCERTAIN
            reasons.append("trend, volatility and structure signals do not agree")

        secondary: list[RegimeLabel] = []
        if high_volatility and primary is not RegimeLabel.HIGH_VOLATILITY:
            secondary.append(RegimeLabel.HIGH_VOLATILITY)
        if low_volatility and primary is not RegimeLabel.LOW_VOLATILITY:
            secondary.append(RegimeLabel.LOW_VOLATILITY)
        if potential_reversal and primary is not RegimeLabel.POTENTIAL_REVERSAL:
            secondary.append(RegimeLabel.POTENTIAL_REVERSAL)
        if breakout is not None and breakout > 0 and primary is not RegimeLabel.BREAKOUT:
            secondary.append(RegimeLabel.BREAKOUT)
        if breakdown is not None and breakdown > 0 and primary is not RegimeLabel.BREAKDOWN:
            secondary.append(RegimeLabel.BREAKDOWN)
        if primary is RegimeLabel.UNCERTAIN and not reasons:
            reasons.append("insufficient directional or structural evidence")

        evidence = {
            name: float(values[name])
            for name in (
                "rsi_14",
                "adx_14",
                "atr_pct",
                "bollinger_width_pct",
                "bollinger_position",
                "range_expansion_5_20",
                "relative_volume_20",
                "return_5",
                "return_20",
                "mtf_trend_agreement",
            )
            if name in values
        }
        supporting = tuple(dict.fromkeys(support))
        return _Classification(
            primary=primary,
            secondary=tuple(dict.fromkeys(secondary)),
            evidence=evidence,
            supporting=supporting,
            reasons=tuple(reasons),
        )

    # -- conditions -----------------------------------------------------------

    def _high_volatility(
        self,
        range_expansion: float | None,
        bollinger_width: float | None,
        volatility_change: float | None,
    ) -> bool:
        signals = 0
        if range_expansion is not None and range_expansion >= self.high_volatility_range_expansion:
            signals += 1
        if bollinger_width is not None and bollinger_width >= self.high_bollinger_width:
            signals += 1
        if volatility_change is not None and volatility_change > 0:
            signals += 1
        return signals >= 2

    def _low_volatility(
        self,
        range_expansion: float | None,
        bollinger_width: float | None,
        volatility_change: float | None,
    ) -> bool:
        signals = 0
        if range_expansion is not None and range_expansion <= self.low_volatility_range_expansion:
            signals += 1
        if bollinger_width is not None and bollinger_width <= self.low_bollinger_width:
            signals += 1
        if volatility_change is not None and volatility_change <= 0:
            signals += 1
        return signals >= 2

    def _potential_reversal(
        self,
        rsi: float | None,
        bollinger_position: float | None,
        return_5: float | None,
        return_20: float | None,
        breakout: float | None,
        breakdown: float | None,
    ) -> bool:
        if breakout is not None and breakout > 0:
            return False
        if breakdown is not None and breakdown > 0:
            return False
        overbought = (
            (rsi is not None and rsi >= 70)
            or (bollinger_position is not None and bollinger_position >= 1.0)
        )
        oversold = (
            (rsi is not None and rsi <= 30)
            or (bollinger_position is not None and bollinger_position <= 0.0)
        )
        if overbought and return_5 is not None and return_20 is not None and return_5 < 0 <= return_20:
            return True
        if oversold and return_5 is not None and return_20 is not None and return_5 > 0 >= return_20:
            return True
        return bool((overbought or oversold) and return_5 is not None and return_20 is not None)

    # -- result ---------------------------------------------------------------

    def _result(
        self,
        feature_set: FeatureSet,
        primary: RegimeLabel,
        secondary: tuple[RegimeLabel, ...],
        evidence: Mapping[str, float],
        supporting: tuple[str, ...],
        quality: FeatureQuality,
        data_quality: str,
        previous: RegimeResult | RegimeLabel | str | None,
        exchange: str,
        generated_at: datetime | None,
        reasons: tuple[str, ...],
    ) -> RegimeResult:
        transition: RegimeTransition | None = None
        if previous is not None:
            before = previous.primary if isinstance(previous, RegimeResult) else RegimeLabel(previous)
            transition = RegimeTransition(
                previous=before,
                current=primary,
                changed=before is not primary,
            )
        regime_id = "rg_" + _digest(
            {
                "feature_set_id": feature_set.feature_set_id,
                "method": REGIME_METHOD_VERSION,
                "primary": str(primary),
                "secondary": [str(item) for item in secondary],
                "previous": str(transition.previous) if transition else "",
                "changed": transition.changed if transition else None,
                "parameters": self.parameters,
            }
        )[:24]
        return RegimeResult(
            regime_id=regime_id,
            feature_set_id=feature_set.feature_set_id,
            feature_set_version=feature_set.version,
            instrument_id=feature_set.instrument_id,
            exchange=exchange or feature_set.exchange,
            as_of=feature_set.as_of,
            resolution=feature_set.resolution,
            primary=primary,
            secondary=secondary,
            confidence=None,
            confidence_calibrated=False,
            supporting_features=supporting,
            evidence=dict(evidence),
            feature_quality=quality,
            data_quality=data_quality,
            transition=transition,
            method=REGIME_METHOD_VERSION,
            generated_at=generated_at or feature_set.generated_at,
            reasons=reasons,
            parameters=self.parameters,
        )

    @property
    def parameters(self) -> dict[str, float]:
        return {
            "strong_trend_score": float(self.strong_trend_score),
            "weak_trend_score": float(self.weak_trend_score),
            "strong_adx": self.strong_adx,
            "trend_adx": self.trend_adx,
            "high_volatility_range_expansion": self.high_volatility_range_expansion,
            "low_volatility_range_expansion": self.low_volatility_range_expansion,
            "high_bollinger_width": self.high_bollinger_width,
            "low_bollinger_width": self.low_bollinger_width,
        }


def _value(values: Mapping[str, float], name: str) -> float | None:
    number = values.get(name)
    if number is None or not math.isfinite(float(number)):
        return None
    return float(number)


#: The detector whose published parameters are the only regime thresholds.
DEFAULT_REGIME_DETECTOR = RegimeDetector()


def classify_regime(
    values: Mapping[str, Any] | None,
    *,
    quality: FeatureQuality | str = FeatureQuality.VALID,
    detector: RegimeDetector | None = None,
) -> RegimeLabel:
    """PI-04's regime label for one feature snapshot's values.

    This is the only regime classifier in the codebase: a subsystem that needs a
    regime per row calls this function instead of restating thresholds, so every
    label shares one vocabulary and one set of limits.
    """
    if not isinstance(values, Mapping) or not values:
        return RegimeLabel.UNCERTAIN
    try:
        resolved = FeatureQuality(quality)
    except ValueError:
        resolved = FeatureQuality.INVALID
    if resolved is FeatureQuality.INVALID or resolved is FeatureQuality.INSUFFICIENT:
        return RegimeLabel.UNCERTAIN
    return (detector or DEFAULT_REGIME_DETECTOR)._classify(values).primary


__all__ = ["DEFAULT_REGIME_DETECTOR", "RegimeDetector", "classify_regime"]
