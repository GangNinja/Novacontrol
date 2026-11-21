"""Evidence-linked investment thesis generation for PI-19."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import (
    HORIZONS,
    CompanyProfile,
    CorporateEvent,
    EventKind,
    EventOccurrence,
    FinancialAnalysis,
    InvestmentThesis,
    NewsArticle,
    ResearchRequest,
    SectorSnapshot,
    SentimentLabel,
    Stance,
    ValuationResult,
    VerificationStatus,
    clean,
    stable_id,
)


def _ratio(analysis: FinancialAnalysis, name: str) -> float | None:
    return analysis.ratios.values.get(name)


def _factor(
    *,
    factor: str,
    value: float | str | None,
    evidence_ids: Sequence[str],
    rationale: str,
    kind: str = "reported_or_derived_metric",
    unit: str = "ratio",
) -> dict[str, Any]:
    """One thesis factor.

    ``unit`` travels with the value so a reader never has to guess whether a
    number is a ratio, a percentage or an amount in the reporting currency.
    """

    return {
        "factor": factor,
        "value": value,
        "unit": unit,
        "evidence_ids": list(evidence_ids),
        "rationale": rationale,
        "factor_kind": kind,
        "is_ai_interpretation": kind in {"derived_estimate", "ai_interpretation"},
        "is_causal_claim": False,
    }


def _statement_evidence(analysis: FinancialAnalysis) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(item.get("evidence_id")) for item in analysis.statements if item.get("evidence_id")
        )
    )


def _confidence(
    *,
    evidence_ids: Sequence[str],
    total_evidence_ids: Sequence[str],
    claims: Sequence[Mapping[str, Any]],
    forecast_context: Mapping[str, Any],
    analysis: FinancialAnalysis,
    profile: CompanyProfile,
) -> dict[str, Any]:
    verified = [
        row
        for row in claims
        if row.get("status")
        in {VerificationStatus.VERIFIED.value, VerificationStatus.PARTIALLY_VERIFIED.value}
    ]
    conflicts = [
        row for row in claims if row.get("status") == VerificationStatus.CONFLICTING_EVIDENCE.value
    ]
    source_reliability = (
        "high"
        if any(row.get("source_tier") == "primary_official" for row in claims)
        else "medium"
        if verified
        else "low"
    )
    profile_total = len(profile.fields) + len(profile.missing_fields)
    checks_total = 4 + profile_total
    checks_missing = len(analysis.missing) + len(profile.missing_fields)
    completeness = 1.0 - min(1.0, checks_missing / max(1, checks_total))
    return {
        "source_reliability": source_reliability,
        "evidence_strength": (
            "strong" if len(verified) >= 4 and not conflicts else "moderate" if verified else "weak"
        ),
        "forecast_confidence": (
            "reported_with_uncertainty" if forecast_context.get("available") else "not_available"
        ),
        "decision_confidence": (
            "low" if conflicts or not verified else "moderate" if len(verified) < 4 else "bounded"
        ),
        "data_completeness_ratio": round(max(0.0, min(1.0, completeness)), 4),
        "unverified_evidence_count": max(0, len(total_evidence_ids) - len(verified)),
        "conflicting_evidence_count": len(conflicts),
        "combined_probability": None,
        "combined_probability_note": (
            "Source reliability, evidence strength, forecast confidence and decision "
            "confidence are deliberately not collapsed into one probability."
        ),
        "is_calibrated": False,
    }


def build_thesis(
    *,
    request: ResearchRequest,
    profile: CompanyProfile,
    analysis: FinancialAnalysis,
    events: Sequence[CorporateEvent],
    news: Sequence[NewsArticle],
    sector: SectorSnapshot | None,
    peers: Mapping[str, Any],
    valuation: ValuationResult | None,
    forecast_context: Mapping[str, Any],
    portfolio_context: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    current_price: float | None,
    prior_thesis: Mapping[str, Any] | None = None,
) -> InvestmentThesis:
    statement_evidence = _statement_evidence(analysis)
    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    unresolved: list[str] = []
    assumptions: list[str] = []
    if not analysis.statements:
        unresolved.append("No point-in-time financial statements were available.")
    for name, label, positive_threshold, negative_threshold, unit in (
        ("revenue_growth", "revenue growth", 0.05, -0.05, "percent"),
        ("eps_growth", "EPS growth", 0.05, -0.05, "percent"),
        ("operating_margin", "operating margin", 0.15, 0.0, "percent"),
        ("net_profit_margin", "net profit margin", 0.08, 0.0, "percent"),
        ("return_on_equity", "return on equity", 0.15, 0.0, "percent"),
        ("return_on_invested_capital", "return on invested capital", 0.12, 0.0, "percent"),
        ("current_ratio", "current ratio", 1.5, 1.0, "ratio"),
        ("debt_to_equity", "debt-to-equity", 0.8, 1.5, "ratio"),
        ("free_cash_flow", "free cash flow", 0.0, 0.0, "currency"),
    ):
        value = _ratio(analysis, name)
        if value is None:
            continue
        if name == "debt_to_equity":
            is_positive = value < positive_threshold
            is_negative = value > negative_threshold
        elif name in {"current_ratio"}:
            is_positive = value > positive_threshold
            is_negative = value < negative_threshold
        elif name == "free_cash_flow":
            is_positive = value > 0
            is_negative = value < 0
        else:
            is_positive = value > positive_threshold
            is_negative = value < negative_threshold
        target = positive if is_positive else negative if is_negative else None
        if target is not None:
            target.append(
                _factor(
                    factor=label,
                    value=value,
                    evidence_ids=statement_evidence,
                    rationale=(
                        f"{label} is {value * 100:.2f}% in the analyzed period."
                        if unit == "percent"
                        else f"{label} is {value:,.2f} in the analyzed period."
                    ),
                    unit=unit,
                )
            )
    for item in news:
        if item.instruction_risk or item.is_ai_interpretation:
            continue
        if item.verification not in {
            VerificationStatus.VERIFIED.value,
            VerificationStatus.PARTIALLY_VERIFIED.value,
        }:
            continue
        news_evidence = item.evidence_ids or (item.article_id,)
        if item.sentiment == SentimentLabel.POSITIVE.value:
            positive.append(
                _factor(
                    factor="news_sentiment",
                    value=item.headline,
                    evidence_ids=news_evidence,
                    rationale=(
                        "A relevant article was classified positive by the deterministic "
                        "lexicon; this is not a price forecast."
                    ),
                    kind="news_interpretation",
                    unit="text",
                )
            )
        elif item.sentiment == SentimentLabel.NEGATIVE.value:
            negative.append(
                _factor(
                    factor="news_sentiment",
                    value=item.headline,
                    evidence_ids=news_evidence,
                    rationale=(
                        "A relevant article was classified negative by the deterministic "
                        "lexicon; this is not a price forecast."
                    ),
                    kind="news_interpretation",
                    unit="text",
                )
            )
    for event in events:
        if event.instruction_risk or not event.evidence_ids:
            # A thesis factor without a source reference would be an
            # unsupported assertion; keep the event in the event section only.
            continue
        if event.verification not in {
            VerificationStatus.VERIFIED.value,
            VerificationStatus.PARTIALLY_VERIFIED.value,
        }:
            continue
        if event.occurrence == EventOccurrence.CANCELLED.value:
            negative.append(
                _factor(
                    factor="corporate_event_cancelled",
                    value=event.event_type,
                    evidence_ids=event.evidence_ids,
                    rationale=(
                        "The announced event is marked cancelled; announcement is not occurrence."
                    ),
                    kind="event_interpretation",
                    unit="text",
                )
            )
        elif event.event_type in {EventKind.DIVIDEND.value, EventKind.BUYBACK.value}:
            positive.append(
                _factor(
                    factor="capital_return_event",
                    value=event.event_type,
                    evidence_ids=event.evidence_ids,
                    rationale="A verified capital-return event is part of the research context.",
                    kind="event_interpretation",
                    unit="text",
                )
            )
        elif event.event_type in {
            EventKind.LITIGATION.value,
            EventKind.REGULATORY.value,
            EventKind.TRADING_SUSPENSION.value,
        }:
            negative.append(
                _factor(
                    factor="event_risk",
                    value=event.event_type,
                    evidence_ids=event.evidence_ids,
                    rationale=(
                        "A verified event may affect business risk; the business impact "
                        "is not established."
                    ),
                    kind="event_interpretation",
                    unit="text",
                )
            )
    if (
        sector is not None
        and sector.evidence_id
        and sector.verification
        in {
            VerificationStatus.VERIFIED.value,
            VerificationStatus.PARTIALLY_VERIFIED.value,
        }
    ):
        if sector.relative_strength is not None and sector.relative_strength > 0:
            positive.append(
                _factor(
                    factor="sector_relative_strength",
                    value=sector.relative_strength,
                    evidence_ids=(sector.evidence_id,) if sector.evidence_id else (),
                    rationale="The supplied sector snapshot reports positive relative strength.",
                    kind="sector_interpretation",
                    unit="percent",
                )
            )
        elif sector.relative_strength is not None and sector.relative_strength < 0:
            negative.append(
                _factor(
                    factor="sector_relative_strength",
                    value=sector.relative_strength,
                    evidence_ids=(sector.evidence_id,) if sector.evidence_id else (),
                    rationale="The supplied sector snapshot reports negative relative strength.",
                    kind="sector_interpretation",
                    unit="percent",
                )
            )
    if (
        valuation is not None
        and valuation.estimate is not None
        and current_price is not None
        and current_price > 0
    ):
        if valuation.estimate > current_price * 1.1:
            positive.append(
                _factor(
                    factor="derived_valuation_range",
                    value=valuation.estimate,
                    evidence_ids=statement_evidence,
                    rationale=(
                        "The model-derived per-share estimate is above the current price; "
                        "this is an assumption-driven estimate, not a verified fair value."
                    ),
                    kind="derived_estimate",
                    unit="currency",
                )
            )
        elif valuation.estimate < current_price * 0.9:
            negative.append(
                _factor(
                    factor="derived_valuation_range",
                    value=valuation.estimate,
                    evidence_ids=statement_evidence,
                    rationale=(
                        "The model-derived per-share estimate is below the current price; "
                        "this is an assumption-driven estimate, not a verified fair value."
                    ),
                    kind="derived_estimate",
                    unit="currency",
                )
            )
    verified_ids = {
        str(row.get("evidence_id"))
        for row in claims
        if row.get("status")
        in {VerificationStatus.VERIFIED.value, VerificationStatus.PARTIALLY_VERIFIED.value}
    }
    contradictory_ids = {
        str(row.get("evidence_id"))
        for row in claims
        if row.get("status") == VerificationStatus.CONFLICTING_EVIDENCE.value
    }
    for row in claims:
        if row.get("status") == VerificationStatus.CONFLICTING_EVIDENCE.value:
            unresolved.append(f"Conflicting evidence: {row.get('claim')}")
    if analysis.missing:
        unresolved.append("Missing reported inputs: " + ", ".join(analysis.missing))
    if confidence_needs_more(analysis, claims):
        unresolved.append("The evidence base is not sufficient for a directional stance.")
    score = len(positive) - len(negative)
    if not verified_ids and not statement_evidence:
        stance = Stance.INSUFFICIENT_EVIDENCE.value
    elif contradictory_ids or (any(item.sentiment == SentimentLabel.MIXED.value for item in news)):
        stance = Stance.UNCERTAIN.value
    elif score >= 2 and len(negative) <= len(positive) + 1:
        stance = Stance.BULLISH.value
    elif score <= -2 and len(positive) <= len(negative) + 1:
        stance = Stance.BEARISH.value
    elif score == 0:
        stance = Stance.NEUTRAL.value
    else:
        stance = Stance.UNCERTAIN.value
    if stance == Stance.BULLISH.value and negative:
        unresolved.append("Counter-evidence exists; the bullish stance is not one-sided.")
    if stance == Stance.BEARISH.value and positive:
        unresolved.append("Counter-evidence exists; the bearish stance is not one-sided.")
    if not analysis.statements:
        assumptions.append(
            "No financial statements were available; the stance relies on profile, "
            "event, news and sector evidence."
        )
    assumptions.append(
        "Investment horizon is an analytical label, not a guarantee that evidence "
        "remains valid for that period."
    )
    if forecast_context.get("available"):
        assumptions.append(
            "Forecasts are separate evidence; their uncertainty and model "
            "disagreement are reported, not converted to confidence."
        )
    else:
        assumptions.append(
            "No point-in-time forecast was supplied; no forecast direction is inferred."
        )
    portfolio_context = {
        **dict(portfolio_context),
        "does_not_change_allocation": True,
        "requires_nm08": True,
        "requires_pi12": True,
    }
    conclusion = {
        Stance.BULLISH.value: (
            "Evidence leans positive, but the stance is not an execution authorization."
        ),
        Stance.BEARISH.value: (
            "Evidence leans negative, but the stance is not an execution authorization."
        ),
        Stance.NEUTRAL.value: (
            "The available evidence is broadly balanced; no directional execution signal is issued."
        ),
        Stance.UNCERTAIN.value: (
            "Evidence is mixed, conflicting or incomplete; the research cannot "
            "justify a directional stance."
        ),
        Stance.INSUFFICIENT_EVIDENCE.value: (
            "Evidence is insufficient for an investment conclusion."
        ),
    }[stance]
    confidence = _confidence(
        evidence_ids=tuple(verified_ids),
        total_evidence_ids=tuple(
            str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")
        ),
        claims=claims,
        forecast_context=forecast_context,
        analysis=analysis,
        profile=profile,
    )
    change_reason: list[str] = []
    parent_id = ""
    version = request.version
    if prior_thesis:
        parent_id = clean(prior_thesis.get("thesis_id"), limit=80)
        previous_stance = prior_thesis.get("stance")
        if previous_stance and previous_stance != stance:
            change_reason.append(f"stance changed from {previous_stance} to {stance}")
        previous_pos = {
            str(item["factor"])
            for item in prior_thesis.get("positive_factors", [])
            if isinstance(item, Mapping) and item.get("factor")
        }
        new_pos = {str(item["factor"]) for item in positive}
        added = sorted(new_pos - previous_pos)
        removed = sorted(previous_pos - new_pos)
        if added:
            change_reason.append("new positive factors: " + ", ".join(added))
        if removed:
            change_reason.append("removed positive factors: " + ", ".join(removed))
        previous_evidence = {str(item) for item in prior_thesis.get("supporting_evidence", [])}
        new_evidence = sorted(verified_ids - previous_evidence)
        if new_evidence:
            change_reason.append(f"{len(new_evidence)} new supporting evidence record(s)")
    if not change_reason:
        change_reason.append("initial research or no material thesis change")
    thesis_id = stable_id(
        "pth_",
        request.instrument_id,
        request.horizon,
        stance,
        request.as_of.isoformat(),
        version,
    )
    horizon_group = HORIZONS[request.horizon]["group"]
    horizon_focus = {
        "intraday": (
            "Prioritize recent market events, liquidity and short-term uncertainty; "
            "slow financial statements cannot establish a five-minute direction."
        ),
        "swing": (
            "Prioritize near-term catalysts, earnings events, momentum context and "
            "event timing over long-cycle valuation precision."
        ),
        "long_term": (
            "Prioritize financial health, cash generation, valuation and business "
            "durability; intraday price noise is not a thesis driver."
        ),
    }[horizon_group]
    return InvestmentThesis(
        thesis_id=thesis_id,
        instrument_id=request.instrument_id,
        horizon=request.horizon,
        stance=stance,
        overview={
            "name": profile.name,
            "symbol": profile.symbol,
            "exchange": profile.exchange,
            "sector": profile.sector,
            "industry": profile.industry,
            "description": profile.description,
            "as_of": profile.as_of.isoformat(),
            "horizon": request.horizon,
            "horizon_group": horizon_group,
            "horizon_focus": horizon_focus,
            "horizon_caveat": (
                "The horizon is an analytical scope, not a forecast guarantee or an "
                "execution authorization."
            ),
        },
        positive_factors=tuple(positive),
        negative_factors=tuple(negative),
        forecast_context=dict(forecast_context),
        portfolio_context=portfolio_context,
        conclusion=conclusion,
        supporting_evidence=tuple(sorted(verified_ids)),
        contradictory_evidence=tuple(sorted(contradictory_ids)),
        assumptions=tuple(dict.fromkeys(assumptions)),
        unresolved_questions=tuple(dict.fromkeys(unresolved)),
        confidence=confidence,
        version=version,
        parent_thesis_id=parent_id,
        change_reason=tuple(change_reason),
    )


def confidence_needs_more(
    analysis: FinancialAnalysis,
    claims: Sequence[Mapping[str, Any]],
) -> bool:
    verified = [
        row
        for row in claims
        if row.get("status")
        in {VerificationStatus.VERIFIED.value, VerificationStatus.PARTIALLY_VERIFIED.value}
    ]
    return len(verified) < 2 and len(analysis.statements) < 2
