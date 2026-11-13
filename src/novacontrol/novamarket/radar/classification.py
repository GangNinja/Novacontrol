"""Explainable importance, and a confidence that cannot impersonate verification.

Importance answers *how much attention this deserves*. It is computed from seven
factors, each with its own contribution recorded, so a consumer can read the
arithmetic rather than trust a label:

| factor | weight | what it measures |
| --- | --- | --- |
| category base | 0.30 | the subcategory's declared significance |
| verification | 0.20 | what the evidence actually supports |
| source reliability | 0.15 | the best publisher backing it |
| recency | 0.10 | how fresh the publication is |
| instrument breadth | 0.10 | how many instruments it touches |
| market-wide | 0.10 | whether the category is market-wide by nature |
| evidence completeness | 0.05 | independent origins behind the claim |

Guards that make the label honest:

* an ``unclassified`` record is always ``LOW`` — it is not a market event;
* an unverified or single-source claim can never be ``CRITICAL``;
* conflicting, retracted or expired events are capped at ``MODERATE``, and a
  retraction is ``LOW``;
* confidence is a separate figure with its own factors, and a state change is
  never inferred from it.

There is no direction, price target, expected return or recommendation field in
this module, so the system cannot express a prediction even by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from novacontrol.novamarket.radar.models import (
    SUBCATEGORY_INDEX,
    UNCLASSIFIED,
    Classification,
    EventMapping,
    EvidenceRecord,
    ExtractedEvent,
    Factor,
    Importance,
    VerificationDecision,
    VerificationState,
    utcnow,
)

#: The one place the weights live. They sum to 1.0; a unit test pins that.
FACTOR_WEIGHTS: dict[str, float] = {
    "category_base": 0.30,
    "verification": 0.20,
    "source_reliability": 0.15,
    "recency": 0.10,
    "instrument_breadth": 0.10,
    "market_wide": 0.10,
    "evidence_completeness": 0.05,
}

#: Verification state -> factor value. Confirmed is 1.0, a rumour is 0.1.
VERIFICATION_WEIGHT: dict[VerificationState, float] = {
    VerificationState.OFFICIALLY_CONFIRMED: 1.0,
    VerificationState.MULTI_SOURCE_CORROBORATED: 0.7,
    VerificationState.SINGLE_SOURCE: 0.35,
    VerificationState.UNVERIFIED: 0.1,
    VerificationState.CONFLICTING_EVIDENCE: 0.15,
    VerificationState.RETRACTED: 0.0,
    VerificationState.EXPIRED: 0.1,
}

BANDS: tuple[tuple[float, Importance], ...] = (
    (75.0, Importance.CRITICAL),
    (55.0, Importance.HIGH),
    (35.0, Importance.MODERATE),
    (0.0, Importance.LOW),
)

_IMPORTANCE_ORDER = {
    Importance.LOW: 0,
    Importance.MODERATE: 1,
    Importance.HIGH: 2,
    Importance.CRITICAL: 3,
}

_RANK_TO_IMPORTANCE = {value: key for key, value in _IMPORTANCE_ORDER.items()}

#: Caps are stated once, so "a rumour cannot be critical" is a rule, not a hope.
_CAPS: dict[VerificationState, Importance] = {
    VerificationState.UNVERIFIED: Importance.HIGH,
    VerificationState.SINGLE_SOURCE: Importance.HIGH,
    VerificationState.CONFLICTING_EVIDENCE: Importance.MODERATE,
    VerificationState.RETRACTED: Importance.LOW,
    VerificationState.EXPIRED: Importance.MODERATE,
}


def recency_value(published_at: datetime | None, now: datetime | None = None) -> float:
    """1.0 for a publication within six hours, decaying to 0.1 after a week."""
    if published_at is None:
        return 0.35
    moment = now or utcnow()
    age = moment - published_at
    if age < timedelta(0):
        # A future-dated publication is a source anomaly, not freshness.
        return 0.5
    hours = age.total_seconds() / 3600
    if hours <= 6:
        return 1.0
    if hours >= 168:
        return 0.1
    return max(0.1, 1.0 - (hours - 6) / 162 * 0.9)


def band(score: float) -> Importance:
    for threshold, importance in BANDS:
        if score >= threshold:
            return importance
    return Importance.LOW


def _cap(importance: Importance, cap: Importance) -> Importance:
    if _IMPORTANCE_ORDER[importance] <= _IMPORTANCE_ORDER[cap]:
        return importance
    return cap


def _best_reliability(evidence: Sequence[EvidenceRecord]) -> tuple[float, str]:
    if not evidence:
        return 0.0, "no evidence"
    best = max(evidence, key=lambda record: record.reliability.weight)
    return best.reliability.weight, best.publisher


def classify(
    *,
    event: ExtractedEvent,
    verification: VerificationDecision,
    evidence: Sequence[EvidenceRecord],
    mappings: Sequence[EventMapping],
    published_at: datetime | None = None,
    now: datetime | None = None,
) -> Classification:
    """Score one event, with every contribution explained."""
    definition = SUBCATEGORY_INDEX.get(event.subcategory, UNCLASSIFIED)
    base = definition.importance_weight
    verification_value = VERIFICATION_WEIGHT[verification.state]
    reliability, publisher = _best_reliability(evidence)
    recency = recency_value(published_at, now)
    breadth = min(len(mappings) / 5.0, 1.0)
    wide = 1.0 if definition.market_wide else 0.0
    completeness = min(verification.independent_sources / 2.0, 1.0)
    if not evidence:
        completeness = 0.0

    values = {
        "category_base": (base, f"{definition.label} ({event.subcategory})"),
        "verification": (
            verification_value,
            f"{verification.state.value} — {verification.reasons[0] if verification.reasons else 'no reason recorded'}",
        ),
        "source_reliability": (reliability, f"best publisher: {publisher}"),
        "recency": (
            recency,
            "published within 6h" if recency == 1.0 else "age-decayed publication",
        ),
        "instrument_breadth": (
            breadth,
            f"{len(mappings)} affected instrument(s)",
        ),
        "market_wide": (
            wide,
            "market-wide category" if wide else "instrument-specific category",
        ),
        "evidence_completeness": (
            completeness,
            f"{verification.independent_sources} independent origin(s)",
        ),
    }

    factors = tuple(
        Factor(
            name=name,
            value=value,
            weight=FACTOR_WEIGHTS[name],
            contribution=FACTOR_WEIGHTS[name] * value * 100.0,
            reason=reason,
        )
        for name, (value, reason) in values.items()
    )
    score = sum(factor.contribution for factor in factors)
    importance = band(score)
    caps: list[str] = []
    if definition.category.value == "unclassified":
        importance = Importance.LOW
        caps.append("unclassified records are never more than LOW")
    else:
        cap = _CAPS.get(verification.state)
        if cap is not None:
            capped = _cap(importance, cap)
            if capped is not importance:
                caps.append(
                    f"capped at {cap.value.upper()} because verification is "
                    f"{verification.state.value}"
                )
            importance = capped

    confidence = _confidence(
        verification=verification,
        reliability=reliability,
        completeness=completeness,
        recency=recency,
        importance=importance,
    )
    top = sorted(factors, key=lambda factor: factor.contribution, reverse=True)[:3]
    explanation = (
        f"importance={importance.value.upper()} (score {score:.1f}/100) from "
        + ", ".join(
            f"{factor.name} {factor.contribution:+.1f}" for factor in top
        )
    )
    if caps:
        explanation += "; " + "; ".join(caps)
    explanation += (
        "; this is an attention classification, not a forecast of price movement"
    )
    return Classification(
        importance=importance,
        score=score,
        confidence=confidence,
        factors=factors,
        explanation=explanation,
    )


def _confidence(
    *,
    verification: VerificationDecision,
    reliability: float,
    completeness: float,
    recency: float,
    importance: Importance,
) -> float:
    """A separate figure: how well-supported the record is, not how big it is."""
    if verification.state is VerificationState.RETRACTED:
        return 0.05
    state_component = VERIFICATION_WEIGHT[verification.state]
    value = (
        0.10
        + 0.40 * state_component
        + 0.20 * reliability
        + 0.20 * completeness
        + 0.10 * recency
    )
    if importance is Importance.CRITICAL:
        # Importance never inflates confidence; if anything the stakes argue for
        # a conservative reading.
        value = min(value, 0.95)
    return max(0.0, min(round(value, 3), 1.0))


__all__ = [
    "BANDS",
    "FACTOR_WEIGHTS",
    "VERIFICATION_WEIGHT",
    "band",
    "classify",
    "recency_value",
]
