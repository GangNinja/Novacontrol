"""Explainable candidate scoring for NM-05.

The model answers exactly one question: **how much attention does this
instrument's current, evidenced situation deserve?** It does not answer whether
the instrument is a good investment, and it cannot express a direction.

Seven normalized factors produce the score. Their weights are fixed, published in
the contract, and sum to 1.0, so every contribution is readable:

========================  ======  ==================================================
factor                    weight  what it measures
========================  ======  ==================================================
``market_activity``       0.22    recent price change over the stored bars
``liquidity``             0.18    median traded value (close × volume) of those bars
``volatility``            0.15    dispersion of bar-to-bar returns
``event_relevance``       0.18    NM-04 events mapped to this instrument
``sector_relevance``      0.08    NM-04 events mapped to the instrument's sector
``freshness``             0.11    age of the newest usable data
``discovery_reason``      0.08    why the instrument entered this cycle
========================  ======  ==================================================

Why these weights: activity and events describe *why now*; liquidity and
volatility describe whether that activity is tradable and noteworthy at all;
freshness is a hard data-quality gate; the discovery reason is the smallest
factor so a pre-filter can nudge a candidate but never carry one.

A factor with no usable input is ``missing`` (value ``None``, contribution 0) and
subtracts half of its weight from the total as an explicit penalty. Missing is
never treated as zero: an instrument with no volume history is not illiquid, it
is unmeasured.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from novacontrol.novamarket.discovery.models import (
    METHODOLOGY_VERSION,
    CandidateEvaluation,
    DataQuality,
    DiscoveryPolicy,
    DiscoveryReason,
    ScoreFactor,
    digest_inputs,
)

#: Fixed weights. Re-weighting means a new methodology version, never a quiet edit.
WEIGHTS: dict[str, float] = {
    "market_activity": 0.22,
    "liquidity": 0.18,
    "volatility": 0.15,
    "event_relevance": 0.18,
    "sector_relevance": 0.08,
    "freshness": 0.11,
    "discovery_reason": 0.08,
}

#: Score caps by data quality: a stale or partial record cannot look excellent.
QUALITY_CAPS: dict[DataQuality, float] = {
    DataQuality.GOOD: 100.0,
    DataQuality.PARTIAL: 60.0,
    DataQuality.STALE: 35.0,
    DataQuality.INSUFFICIENT: 15.0,
}

#: Half a missing factor's weight is removed from the total, so gaps cost.
MISSING_PENALTY_RATIO = 0.5

IMPORTANCE_WEIGHT: dict[str, float] = {
    "low": 0.25,
    "moderate": 0.5,
    "high": 0.75,
    "critical": 1.0,
}

#: An unverified rumour is not evidence of relevance; a confirmed statement is.
VERIFICATION_WEIGHT: dict[str, float] = {
    "officially_confirmed": 1.0,
    "multi_source_corroborated": 0.9,
    "single_source": 0.5,
    "conflicting_evidence": 0.4,
    "unverified": 0.15,
    "retracted": 0.0,
    "expired": 0.0,
}

RELATIONSHIP_WEIGHT: dict[str, float] = {
    "direct": 1.0,
    "indirect": 0.6,
    "uncertain": 0.3,
}

REASON_VALUE: dict[DiscoveryReason, float] = {
    DiscoveryReason.CONFIGURED: 1.0,
    DiscoveryReason.EVENT: 0.9,
    DiscoveryReason.ACTIVITY: 0.75,
    DiscoveryReason.SECTOR: 0.6,
    DiscoveryReason.LIQUIDITY: 0.55,
    DiscoveryReason.CATALOGUE: 0.35,
}

#: Price change that saturates the activity factor (a 5% move is "full marks").
ACTIVITY_SATURATION_PCT = 5.0
#: Return dispersion that saturates the volatility factor.
VOLATILITY_SATURATION_PCT = 4.0
#: Traded value that saturates liquidity: 1e9 in the instrument's currency.
LIQUIDITY_SATURATION = 1e9


@dataclass(frozen=True, slots=True)
class BarPoint:
    """One completed bar, reduced to what scoring needs."""

    close: float
    volume: float | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class EventSignal:
    """One NM-04 event, reduced to its evidential weight."""

    event_id: str
    importance: str
    verification: str
    relationship: str
    published_at: datetime | None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _returns(bars: Sequence[BarPoint]) -> list[float]:
    values: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        if previous.close > 0 and current.close > 0:
            values.append((current.close - previous.close) / previous.close)
    return values


def _event_value(signals: Sequence[EventSignal], now: datetime, window_days: int) -> float:
    total = 0.0
    horizon = timedelta(days=window_days)
    for signal in signals:
        if signal.published_at is not None and now - signal.published_at > horizon:
            continue
        weight = (
            IMPORTANCE_WEIGHT.get(signal.importance, 0.25)
            * VERIFICATION_WEIGHT.get(signal.verification, 0.15)
            * RELATIONSHIP_WEIGHT.get(signal.relationship, 0.3)
        )
        total += weight
    return _clamp(total)


def _age_hours(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds() / 3600.0)


def _quality(
    *,
    has_quote: bool,
    bars: Sequence[BarPoint],
    newest: datetime | None,
    age_hours: float | None,
    missing: Sequence[str],
    policy: DiscoveryPolicy,
) -> DataQuality:
    if not has_quote and len(bars) < 2:
        return DataQuality.INSUFFICIENT
    if age_hours is None or age_hours > policy.stale_after_hours:
        return DataQuality.STALE
    if missing:
        return DataQuality.PARTIAL
    return DataQuality.GOOD


def _tier(score: float) -> str:
    if score >= 70:
        return "priority"
    if score >= 50:
        return "review"
    if score >= 30:
        return "monitor"
    return "background"


def score_candidate(
    *,
    instrument_id: str,
    asset_class: str,
    trading_status: str,
    bars: Sequence[BarPoint],
    quote_price: float | None,
    quote_observed_at: datetime | None,
    events: Sequence[EventSignal],
    sector_events: Sequence[EventSignal],
    discovery_reason: DiscoveryReason,
    now: datetime,
    policy: DiscoveryPolicy,
) -> CandidateEvaluation:
    """Score one instrument, preserving every factor and the input digest."""
    ordered = list(bars)
    changes = _returns(ordered)
    factors: list[ScoreFactor] = []
    missing: list[str] = []
    notes: list[str] = []

    # --- market activity -----------------------------------------------------
    if len(ordered) >= 2 and ordered[0].close > 0:
        change_pct = abs(
            (ordered[-1].close - ordered[0].close) / ordered[0].close * 100.0
        )
        value = _clamp(change_pct / ACTIVITY_SATURATION_PCT)
        factors.append(
            ScoreFactor(
                "market_activity",
                value,
                WEIGHTS["market_activity"],
                value * WEIGHTS["market_activity"] * 100,
                f"price moved {change_pct:.2f}% across {len(ordered)} bars",
            )
        )
    else:
        missing.append("market_activity")
        factors.append(
            ScoreFactor(
                "market_activity",
                None,
                WEIGHTS["market_activity"],
                0.0,
                "fewer than two completed bars; the move is unknown, not zero",
                missing=True,
            )
        )

    # --- liquidity -----------------------------------------------------------
    traded = [
        bar.close * bar.volume
        for bar in ordered
        if bar.volume is not None and bar.close > 0
    ]
    if traded:
        median_value = statistics.median(traded)
        value = _clamp(math.log10(median_value + 1.0) / math.log10(LIQUIDITY_SATURATION))
        factors.append(
            ScoreFactor(
                "liquidity",
                value,
                WEIGHTS["liquidity"],
                value * WEIGHTS["liquidity"] * 100,
                f"median traded value {median_value:,.0f} over {len(traded)} bars",
            )
        )
    else:
        missing.append("liquidity")
        factors.append(
            ScoreFactor(
                "liquidity",
                None,
                WEIGHTS["liquidity"],
                0.0,
                "no bar carried volume; liquidity is unmeasured, not zero",
                missing=True,
            )
        )

    # --- volatility ----------------------------------------------------------
    if len(changes) >= 2:
        dispersion = statistics.pstdev(changes) * 100.0
        value = _clamp(dispersion / VOLATILITY_SATURATION_PCT)
        factors.append(
            ScoreFactor(
                "volatility",
                value,
                WEIGHTS["volatility"],
                value * WEIGHTS["volatility"] * 100,
                f"return dispersion {dispersion:.2f}% across {len(changes)} changes",
            )
        )
    else:
        missing.append("volatility")
        factors.append(
            ScoreFactor(
                "volatility",
                None,
                WEIGHTS["volatility"],
                0.0,
                "too few returns to measure dispersion",
                missing=True,
            )
        )

    # --- event and sector relevance -----------------------------------------
    event_value = _event_value(events, now, policy.event_window_days)
    factors.append(
        ScoreFactor(
            "event_relevance",
            event_value,
            WEIGHTS["event_relevance"],
            event_value * WEIGHTS["event_relevance"] * 100,
            f"{len(events)} mapped event(s), weighted by importance and verification",
        )
    )
    if sector_events:
        sector_value = _event_value(sector_events, now, policy.event_window_days)
        factors.append(
            ScoreFactor(
                "sector_relevance",
                sector_value,
                WEIGHTS["sector_relevance"],
                sector_value * WEIGHTS["sector_relevance"] * 100,
                f"{len(sector_events)} sector-level event(s)",
            )
        )
    else:
        factors.append(
            ScoreFactor(
                "sector_relevance",
                0.0,
                WEIGHTS["sector_relevance"],
                0.0,
                "no sector context available for this instrument",
            )
        )

    # --- freshness -----------------------------------------------------------
    newest = quote_observed_at
    if newest is None and ordered:
        newest = ordered[-1].observed_at
    age_hours = _age_hours(newest, now)
    if age_hours is None:
        missing.append("freshness")
        factors.append(
            ScoreFactor(
                "freshness",
                None,
                WEIGHTS["freshness"],
                0.0,
                "no timestamped market data for this instrument",
                missing=True,
            )
        )
    else:
        if age_hours <= 6:
            value = 1.0
        elif age_hours >= policy.stale_after_hours:
            value = 0.0
        else:
            span = max(policy.stale_after_hours - 6.0, 1.0)
            value = _clamp(1.0 - (age_hours - 6.0) / span)
        factors.append(
            ScoreFactor(
                "freshness",
                value,
                WEIGHTS["freshness"],
                value * WEIGHTS["freshness"] * 100,
                f"newest data is {age_hours:.1f}h old",
            )
        )

    # --- discovery reason ----------------------------------------------------
    reason_value = REASON_VALUE[discovery_reason]
    factors.append(
        ScoreFactor(
            "discovery_reason",
            reason_value,
            WEIGHTS["discovery_reason"],
            reason_value * WEIGHTS["discovery_reason"] * 100,
            f"discovered through {discovery_reason.value}",
        )
    )

    raw = sum(factor.contribution for factor in factors)
    penalty = sum(
        WEIGHTS[factor.name] * 100.0 * MISSING_PENALTY_RATIO
        for factor in factors
        if factor.missing
    )
    quality = _quality(
        has_quote=quote_price is not None,
        bars=ordered,
        newest=newest,
        age_hours=age_hours,
        missing=missing,
        policy=policy,
    )
    total = max(0.0, min(raw - penalty, QUALITY_CAPS[quality]))
    completeness = sum(
        WEIGHTS[factor.name] for factor in factors if not factor.missing
    )
    confidence = _clamp(
        completeness
        * {
            DataQuality.GOOD: 1.0,
            DataQuality.PARTIAL: 0.8,
            DataQuality.STALE: 0.4,
            DataQuality.INSUFFICIENT: 0.1,
        }[quality]
    )
    if trading_status in {"halted", "delisted"}:
        notes.append(f"trading status is {trading_status}")
    if quote_price is not None and quote_price <= 0:
        notes.append("quoted price is not positive")
    if not ordered and quote_price is None:
        notes.append("no quote and no bars: nothing can be ranked from this record")

    digest = digest_inputs(
        {
            "methodology": METHODOLOGY_VERSION,
            "instrument_id": instrument_id,
            "asset_class": asset_class,
            "trading_status": trading_status,
            "quote": [quote_price, quote_observed_at.isoformat() if quote_observed_at else None],
            "bars": [
                [bar.close, bar.volume, bar.observed_at.isoformat()] for bar in ordered
            ],
            "events": [
                [
                    signal.event_id,
                    signal.importance,
                    signal.verification,
                    signal.relationship,
                    signal.published_at.isoformat() if signal.published_at else None,
                ]
                for signal in events
            ],
            "sector_events": [signal.event_id for signal in sector_events],
            "reason": discovery_reason.value,
            "policy": policy.to_dict(),
        }
    )

    return CandidateEvaluation(
        instrument_id=instrument_id,
        score=round(total, 2),
        tier=_tier(total),
        data_quality=quality,
        confidence=confidence,
        factors=tuple(factors),
        missing_fields=tuple(missing),
        event_ids=tuple(signal.event_id for signal in events),
        discovery_reason=discovery_reason,
        methodology_version=METHODOLOGY_VERSION,
        evaluated_at=now,
        digest=digest,
        notes=tuple(notes),
    )


def describe_model() -> dict[str, Any]:
    """The published model description used by the contract endpoint."""
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "weights": dict(WEIGHTS),
        "quality_caps": {quality.value: cap for quality, cap in QUALITY_CAPS.items()},
        "importance_weights": dict(IMPORTANCE_WEIGHT),
        "verification_weights": dict(VERIFICATION_WEIGHT),
        "relationship_weights": dict(RELATIONSHIP_WEIGHT),
        "reason_values": {reason.value: value for reason, value in REASON_VALUE.items()},
        "missing_penalty_ratio": MISSING_PENALTY_RATIO,
        "notes": [
            "a missing factor contributes nothing and is charged half its weight",
            "data quality caps the score: partial 60, stale 35, insufficient 15",
            "discovery priority is not investment attractiveness and not a prediction",
        ],
    }


__all__ = [
    "BarPoint",
    "EventSignal",
    "QUALITY_CAPS",
    "WEIGHTS",
    "describe_model",
    "score_candidate",
]
