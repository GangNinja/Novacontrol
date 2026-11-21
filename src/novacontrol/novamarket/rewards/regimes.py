"""Per-market-regime metric slices computed from PI-13 observation evidence.

A regime label is derived from ONE observation's own point-in-time evidence.
Nothing is aggregated across splits, folds or evaluations to decide a label, so
a slice is reproducible from that observation alone — and a test split's evidence
can never influence a validation slice or a training decision.

The label itself is PI-04's: when the observation carries a feature snapshot,
``classify_regime`` runs PI-04's ``RegimeDetector`` over that snapshot's values,
so this slice shares PI-04's vocabulary (``strong_up``, ``weak_up``,
``strong_down``, ``weak_down``, ``sideways``, ``high_volatility``,
``low_volatility``, ``potential_reversal``, ``breakout``, ``breakdown``,
``uncertain``) and PI-04's published thresholds instead of restating its own.

The market-only fallback below is kept for the one case PI-04 cannot label — an
observation with no feature snapshot at all.  It reads that observation's own
20-bar trend and 20-bar realized volatility.  Its labels are still PI-04's own
(``weak_up``/``weak_down``, because market-only evidence cannot satisfy
PI-04's strong-trend requirement), so no slice ever publishes a second
vocabulary; its thresholds are published separately (``REGIME_THRESHOLDS``) so
they are never mistaken for PI-04's classifier parameters.
An observation whose evidence is missing or non-finite is ``unclassified``.
The slice is descriptive: a regime with too few observations reports that it is
insufficient rather than a conclusion.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.novamarket.features.models import (
    REGIME_METHOD_VERSION,
    FeatureQuality,
    RegimeLabel,
)
from novacontrol.novamarket.features.regime import (
    DEFAULT_REGIME_DETECTOR,
    classify_regime,
)

UNCLASSIFIED = "unclassified"

#: Market-only fallback thresholds: used ONLY when an observation carries no
#: feature snapshot, i.e. when PI-04 has no evidence to classify.  They are a
#: declared decision, not a fitted parameter, and are not PI-04's thresholds.
TREND_THRESHOLD = 0.02
HIGH_VOLATILITY_THRESHOLD = 0.02
LOW_VOLATILITY_THRESHOLD = 0.005
#: Below this many observations a regime draws no conclusion.
MIN_REGIME_SAMPLES = 3

REGIME_THRESHOLDS: dict[str, float] = {
    "trend_return_20": TREND_THRESHOLD,
    "high_volatility_20": HIGH_VOLATILITY_THRESHOLD,
    "low_volatility_20": LOW_VOLATILITY_THRESHOLD,
}

#: Which evidence produced a label.  Reported per row so a slice never claims a
#: PI-04 label for a row that actually used the market-only fallback.
SOURCE_FEATURES = "features"
SOURCE_MARKET = "market"
SOURCE_NONE = "none"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _feature_quality(features: Mapping[str, Any]) -> FeatureQuality:
    status = str(features.get("status") or features.get("quality") or "").strip().lower()
    try:
        return FeatureQuality(status)
    except ValueError:
        return FeatureQuality.INVALID


def _feature_regime(features: Mapping[str, Any]) -> str | None:
    """PI-04's label for this snapshot, or ``None`` when there is no evidence."""
    values = features.get("values")
    if not isinstance(values, Mapping) or not values:
        return None
    return str(classify_regime(values, quality=_feature_quality(features)))


def _market_fallback(market: Mapping[str, Any]) -> str:
    """The documented fallback for an observation with no feature snapshot.

    Only market evidence is read, so the decision can never claim more than a
    directional 20-bar move: it reports PI-04's ``weak_up``/``weak_down`` rather
    than inventing a trend label outside the classifier's vocabulary.
    """
    trend = _number(market.get("return_20"))
    if trend is None:
        trend = _number(market.get("price_vs_sma_20"))
    volatility = _number(market.get("volatility_20"))
    if trend is None and volatility is None:
        return UNCLASSIFIED
    if volatility is not None and volatility >= HIGH_VOLATILITY_THRESHOLD:
        return RegimeLabel.HIGH_VOLATILITY.value
    if (
        volatility is not None
        and volatility <= LOW_VOLATILITY_THRESHOLD
        and (trend is None or abs(trend) < TREND_THRESHOLD)
    ):
        return RegimeLabel.LOW_VOLATILITY.value
    if trend is None:
        return UNCLASSIFIED
    if trend >= TREND_THRESHOLD:
        return RegimeLabel.WEAK_UP.value
    if trend <= -TREND_THRESHOLD:
        return RegimeLabel.WEAK_DOWN.value
    return RegimeLabel.SIDEWAYS.value


def _regime_and_source(
    observation: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """The label and which evidence produced it.

    A feature snapshot always wins: PI-04 is the only regime classifier, so its
    decision is used wherever it has evidence.  The market-only fallback runs
    only when the observation carries no features at all.
    """
    if not isinstance(observation, Mapping):
        return UNCLASSIFIED, SOURCE_NONE
    features = observation.get("features")
    if isinstance(features, Mapping):
        label = _feature_regime(features)
        if label is not None:
            return label, SOURCE_FEATURES
    market = observation.get("market")
    if not isinstance(market, Mapping):
        return UNCLASSIFIED, SOURCE_NONE
    label = _market_fallback(market)
    if label == UNCLASSIFIED:
        return UNCLASSIFIED, SOURCE_NONE
    return label, SOURCE_MARKET


def observation_regime(observation: Mapping[str, Any] | None) -> str:
    """The descriptive regime of ONE PI-13 observation.

    Only this observation is read: its own ``features`` snapshot through PI-04's
    classifier, or — when it has no features — its own ``market`` evidence.  No
    other observation, split, evaluation or aggregate is consulted, so the label
    cannot leak another window's evidence into this one.
    """
    return _regime_and_source(observation)[0]


def regime_breakdown(
    *,
    transitions: Sequence[Any],
    rewards: Sequence[Mapping[str, Any]] = (),
    min_samples: int = MIN_REGIME_SAMPLES,
) -> dict[str, Any]:
    """Group one split's transitions by regime and summarise each group.

    ``transitions`` are PI-14 ``TransitionView`` rows whose ``observation`` is
    the PI-13 observation that produced the decision, so the label always comes
    from evidence available before that decision was taken.
    """
    minimum = max(1, int(min_samples))
    buckets: dict[str, dict[str, Any]] = {}
    sources: dict[str, int] = {}
    for position, view in enumerate(transitions):
        label, source = _regime_and_source(getattr(view, "observation", None))
        sources[source] = sources.get(source, 0) + 1
        bucket = buckets.setdefault(
            label,
            {
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "return_sum": 0.0,
                "return_count": 0,
                "reward_sum": 0.0,
                "reward_count": 0,
                "invalid_actions": 0,
                "label_source": source,
            },
        )
        bucket["samples"] += 1
        net_return = _number(getattr(view, "net_return", None))
        if net_return is not None:
            bucket["return_sum"] += net_return
            bucket["return_count"] += 1
            if net_return > 0:
                bucket["wins"] += 1
            elif net_return < 0:
                bucket["losses"] += 1
        if position < len(rewards):
            payload = rewards[position]
            total = _number(payload.get("total")) if isinstance(payload, Mapping) else None
            if total is not None:
                bucket["reward_sum"] += total
                bucket["reward_count"] += 1
        if str(getattr(view, "execution_status", "") or "").lower() == "invalid":
            bucket["invalid_actions"] += 1
    total_samples = sum(bucket["samples"] for bucket in buckets.values())
    rows: list[dict[str, Any]] = []
    for label in sorted(buckets):
        bucket = buckets[label]
        sufficient = bucket["samples"] >= minimum
        rows.append(
            {
                "regime": label,
                "label_source": bucket["label_source"],
                "samples": bucket["samples"],
                "share": round(bucket["samples"] / total_samples, 6) if total_samples else 0.0,
                "sufficient": sufficient,
                "mean_return": (
                    bucket["return_sum"] / bucket["return_count"]
                    if bucket["return_count"]
                    else None
                ),
                "positive_ratio": (
                    bucket["wins"] / bucket["return_count"] if bucket["return_count"] else None
                ),
                "losses": bucket["losses"],
                "mean_reward": (
                    bucket["reward_sum"] / bucket["reward_count"]
                    if bucket["reward_count"]
                    else None
                ),
                "invalid_actions": bucket["invalid_actions"],
                "conclusion": (
                    ""
                    if sufficient
                    else "insufficient samples; no regime conclusion is drawn"
                ),
            }
        )
    insufficient = [row["regime"] for row in rows if not row["sufficient"]]
    return {
        "available": bool(rows),
        "classifier": REGIME_METHOD_VERSION,
        "source": (
            "PI-04 RegimeDetector over each PI-13 observation's own feature "
            "snapshot; PI-13 observation evidence (market.return_20, "
            "market.volatility_20) only where no features exist"
        ),
        "label_basis": (
            "one label per observation from that observation's own point-in-time "
            "evidence; nothing is aggregated across observations to decide a label"
        ),
        "thresholds": dict(DEFAULT_REGIME_DETECTOR.parameters),
        "fallback": {
            "used_when": "the observation carries no feature snapshot",
            "thresholds": dict(REGIME_THRESHOLDS),
        },
        "label_sources": dict(sorted(sources.items())),
        "min_samples": minimum,
        "samples": total_samples,
        "rows": rows,
        "insufficient": insufficient,
        "cross_split_inference": False,
        "warnings": (
            []
            if not insufficient
            else [
                "regime slices below the minimum sample count are reported as "
                "insufficient, never as evidence of regime-specific performance"
            ]
        ),
        "simulated": True,
        "advisory_only": True,
        "production_authority": False,
    }


__all__ = [
    "HIGH_VOLATILITY_THRESHOLD",
    "LOW_VOLATILITY_THRESHOLD",
    "MIN_REGIME_SAMPLES",
    "REGIME_THRESHOLDS",
    "SOURCE_FEATURES",
    "SOURCE_MARKET",
    "SOURCE_NONE",
    "TREND_THRESHOLD",
    "UNCLASSIFIED",
    "observation_regime",
    "regime_breakdown",
]
