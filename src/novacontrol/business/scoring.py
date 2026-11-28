"""Explainable opportunity scoring (Phase 35).

The score is a weighted average over dimensions that each have an explicit
input. A dimension with no evidence is `None`, appears in ``missing`` and lowers
coverage; it is never replaced by a neutral 0.5 to make the total look complete.
Eligibility is deliberately stricter than the weighted score.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from novacontrol.business.models import (
    CompetitorLandscape,
    CustomerValidation,
    EvidenceKind,
    EvidenceQuality,
    EvidenceRecord,
    FeasibilityAssessment,
    FinancialAnalysis,
    MarketResearch,
    OpportunityScore,
    ScoreDimension,
    TradeStatus,
)

#: Explicit, configurable default weights. They sum to 1.0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "demand_evidence": 0.17,
    "problem_severity": 0.11,
    "market_accessibility": 0.09,
    "competitive_intensity": 0.07,
    "differentiation": 0.08,
    "technical_feasibility": 0.12,
    "financial_feasibility": 0.12,
    "execution_complexity": 0.06,
    "risk_exposure": 0.08,
    "evidence_quality": 0.10,
}

DIMENSION_LABELS = {
    "demand_evidence": "Demand evidence",
    "problem_severity": "Problem severity",
    "market_accessibility": "Market accessibility",
    "competitive_intensity": "Competitive intensity (lower is better)",
    "differentiation": "Differentiation",
    "technical_feasibility": "Technical feasibility",
    "financial_feasibility": "Financial feasibility",
    "execution_complexity": "Execution complexity (lower is better)",
    "risk_exposure": "Risk exposure (lower is better)",
    "evidence_quality": "Evidence quality",
}


def _weighted(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def _measured(evidence: tuple[EvidenceRecord, ...]) -> tuple[EvidenceRecord, ...]:
    # An unsourced estimate is an assumption in disguise: it cannot support a
    # current validation reading even though its quality label is an estimate.
    return tuple(
        row
        for row in evidence
        if row.source is not None
        and row.quality.counts_toward_validation
        and not row.stale()
    )


def _dimension(
    name: str,
    score: float | None,
    *,
    weight: float,
    evidence_count: int = 0,
    missing_reason: str = "",
    rationale: str = "",
) -> ScoreDimension:
    return ScoreDimension(
        name=name,
        weight=weight,
        score=score,
        contribution=(score * weight) if score is not None else None,
        evidence_count=evidence_count,
        missing_reason=missing_reason,
        rationale=rationale,
    )


def score_opportunity(
    *,
    evidence: tuple[EvidenceRecord, ...],
    market: MarketResearch | None,
    customer: CustomerValidation | None,
    competition: CompetitorLandscape | None,
    feasibility: FeasibilityAssessment | None,
    financials: FinancialAnalysis | None,
    target_audience: str,
    inputs: Mapping[str, Any] | None = None,
    weights: Mapping[str, float] | None = None,
    threshold: float = 0.6,
) -> OpportunityScore:
    data = dict(inputs or {})
    active_weights = {
        key: max(0.0, float(value))
        for key, value in (weights or DEFAULT_WEIGHTS).items()
        if key in DEFAULT_WEIGHTS
    }
    if not active_weights or sum(active_weights.values()) <= 0:
        active_weights = dict(DEFAULT_WEIGHTS)
    measured = _measured(evidence)
    dimensions: list[ScoreDimension] = []
    missing: list[str] = []
    disqualifiers: list[str] = []

    # Demand evidence: primary customer records count most, secondary market
    # research next, estimates least. Assumptions never count.
    demand_kinds = {
        EvidenceKind.DEMAND_SIGNAL,
        EvidenceKind.CUSTOMER_INTERVIEW,
        EvidenceKind.CUSTOMER_FEEDBACK,
        EvidenceKind.MARKET_TREND,
        EvidenceKind.MARKET_SIZE,
    }
    demand_rows = tuple(row for row in measured if row.kind in demand_kinds)
    demand_count = len(demand_rows)
    if demand_count:
        primary = sum(1 for row in demand_rows if row.quality is EvidenceQuality.PRIMARY)
        secondary = sum(1 for row in demand_rows if row.quality is EvidenceQuality.SECONDARY)
        estimate = sum(1 for row in demand_rows if row.quality is EvidenceQuality.ESTIMATE)
        demand_score = min(1.0, (primary * 1.0 + secondary * 0.7 + estimate * 0.4) / 3.0)
        dimensions.append(
            _dimension(
                "demand_evidence",
                demand_score,
                weight=active_weights["demand_evidence"],
                evidence_count=demand_count,
                rationale=f"{primary} primary, {secondary} secondary, {estimate} estimate record(s)",
            )
        )
    else:
        dimensions.append(
            _dimension(
                "demand_evidence",
                None,
                weight=active_weights["demand_evidence"],
                missing_reason="no measured demand/customer evidence",
            )
        )
        missing.append("demand_evidence")
        disqualifiers.append("no measured demand evidence")

    severity_values = []
    for row in measured:
        if row.kind in {EvidenceKind.CUSTOMER_INTERVIEW, EvidenceKind.CUSTOMER_FEEDBACK}:
            raw = row.value.get("severity")
            if raw is None or raw == "":
                continue
            try:
                value = float(str(raw))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            severity_values.append(max(0.0, min(1.0, value / 5.0 if value > 1 else value)))
    if severity_values:
        severity_score = sum(severity_values) / len(severity_values)
        dimensions.append(
            _dimension(
                "problem_severity",
                severity_score,
                weight=active_weights["problem_severity"],
                evidence_count=len(severity_values),
                rationale="customer records supplied a 1–5 severity rating",
            )
        )
    else:
        dimensions.append(
            _dimension(
                "problem_severity",
                None,
                weight=active_weights["problem_severity"],
                missing_reason="no customer severity rating was supplied",
            )
        )
        missing.append("problem_severity")

    access_score = None
    if target_audience.strip() and (market and (market.segments or market.demand_indicators)):
        access_score = 0.6
    elif (
        target_audience.strip()
        and customer
        and customer.has_current_primary
        and customer.segments
    ):
        # The target audience alone is an operator input, not researched
        # evidence. Only a current primary customer record can substantiate a
        # segment when no market research exists.
        access_score = 0.5
    if access_score is None:
        dimensions.append(
            _dimension(
                "market_accessibility",
                None,
                weight=active_weights["market_accessibility"],
                missing_reason="no identified segment and target audience together",
            )
        )
        missing.append("market_accessibility")
    else:
        dimensions.append(
            _dimension(
                "market_accessibility",
                access_score,
                weight=active_weights["market_accessibility"],
                rationale="target audience and a researched customer segment are both present",
            )
        )

    if competition is not None and competition.search_complete and competition.competitors:
        count = len([row for row in competition.competitors if not row.get("stale")])
        competitive_score = max(0.1, min(1.0, 1.0 / (1.0 + count)))
        dimensions.append(
            _dimension(
                "competitive_intensity",
                competitive_score,
                weight=active_weights["competitive_intensity"],
                evidence_count=count,
                rationale=f"{count} sourced competitor(s); lower intensity is better",
            )
        )
    else:
        dimensions.append(
            _dimension(
                "competitive_intensity",
                None,
                weight=active_weights["competitive_intensity"],
                missing_reason="competitor search is incomplete or no sourced competitor exists",
            )
        )
        missing.append("competitive_intensity")
        disqualifiers.append("competitor discovery is incomplete")

    differentiation = _weighted(data.get("differentiation"))
    if differentiation is None:
        dimensions.append(
            _dimension(
                "differentiation",
                None,
                weight=active_weights["differentiation"],
                missing_reason="no differentiation judgement was supplied",
            )
        )
        missing.append("differentiation")
    else:
        dimensions.append(
            _dimension(
                "differentiation",
                differentiation,
                weight=active_weights["differentiation"],
                rationale="supplied differentiation score",
            )
        )

    dimension_map = (
        {row.name: row for row in feasibility.dimensions} if feasibility is not None else {}
    )

    def from_feasibility(key: str, name: str, weight_key: str) -> None:
        row = dimension_map.get(key)
        if row is None or row.score is None:
            dimensions.append(
                _dimension(
                    name,
                    None,
                    weight=active_weights[weight_key],
                    missing_reason=(row.missing_reason if row else f"{key} feasibility was not assessed"),
                )
            )
            missing.append(name)
            return
        dimensions.append(
            _dimension(
                name,
                row.score,
                weight=active_weights[weight_key],
                evidence_count=len(row.evidence_ids),
                rationale=row.rationale,
            )
        )

    from_feasibility("technical", "technical_feasibility", "technical_feasibility")
    from_feasibility("financial", "financial_feasibility", "financial_feasibility")
    from_feasibility("execution", "execution_complexity", "execution_complexity")
    from_feasibility("operational", "risk_exposure", "risk_exposure")

    if feasibility is not None and feasibility.dimensions:
        available = [row.score for row in feasibility.dimensions if row.score is not None]
        blockers = len(feasibility.blockers)
        # The operational dimension above is a proxy for operational risk; this
        # explicit calculation adds blockers and incomplete feasibility without
        # hiding either. It only replaces the proxy when a feasibility object is
        # present. With no scored dimension there is no defensible risk score:
        # report it missing instead of inventing a neutral 0.5.
        dimensions = [row for row in dimensions if row.name != "risk_exposure"]
        missing = [row for row in missing if row != "risk_exposure"]
        if available:
            average = sum(available) / len(available)
            risk = max(
                0.0,
                min(1.0, 1.0 - (blockers * 0.3 + max(0.0, 1.0 - average) * 0.5)),
            )
            dimensions.append(
                _dimension(
                    "risk_exposure",
                    risk,
                    weight=active_weights["risk_exposure"],
                    evidence_count=len(available),
                    rationale=f"{blockers} blocker(s); {len(feasibility.gaps)} missing input(s)",
                )
            )
        else:
            dimensions.append(
                _dimension(
                    "risk_exposure",
                    None,
                    weight=active_weights["risk_exposure"],
                    missing_reason="no feasibility dimension has a usable score",
                )
            )
            missing.append("risk_exposure")

    sources = {row.source.id for row in measured if row.source is not None}
    stale_measured_rows = tuple(
        row
        for row in evidence
        if row.stale()
        and row.source is not None
        and row.quality.counts_toward_validation
    )
    stale_measured = bool(stale_measured_rows)
    stale_sources = {row.source.id for row in stale_measured_rows if row.source is not None}
    primary_count = sum(1 for row in measured if row.quality is EvidenceQuality.PRIMARY)
    if measured and sources:
        freshness = 1.0 - min(
            1.0, len(stale_sources) / max(1, len(sources) + len(stale_sources))
        )
        evidence_score = min(
            1.0,
            (min(1.0, len(sources) / 4.0) * 0.45)
            + (min(1.0, primary_count / 2.0) * 0.35)
            + (freshness * 0.20),
        )
        dimensions.append(
            _dimension(
                "evidence_quality",
                evidence_score,
                weight=active_weights["evidence_quality"],
                evidence_count=len(measured),
                rationale=(
                    f"{len(sources)} unique current source(s), {primary_count} primary, "
                    f"{len(stale_sources)} stale source(s)"
                ),
            )
        )
    else:
        dimensions.append(
            _dimension(
                "evidence_quality",
                None,
                weight=active_weights["evidence_quality"],
                missing_reason="no current, sourced evidence; assumptions are excluded",
            )
        )
        missing.append("evidence_quality")
        disqualifiers.append("no current sourced evidence")

    available_weight = sum(row.weight for row in dimensions if row.score is not None)
    total_weight = sum(active_weights.values())
    weighted_sum = sum(row.contribution or 0.0 for row in dimensions)
    weighted_score = weighted_sum / available_weight if available_weight > 0 else 0.0
    coverage = available_weight / total_weight if total_weight > 0 else 0.0
    if coverage < 0.8:
        disqualifiers.append(f"evidence coverage {coverage:.0%} is below the 80% eligibility floor")
    if not (customer and customer.has_current_primary):
        disqualifiers.append("no current primary customer evidence")
    if customer and customer.has_current_primary and not (
        market and (market.demand_indicators or market.segments)
    ):
        disqualifiers.append("no market demand evidence")
    has_measured_market = bool(
        market
        and (
            market.market_size.status is TradeStatus.MEASURED
            or any(
                row.quality.measured
                for row in (*market.demand_indicators, *market.trends)
            )
        )
    )
    if not has_measured_market:
        disqualifiers.append("no current measured market evidence")
    base = next(
        (row for row in (financials.scenarios if financials else ()) if row.name == "base"),
        None,
    )
    if base is None or not base.calculated:
        disqualifiers.append("financial base scenario is incomplete")
    if feasibility is None or not feasibility.complete:
        disqualifiers.append("feasibility assessment is incomplete")
    if stale_measured:
        disqualifiers.append(
            f"{len(stale_sources)} evidence source(s) are stale and excluded from current validation"
        )
    eligible = bool(
        weighted_score >= threshold
        and coverage >= 0.8
        and not disqualifiers
    )
    return OpportunityScore(
        weighted_score=weighted_score,
        evidence_coverage=coverage,
        threshold=threshold,
        eligible=eligible,
        dimensions=tuple(dimensions),
        missing=tuple(dict.fromkeys(missing)),
        disqualifiers=tuple(dict.fromkeys(disqualifiers)),
        weights=active_weights,
        methodology=(
            "Weighted average of available dimensions (0–1 each). Weights are "
            "normalised to the available weight; coverage reports how much of the "
            "full model had evidence. Eligibility requires the score threshold, "
            "≥80% coverage, current primary customer evidence, current measured "
            "market evidence, a completed sourced competitor search, a calculated "
            "base financial scenario, complete feasibility and no stale measured "
            "evidence."
        ),
    )


__all__ = ["DEFAULT_WEIGHTS", "DIMENSION_LABELS", "score_opportunity"]
