"""Translate recorded evidence into research, validation, scoring and reports.

This module is deterministic and model-free. Explore supplies sources; a person
supplies interviews, estimates and assumptions. The analysis never turns a
missing source into a fact, never treats an assumption as customer evidence and
never lets a numerical score override an evidence gap.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from novacontrol.business.models import (
    CompetitorLandscape,
    CustomerValidation,
    EvidenceKind,
    EvidenceQuality,
    EvidenceRecord,
    Experiment,
    FeasibilityAssessment,
    FinancialAnalysis,
    Finding,
    MarketResearch,
    MarketSize,
    Opportunity,
    OpportunityScore,
    ReportSection,
    TradeStatus,
    ValidationReport,
    new_id,
    utcnow,
)

MARKET_EVIDENCE = {
    EvidenceKind.MARKET_SIZE,
    EvidenceKind.MARKET_TREND,
    EvidenceKind.DEMAND_SIGNAL,
}
CUSTOMER_EVIDENCE = {
    EvidenceKind.CUSTOMER_INTERVIEW,
    EvidenceKind.CUSTOMER_FEEDBACK,
    EvidenceKind.DEMAND_SIGNAL,
}


def _finding(evidence: EvidenceRecord, text: str | None = None) -> Finding:
    return Finding(
        text=text or evidence.claim,
        source_ids=(evidence.source.id,) if evidence.source else (),
        quality=evidence.quality,
        stale=evidence.stale(),
    )


def _source_count(evidence: Sequence[EvidenceRecord]) -> int:
    return len({row.source.id for row in evidence if row.source is not None})


def _current_sourced(evidence: Sequence[EvidenceRecord]) -> tuple[EvidenceRecord, ...]:
    """Records that can support a current verdict: sourced, fresh, non-assumption."""
    return tuple(
        row
        for row in evidence
        if row.source is not None
        and not row.stale()
        and row.quality.counts_toward_validation
    )


def _has_measured_demand(market: MarketResearch | None) -> bool:
    if market is None:
        return False
    if market.market_size.status is TradeStatus.MEASURED:
        return True
    return any(
        row.quality.measured for row in (*market.demand_indicators, *market.trends)
    )


def build_market_research(evidence: tuple[EvidenceRecord, ...]) -> MarketResearch:
    # A stale source or an assumption is retained in the evidence viewer but is
    # not presented as current market research. Estimates remain labelled as
    # estimates, never as measured data.
    measured = _current_sourced(tuple(row for row in evidence if row.kind in MARKET_EVIDENCE))
    market_rows = tuple(
        row
        for row in measured
        if row.kind == EvidenceKind.MARKET_SIZE
        and row.quality is not EvidenceQuality.ASSUMPTION
        and row.source is not None
    )
    size = MarketSize(value=None, status=TradeStatus.UNAVAILABLE)
    if market_rows:
        chosen = market_rows[-1]
        raw_value = chosen.value.get("value", chosen.value.get("amount"))
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None and not math.isfinite(numeric):
            numeric = None
        if numeric is not None:
            try:
                year = int(chosen.value["year"]) if chosen.value.get("year") else None
            except (TypeError, ValueError):
                # A malformed year must not crash assessment or become a made-up
                # date; leave it unknown and keep the sourced value visible.
                year = None
            size = MarketSize(
                value=numeric,
                currency=str(chosen.value.get("currency", "USD") or "USD"),
                year=year,
                status=(
                    TradeStatus.MEASURED
                    if chosen.quality in {EvidenceQuality.PRIMARY, EvidenceQuality.SECONDARY}
                    else TradeStatus.ESTIMATED
                ),
                source_ids=(chosen.source.id,) if chosen.source else (),
                method=str(chosen.value.get("method", "supplied market-size record")),
                note=chosen.claim,
            )
    return MarketResearch(
        market_size=size,
        segments=tuple(
            _finding(row)
            for row in measured
            if row.kind == EvidenceKind.DEMAND_SIGNAL
            and row.value.get("segment")
        ),
        demand_indicators=tuple(
            _finding(row)
            for row in measured
            if row.kind in {EvidenceKind.DEMAND_SIGNAL, EvidenceKind.MARKET_TREND}
        ),
        pain_points=tuple(
            _finding(row)
            for row in measured
            if row.kind == EvidenceKind.DEMAND_SIGNAL and row.value.get("pain_point")
        ),
        trends=tuple(
            _finding(row) for row in measured if row.kind == EvidenceKind.MARKET_TREND
        ),
        growth_indicators=tuple(
            _finding(row)
            for row in measured
            if row.kind == EvidenceKind.MARKET_TREND and row.value.get("growth")
        ),
        regulatory_considerations=tuple(
            _finding(row)
            for row in _current_sourced(evidence)
            if row.kind == EvidenceKind.REGULATORY
        ),
        geography=str(
            next((row.value.get("geography") for row in measured if row.value.get("geography")), "")
        ),
        maturity=str(
            next((row.value.get("maturity") for row in measured if row.value.get("maturity")), "")
        ),
        source_count=_source_count(measured),
        stale_source_count=sum(
            1 for row in evidence if row.kind in MARKET_EVIDENCE and row.stale()
        ),
        note=(
            "Market size is unavailable; no current sourced figure was supplied."
            if size.status is TradeStatus.UNAVAILABLE
            else (
                "Market size is a supplied estimate, not a measured figure."
                if size.status is TradeStatus.ESTIMATED
                else "Market size is a supplied sourced figure; it is not an estimate made by this engine."
            )
        ),
    )


def build_customer_validation(
    evidence: tuple[EvidenceRecord, ...], target_audience: str
) -> CustomerValidation:
    all_customer = tuple(row for row in evidence if row.kind in CUSTOMER_EVIDENCE)
    # Only current, sourced, non-assumption records contribute to the current
    # validation reading. Historical/stale records remain in primary_evidence_ids
    # so the report and UI can show that they exist rather than pretending they
    # were never supplied.
    customer_rows = _current_sourced(all_customer)
    primary = tuple(
        row
        for row in all_customer
        if row.quality is EvidenceQuality.PRIMARY
        and row.kind in {EvidenceKind.CUSTOMER_INTERVIEW, EvidenceKind.CUSTOMER_FEEDBACK}
    )
    current_primary = tuple(
        row
        for row in customer_rows
        if row.quality is EvidenceQuality.PRIMARY
        and row.kind in {EvidenceKind.CUSTOMER_INTERVIEW, EvidenceKind.CUSTOMER_FEEDBACK}
    )
    secondary = tuple(
        row for row in customer_rows if row.quality is EvidenceQuality.SECONDARY
    )
    interviews = tuple(row for row in customer_rows if row.kind == EvidenceKind.CUSTOMER_INTERVIEW)
    feedback = tuple(row for row in customer_rows if row.kind == EvidenceKind.CUSTOMER_FEEDBACK)
    segments = tuple(
        dict.fromkeys(
            str(row.value.get("segment"))
            for row in customer_rows
            if row.value.get("segment")
        )
    ) or ((target_audience,) if target_audience.strip() else ())
    severity = tuple(
        _finding(row, f"Severity {row.value.get('severity')}/5 — {row.claim}")
        for row in customer_rows
        if row.value.get("severity") is not None
    )
    frequency = tuple(
        _finding(row, f"Frequency: {row.value.get('frequency')} — {row.claim}")
        for row in customer_rows
        if row.value.get("frequency")
    )
    alternatives = tuple(
        _finding(row, f"Alternative: {row.value.get('alternative')} — {row.claim}")
        for row in customer_rows
        if row.value.get("alternative") or row.value.get("alternatives")
    )
    wtp = tuple(
        _finding(
            row,
            f"Willingness to pay: {row.value.get('willingness_to_pay')} — {row.claim}",
        )
        for row in customer_rows
        if row.value.get("willingness_to_pay")
    )
    unmet = tuple(
        _finding(row, str(row.value.get("unmet_demand") or row.claim))
        for row in customer_rows
        if row.value.get("unmet_demand") or row.kind in CUSTOMER_EVIDENCE
    )
    assumptions = tuple(
        row.claim for row in evidence if row.quality is EvidenceQuality.ASSUMPTION
    )
    return CustomerValidation(
        segments=segments,
        severity=severity,
        frequency=frequency,
        alternatives=alternatives,
        willingness_to_pay=wtp,
        unmet_demand=unmet,
        interview_count=len(interviews),
        feedback_count=len(feedback),
        primary_evidence_ids=tuple(row.id for row in primary),
        current_primary_evidence_ids=tuple(row.id for row in current_primary),
        secondary_evidence_ids=tuple(row.id for row in secondary),
        assumptions=assumptions,
        note=(
            "Current primary customer evidence exists; it was supplied by the named source, "
            "not generated by an agent."
            if current_primary
            else (
                "Primary customer evidence exists but is outside the 90-day freshness window; "
                "it is historical, not current validation."
                if primary
                else "No primary customer interview, survey or feedback record was supplied; "
                "secondary demand evidence is labelled separately."
            )
        ),
        gap=(
            ""
            if current_primary
            else (
                "Refresh the stale primary customer evidence before claiming validated demand."
                if primary
                else "Run real customer interviews or collect feedback before claiming validated demand."
            )
        ),
    )


def build_competition(evidence: tuple[EvidenceRecord, ...], query: str = "") -> CompetitorLandscape:
    rows = tuple(row for row in evidence if row.kind == EvidenceKind.COMPETITOR)
    # An estimate is not a verifiable competitor record. Only current primary or
    # secondary evidence with a source can make the search count as complete.
    sourced = tuple(
        row
        for row in rows
        if row.source is not None and row.quality.measured and not row.stale()
    )
    competitors: list[dict[str, Any]] = []
    for row in sourced:
        value = dict(row.value)
        competitors.append(
            {
                "name": str(value.get("name") or row.claim[:120]),
                "offering": str(value.get("offering", row.claim)),
                "pricing": value.get("pricing"),
                "target_customers": value.get("target_customers", ""),
                "features": list(value.get("features") or []),
                "distribution": list(value.get("distribution") or []),
                "strengths": list(value.get("strengths") or []),
                "weaknesses": list(value.get("weaknesses") or []),
                "positioning": str(value.get("positioning", "")),
                "source_url": row.source.url if row.source else "",
                "source_title": row.source.title if row.source else "",
                "evidence_id": row.id,
                "verified_source": bool(row.source and row.source.url),
                "stale": row.stale(),
            }
        )
    complete = bool(sourced)
    gaps: list[str] = []
    if not complete:
        gaps.append(
            "Competitor discovery is incomplete: no current, verifiable competitor or alternative "
            "has been recorded. This is not evidence that no competitors exist."
        )
    return CompetitorLandscape(
        competitors=tuple(competitors),
        search_complete=complete,
        query=query,
        note=(
            f"{len(competitors)} sourced competitor/alternative record(s)."
            if complete
            else "No competitor search result was recorded; the search must be treated as incomplete."
        ),
        gaps=tuple(gaps),
    )


def _experiment(
    *,
    title: str,
    hypothesis: str,
    objective: str,
    audience: str,
    resources: Sequence[str],
    cost: float | None,
    currency: str,
    duration: int | None,
    success: Sequence[str],
    failure: Sequence[str],
    measurement: str,
    approval: bool,
    expected: Sequence[str],
) -> Experiment:
    return Experiment(
        id=new_id("exp"),
        title=title,
        hypothesis=hypothesis,
        objective=objective,
        target_audience=audience,
        required_resources=tuple(resources),
        estimated_cost=cost,
        currency=currency,
        duration_days=duration,
        success_criteria=tuple(success),
        failure_criteria=tuple(failure),
        measurement_method=measurement,
        requires_approval=approval,
        expected_evidence=tuple(expected),
    )


def plan_experiments(
    *,
    opportunity: Opportunity,
    gaps: Sequence[str],
    currency: str = "USD",
) -> tuple[Experiment, ...]:
    """Build practical experiments from the actual gaps, never external actions."""
    audience = opportunity.target_audience or "the identified customer segment"
    rows: list[Experiment] = []
    customer = opportunity.customer
    competition = opportunity.competition
    financials = opportunity.financials
    base = next(
        (row for row in (financials.scenarios if financials else ()) if row.name == "base"),
        None,
    )
    if customer is None or not customer.has_current_primary:
        rows.append(
            _experiment(
                title="Customer problem interviews",
                hypothesis=f"{audience} experience the stated problem frequently enough to seek a solution.",
                objective="Establish primary problem severity, frequency, alternatives and willingness to pay.",
                audience=audience,
                resources=("interview guide", "5–10 consenting participants"),
                cost=0.0,
                currency=currency,
                duration=14,
                success=("At least 5 interviews completed", "At least 3 participants describe the problem unprompted"),
                failure=("Fewer than 3 participants recognise the problem", "No participant reports a workaround or cost"),
                measurement="Count independent participants and record verbatim notes; no synthesized interviews.",
                approval=True,
                expected=("primary customer interviews", "severity/frequency ratings", "willingness-to-pay signals"),
            )
        )
    if not _has_measured_demand(opportunity.market):
        rows.append(
            _experiment(
                title="Landing-page demand test",
                hypothesis=f"{audience} will take a measurable action for the proposed solution.",
                objective="Measure real demand before building the full product.",
                audience=audience,
                resources=("single-page site", "analytics", "waitlist or contact form"),
                cost=100.0,
                currency=currency,
                duration=14,
                success=("At least 20 qualified visitors", "At least 5% join the waitlist or request a trial"),
                failure=("Fewer than 10 qualified visitors arrive", "No measurable conversion after 14 days"),
                measurement="Visitors, qualified visitors, waitlist conversions and source attribution.",
                approval=True,
                expected=("demand signal", "conversion evidence", "channel cost data"),
            )
        )
    if competition is None or not competition.search_complete:
        rows.append(
            _experiment(
                title="Competitor and alternative pricing scan",
                hypothesis="At least one existing product or manual alternative serves this need.",
                objective="Complete the competitor landscape and record verifiable pricing.",
                audience="market participants",
                resources=("public pricing pages", "review sites", "documented search queries"),
                cost=0.0,
                currency=currency,
                duration=7,
                success=("At least one sourced competitor or alternative recorded", "Pricing evidence captured where public"),
                failure=("Search remains incomplete after the planned queries",),
                measurement="Record source URL, retrieval time and excerpt for every competitor claim.",
                approval=True,
                expected=("sourced competitor evidence", "pricing evidence where public"),
            )
        )
    if base is None or not base.calculated:
        rows.append(
            _experiment(
                title="Willingness-to-pay and unit-economics test",
                hypothesis="A specific price can cover variable cost and acquisition cost.",
                objective="Replace missing financial assumptions with measured or explicitly stated inputs.",
                audience=audience,
                resources=("pricing interview script", "supplier quotes", "hosting quote"),
                cost=0.0,
                currency=currency,
                duration=10,
                success=("Price and cost assumptions recorded with sources", "Positive contribution margin"),
                failure=("No viable price point above measured variable cost",),
                measurement="Record stated prices, supplier quotes, and the resulting contribution margin.",
                approval=True,
                expected=("pricing evidence", "cost evidence", "financial assumptions"),
            )
        )
    if opportunity.feasibility is None or not opportunity.feasibility.complete:
        rows.append(
            _experiment(
                title="Technical feasibility spike",
                hypothesis="A bounded prototype can deliver the core solution within the measured hardware budget.",
                objective="Test the riskiest technical assumption with a disposable prototype.",
                audience="internal engineering",
                resources=("existing NovaControl runtime", "measured RAM/CPU", "time-boxed prototype"),
                cost=0.0,
                currency=currency,
                duration=10,
                success=("Prototype completes the core workflow", "Memory and CPU stay inside the measured budget"),
                failure=("Core workflow cannot run within the declared resource limits",),
                measurement="Measured latency, peak memory, and a pass/fail on the core workflow.",
                approval=False,
                expected=("technical evidence", "resource measurements"),
            )
        )
    if opportunity.feasibility and opportunity.feasibility.blockers:
        rows.append(
            _experiment(
                title="Regulatory and dependency review",
                hypothesis="The identified blockers can be resolved or bounded before launch.",
                objective="Obtain an explicit legal/regulatory and dependency-risk review.",
                audience="operator and qualified adviser",
                resources=("regulatory checklist", "dependency inventory"),
                cost=None,
                currency=currency,
                duration=14,
                success=("Every blocker has an owner and disposition",),
                failure=("A blocker remains unresolved with no compliant path",),
                measurement="Signed review notes and a blocker disposition table.",
                approval=True,
                expected=("regulatory evidence", "dependency risk evidence"),
            )
        )
    if not rows:
        rows.append(
            _experiment(
                title="Paid pilot confirmation",
                hypothesis="A small group will pay for the proposed solution under realistic terms.",
                objective="Confirm willingness to pay and operational delivery before scaling.",
                audience=audience,
                resources=("pilot offer", "support process", "payment terms"),
                cost=None,
                currency=currency,
                duration=30,
                success=("At least 3 paying pilot customers", "Delivery cost remains within the modelled range"),
                failure=("No paying pilot customer", "Delivery cost exceeds the modelled contribution"),
                measurement="Signed pilot agreements, collected revenue and measured delivery cost.",
                approval=True,
                expected=("actual purchase evidence", "delivery cost evidence"),
            )
        )
    return tuple(rows)


def evidence_gaps(
    *,
    opportunity: Opportunity,
    score: OpportunityScore | None,
    feasibility: FeasibilityAssessment | None,
    financials: FinancialAnalysis | None,
) -> tuple[str, ...]:
    gaps: list[str] = []
    evidence = opportunity.evidence
    measured = tuple(
        row
        for row in evidence
        if row.quality is not EvidenceQuality.ASSUMPTION and row.source is not None and not row.stale()
    )
    if not measured:
        gaps.append("No current, sourced evidence has been recorded.")
    if not any(row.kind in MARKET_EVIDENCE for row in measured):
        gaps.append("Market demand evidence is missing; no market size or demand indicator is established.")
    if not (opportunity.customer and opportunity.customer.has_current_primary):
        gaps.append(
            "No current primary customer interview, survey or feedback record has been supplied."
        )
    if not (opportunity.competition and opportunity.competition.search_complete):
        gaps.append(
            "Competitor discovery is incomplete; an incomplete search is not proof that no competitors exist."
        )
    if not _has_measured_demand(opportunity.market):
        gaps.append(
            "No current measured market demand evidence exists; estimates and assumptions cannot support validation."
        )
    base = next(
        (row for row in (financials.scenarios if financials else ()) if row.name == "base"),
        None,
    )
    if base is None or not base.calculated:
        gaps.append("The base financial scenario is incomplete; revenue and margin cannot be calculated.")
    if feasibility is None:
        gaps.append("No feasibility assessment has been completed.")
    else:
        gaps.extend(feasibility.gaps)
    stale = sum(1 for row in evidence if row.source is not None and row.stale())
    if stale:
        gaps.append(
            f"{stale} sourced evidence record(s) are older than the 90-day freshness window "
            "and are not current market evidence."
        )
    if score is not None:
        gaps.extend(score.disqualifiers)
    return tuple(dict.fromkeys(gaps))


def decide_verdict(
    *,
    opportunity: Opportunity,
    score: OpportunityScore | None,
    feasibility: FeasibilityAssessment | None,
    financials: FinancialAnalysis | None,
) -> tuple[str, str, tuple[str, ...]]:
    gaps = evidence_gaps(
        opportunity=opportunity,
        score=score,
        feasibility=feasibility,
        financials=financials,
    )
    base = next(
        (row for row in (financials.scenarios if financials else ()) if row.name == "base"),
        None,
    )
    measured = tuple(
        row
        for row in opportunity.evidence
        if row.quality is not EvidenceQuality.ASSUMPTION
        and row.source is not None
        and not row.stale()
    )
    technical = next(
        (row for row in (feasibility.dimensions if feasibility else ()) if row.name == "technical"),
        None,
    )
    blockers = list(feasibility.blockers if feasibility else ())
    if base is not None and base.calculated and (base.contribution_margin or 0) <= 0:
        blockers.append("the base scenario has no positive contribution margin")
    if technical is not None and technical.score is not None and technical.score < 0.2:
        blockers.append("technical feasibility is too low for this machine or requirement")
    if blockers:
        return (
            "NOT_VIABLE",
            "Recorded evidence contains a disqualifying constraint: " + "; ".join(dict.fromkeys(blockers)),
            gaps,
        )
    if not measured or len(measured) < 2:
        return (
            "INSUFFICIENT_EVIDENCE",
            "There is not enough current, sourced evidence to support a business verdict.",
            gaps,
        )
    if score is None:
        return (
            "INSUFFICIENT_EVIDENCE",
            "No opportunity score could be computed.",
            gaps,
        )
    if score.eligible:
        return (
            "VALIDATED",
            (
                "Current primary customer evidence, current measured market evidence, a "
                "sourced competitor search, a calculated base financial scenario, complete "
                "feasibility and no stale measured evidence support the score above the "
                "configured eligibility floor."
            ),
            gaps,
        )
    current_primary = any(
        row.quality is EvidenceQuality.PRIMARY
        and row.kind in {EvidenceKind.CUSTOMER_INTERVIEW, EvidenceKind.CUSTOMER_FEEDBACK}
        and row.source is not None
        and not row.stale()
        for row in opportunity.evidence
    )
    current_market = any(
        row.kind in MARKET_EVIDENCE
        and row.quality.measured
        and row.source is not None
        and not row.stale()
        for row in opportunity.evidence
    )
    current_competitor = any(
        row.kind is EvidenceKind.COMPETITOR
        and row.quality.measured
        and row.source is not None
        and not row.stale()
        for row in opportunity.evidence
    )
    any_stale = any(
        row.stale()
        and row.source is not None
        and row.quality.counts_toward_validation
        for row in opportunity.evidence
    )
    has_financial = bool(base and base.calculated)
    if (
        current_primary
        and current_market
        and current_competitor
        and has_financial
        and not any_stale
        and feasibility is not None
        and feasibility.complete
        and score.weighted_score >= score.threshold
    ):
        return (
            "CONDITIONALLY_VALIDATED",
            (
                "The core evidence is present and the score clears the threshold, but one "
                "or more non-critical dimensions or coverage conditions still need completion."
            ),
            gaps,
        )
    return (
        "REQUIRES_FURTHER_RESEARCH",
        "Evidence exists, but the required gaps prevent a validated verdict.",
        gaps,
    )


def _safe(text: str, limit: int = 900) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _source_lines(evidence: Sequence[EvidenceRecord]) -> str:
    rows = []
    seen: set[str] = set()
    for row in evidence:
        if row.source is None or row.source.id in seen:
            continue
        seen.add(row.source.id)
        rows.append(f"{row.source.title or 'source'} — {row.source.url or row.source.source_type}")
    return "; ".join(rows) if rows else "No external source."


def build_report(
    *,
    opportunity: Opportunity,
    market: MarketResearch,
    customer: CustomerValidation,
    competition: CompetitorLandscape,
    feasibility: FeasibilityAssessment,
    financials: FinancialAnalysis,
    score: OpportunityScore,
    experiments: Sequence[Experiment],
    verdict: str,
    rationale: str,
    gaps: Sequence[str],
    next_steps: Sequence[str],
    version: int = 1,
) -> ValidationReport:
    evidence = opportunity.evidence
    source_count = _source_count(evidence)
    primary_count = sum(1 for row in evidence if row.quality is EvidenceQuality.PRIMARY)
    current_primary_count = sum(
        1
        for row in evidence
        if row.quality is EvidenceQuality.PRIMARY
        and row.source is not None
        and not row.stale()
    )
    stale_count = sum(1 for row in evidence if row.stale())
    if (
        market.market_size.status is TradeStatus.UNAVAILABLE
        or market.market_size.value is None
    ):
        size_text = "Market size unavailable (no current sourced figure supplied)."
    else:
        size_text = (
            f"Market size {market.market_size.currency} {market.market_size.value:,.0f}"
            f" ({market.market_size.year or 'year not supplied'}; "
            f"{market.market_size.status.value})."
        )
    measured_market_finding = any(
        row.quality.measured
        for row in (*market.demand_indicators, *market.trends)
    )
    market_status = (
        "measured"
        if market.market_size.status is TradeStatus.MEASURED or measured_market_finding
        else "estimated"
        if market.source_count
        else "unavailable"
    )
    competitor_text = (
        f"{len(competition.competitors)} sourced competitor/alternative record(s); "
        "search marked complete for the sources recorded."
        if competition.search_complete
        else "Competitor discovery is incomplete; no sourced competitor record exists."
    )
    tests = "; ".join(f"{row.title} ({row.status})" for row in experiments) or "No experiments planned."
    gaps_text = "; ".join(gaps) if gaps else "No material evidence gaps were recorded."
    next_text = "; ".join(next_steps) if next_steps else "Archive or monitor the opportunity."
    sections = (
        ReportSection(
            "executive_summary",
            "1. Executive summary",
            f"{opportunity.title}: {verdict}. {rationale} Score {score.weighted_score:.2f} "
            f"against threshold {score.threshold:.2f}; evidence coverage {score.evidence_coverage:.0%}; "
            f"{source_count} source(s), {primary_count} primary record(s) "
            f"({current_primary_count} current), {stale_count} stale.",
            tuple(row.source.id for row in evidence if row.source),
        ),
        ReportSection(
            "opportunity_description",
            "2. Opportunity description",
            _safe(opportunity.description or opportunity.proposed_solution),
        ),
        ReportSection(
            "customer_problem",
            "3. Customer problem",
            _safe(opportunity.customer_problem),
        ),
        ReportSection(
            "target_audience",
            "4. Target audience",
            _safe(opportunity.target_audience or "Target audience not specified."),
            status="assumption" if not opportunity.target_audience else "reported",
        ),
        ReportSection(
            "market_research",
            "5. Market research",
            f"{size_text} {market.note} Sourced market records: {market.source_count}. "
            f"Demand indicators: {len(market.demand_indicators)}; trends: {len(market.trends)}; "
            f"regulatory notes: {len(market.regulatory_considerations)}. {_source_lines(evidence)}",
            tuple(
                source_id
                for row in (*market.demand_indicators, *market.trends)
                for source_id in row.source_ids
            ),
            market_status,
        ),
        ReportSection(
            "demand_evidence",
            "6. Demand evidence",
            f"{customer.interview_count} interview(s), {customer.feedback_count} feedback record(s), "
            f"{len(customer.primary_evidence_ids)} primary and {len(customer.secondary_evidence_ids)} secondary. "
            f"{customer.note}",
            tuple(customer.current_primary_evidence_ids + customer.secondary_evidence_ids),
            "primary" if customer.has_current_primary else "secondary_only" if customer.secondary_evidence_ids else "unavailable",
        ),
        ReportSection(
            "competitive_analysis",
            "7. Competitive analysis",
            competitor_text
            + " "
            + " ".join(
                f"{row.get('name')}: {row.get('offering')} (pricing: {row.get('pricing') or 'unavailable'})."
                for row in competition.competitors
            ),
            tuple(
                str(row.get("evidence_id"))
                for row in competition.competitors
                if row.get("evidence_id")
            ),
            "reported" if competition.search_complete else "incomplete",
        ),
        ReportSection(
            "feasibility_assessment",
            "8. Feasibility assessment",
            " ".join(
                f"{row.name}: "
                + (f"{row.score:.2f}" if row.score is not None else f"unavailable ({row.missing_reason})")
                for row in feasibility.dimensions
            )
            + f" Blockers: {', '.join(feasibility.blockers) if feasibility.blockers else 'none recorded'}.",
            status="measured" if feasibility.complete else "partial",
        ),
        ReportSection(
            "financial_estimates",
            "9. Financial estimates",
            " ".join(
                f"{row.name}: "
                + (
                    f"monthly revenue {row.monthly_revenue}, gross profit {row.monthly_gross_profit}, "
                    f"margin {row.contribution_margin_pct}, net {row.monthly_net_profit}, "
                    f"break-even units {row.break_even_units}, cash required {row.cash_required}."
                    if row.calculated
                    else f"not calculated; missing {', '.join(row.missing_inputs)}."
                )
                for row in financials.scenarios
            )
            + " "
            + financials.assumptions_note,
            status="estimate" if any(row.calculated for row in financials.scenarios) else "unavailable",
        ),
        ReportSection(
            "risk_assessment",
            "10. Risk assessment",
            f"Blockers: {', '.join(feasibility.blockers) if feasibility.blockers else 'none recorded'}. "
            f"Feasibility gaps: {', '.join(feasibility.gaps) if feasibility.gaps else 'none recorded'}. "
            f"Stale evidence: {stale_count}. {gaps_text}",
            status="reported",
        ),
        ReportSection(
            "opportunity_score",
            "11. Opportunity score",
            f"Weighted score {score.weighted_score:.2f}/{score.threshold:.2f} threshold; "
            f"coverage {score.evidence_coverage:.0%}; eligible: {score.eligible}. "
            f"Missing dimensions: {', '.join(score.missing) if score.missing else 'none'}. "
            f"{score.methodology}",
            status="measured",
        ),
        ReportSection(
            "evidence_gaps",
            "12. Evidence gaps",
            gaps_text,
            status="incomplete" if gaps else "complete",
        ),
        ReportSection(
            "validation_experiments",
            "13. Validation experiments",
            tests
            + " No experiment is executed automatically; external tests require explicit approval.",
            status="proposed",
        ),
        ReportSection(
            "recommended_next_steps",
            "14. Recommended next steps",
            next_text,
            status="recommendation",
        ),
        ReportSection(
            "final_verdict",
            "15. Final validation verdict",
            f"{verdict}. {rationale} This verdict is derived from recorded evidence, not from an agent recommendation.",
            status="final",
        ),
    )
    return ValidationReport(
        generated_at=utcnow(),
        verdict=verdict,
        rationale=rationale,
        sections=sections,
        evidence_gaps=tuple(gaps),
        next_steps=tuple(next_steps),
        source_count=source_count,
        primary_count=primary_count,
        stale_count=stale_count,
        score=score,
        limitations=(
            "External market claims are limited to the sources actually retrieved and recorded.",
            "A missing competitor record means the search is incomplete, not that the market is empty.",
            "Financial scenarios are modelled estimates from supplied assumptions, not actual revenue.",
            "Primary customer evidence is accepted only when a person supplies and attributes it.",
        ),
        version=version,
    )


def next_steps_for(
    *,
    opportunity: Opportunity,
    gaps: Sequence[str],
    financials: FinancialAnalysis | None,
) -> tuple[str, ...]:
    steps: list[str] = []
    if not (opportunity.customer and opportunity.customer.has_current_primary):
        steps.append(
            "Run and record current real customer interviews; do not substitute generated personas."
        )
    if not (opportunity.competition and opportunity.competition.search_complete):
        steps.append("Complete a sourced competitor/alternative search before drawing a market conclusion.")
    if not _has_measured_demand(opportunity.market):
        steps.append(
            "Collect current measured demand evidence or run an approved demand experiment; "
            "estimates and assumptions are not enough."
        )
    base = next(
        (row for row in (financials.scenarios if financials else ()) if row.name == "base"),
        None,
    )
    if base is None or not base.calculated:
        steps.append("Supply price, volume, variable cost and fixed-cost assumptions with their source or basis.")
    if opportunity.feasibility is None or not opportunity.feasibility.complete:
        steps.append("Complete the missing feasibility dimensions, especially legal and operational risk.")
    if any("stale" in gap for gap in gaps):
        steps.append("Refresh stale sources before using their claims as current market evidence.")
    if not steps:
        steps.append("Proceed only through an approved paid pilot; no external action is executed automatically.")
    return tuple(dict.fromkeys(steps))


__all__ = [
    "build_competition",
    "build_customer_validation",
    "build_market_research",
    "build_report",
    "decide_verdict",
    "evidence_gaps",
    "next_steps_for",
    "plan_experiments",
]
