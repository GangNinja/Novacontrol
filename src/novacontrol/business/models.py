"""Business opportunity and validation domain models (Phase 35).

The models are deliberately evidence-first:

* every externally sourced claim carries a :class:`SourceRef` and a retrieval
  time, and a stale source is labelled rather than quietly treated as current;
* an assumption is a first-class evidence quality, never a measured fact;
* a missing input remains missing (``None``) and appears in a gaps list instead
  of becoming a plausible-looking zero;
* a final verdict is a value the service derives from recorded evidence, not a
  field an agent can write directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class OpportunityStage(StrEnum):
    DISCOVERED = "discovered"
    RESEARCHING = "researching"
    EVIDENCE_COLLECTION = "evidence_collection"
    FEASIBILITY_ASSESSMENT = "feasibility_assessment"
    EXPERIMENT_PLANNING = "experiment_planning"
    VALIDATION = "validation"
    VALIDATED = "validated"
    CONDITIONALLY_VALIDATED = "conditionally_validated"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_VIABLE = "not_viable"
    REQUIRES_FURTHER_RESEARCH = "requires_further_research"
    PAUSED = "paused"
    REJECTED = "rejected"
    ARCHIVED = "archived"

    @property
    def terminal(self) -> bool:
        return self in {
            OpportunityStage.VALIDATED,
            OpportunityStage.CONDITIONALLY_VALIDATED,
            OpportunityStage.INSUFFICIENT_EVIDENCE,
            OpportunityStage.NOT_VIABLE,
            OpportunityStage.REJECTED,
            OpportunityStage.ARCHIVED,
        }

    @property
    def decided(self) -> bool:
        return self in {
            OpportunityStage.VALIDATED,
            OpportunityStage.CONDITIONALLY_VALIDATED,
            OpportunityStage.INSUFFICIENT_EVIDENCE,
            OpportunityStage.NOT_VIABLE,
            OpportunityStage.REQUIRES_FURTHER_RESEARCH,
        }


_FORWARD_STAGES = {
    OpportunityStage.DISCOVERED: {
        OpportunityStage.RESEARCHING,
        OpportunityStage.EVIDENCE_COLLECTION,
    },
    OpportunityStage.RESEARCHING: {OpportunityStage.EVIDENCE_COLLECTION},
    OpportunityStage.EVIDENCE_COLLECTION: {OpportunityStage.FEASIBILITY_ASSESSMENT},
    OpportunityStage.FEASIBILITY_ASSESSMENT: {OpportunityStage.EXPERIMENT_PLANNING},
    OpportunityStage.EXPERIMENT_PLANNING: {OpportunityStage.VALIDATION},
    OpportunityStage.VALIDATION: {
        OpportunityStage.VALIDATED,
        OpportunityStage.CONDITIONALLY_VALIDATED,
        OpportunityStage.INSUFFICIENT_EVIDENCE,
        OpportunityStage.NOT_VIABLE,
        OpportunityStage.REQUIRES_FURTHER_RESEARCH,
    },
    OpportunityStage.REQUIRES_FURTHER_RESEARCH: {
        OpportunityStage.EVIDENCE_COLLECTION,
        OpportunityStage.EXPERIMENT_PLANNING,
        OpportunityStage.VALIDATION,
    },
    OpportunityStage.CONDITIONALLY_VALIDATED: {
        OpportunityStage.EVIDENCE_COLLECTION,
        OpportunityStage.VALIDATION,
        OpportunityStage.NOT_VIABLE,
    },
}

#: Paused may resume to the stage it came from, or move to archived/rejected.
#: A decided or terminal record is never silently reopened: it must be rejected
#: or archived deliberately, and a genuinely new opportunity gets a new record.
ALLOWED_TRANSITIONS: dict[OpportunityStage, frozenset[OpportunityStage]] = {
    stage: frozenset(targets)
    for stage, targets in {
        OpportunityStage.DISCOVERED: _FORWARD_STAGES[OpportunityStage.DISCOVERED],
        OpportunityStage.RESEARCHING: _FORWARD_STAGES[OpportunityStage.RESEARCHING],
        OpportunityStage.EVIDENCE_COLLECTION: _FORWARD_STAGES[
            OpportunityStage.EVIDENCE_COLLECTION
        ],
        OpportunityStage.FEASIBILITY_ASSESSMENT: _FORWARD_STAGES[
            OpportunityStage.FEASIBILITY_ASSESSMENT
        ],
        OpportunityStage.EXPERIMENT_PLANNING: _FORWARD_STAGES[
            OpportunityStage.EXPERIMENT_PLANNING
        ],
        OpportunityStage.VALIDATION: _FORWARD_STAGES[OpportunityStage.VALIDATION],
        OpportunityStage.REQUIRES_FURTHER_RESEARCH: _FORWARD_STAGES[
            OpportunityStage.REQUIRES_FURTHER_RESEARCH
        ],
        OpportunityStage.CONDITIONALLY_VALIDATED: _FORWARD_STAGES[
            OpportunityStage.CONDITIONALLY_VALIDATED
        ],
        OpportunityStage.VALIDATED: frozenset({OpportunityStage.ARCHIVED}),
        OpportunityStage.INSUFFICIENT_EVIDENCE: frozenset(
            {OpportunityStage.EVIDENCE_COLLECTION, OpportunityStage.ARCHIVED}
        ),
        OpportunityStage.NOT_VIABLE: frozenset({OpportunityStage.ARCHIVED}),
        OpportunityStage.PAUSED: frozenset(),
        OpportunityStage.REJECTED: frozenset({OpportunityStage.ARCHIVED}),
        OpportunityStage.ARCHIVED: frozenset(),
    }.items()
}

#: Rejection and archiving are operator escape hatches from every live stage.
#: They are added after the forward graph so no forward path can accidentally
#: make a decision look like progress. A terminal record can still be archived.
for _closable in (
    OpportunityStage.DISCOVERED,
    OpportunityStage.RESEARCHING,
    OpportunityStage.EVIDENCE_COLLECTION,
    OpportunityStage.FEASIBILITY_ASSESSMENT,
    OpportunityStage.EXPERIMENT_PLANNING,
    OpportunityStage.VALIDATION,
    OpportunityStage.REQUIRES_FURTHER_RESEARCH,
    OpportunityStage.CONDITIONALLY_VALIDATED,
    OpportunityStage.INSUFFICIENT_EVIDENCE,
):
    ALLOWED_TRANSITIONS[_closable] = ALLOWED_TRANSITIONS[_closable] | frozenset(
        {OpportunityStage.REJECTED, OpportunityStage.ARCHIVED}
    )
for _terminal in (
    OpportunityStage.VALIDATED,
    OpportunityStage.NOT_VIABLE,
    OpportunityStage.REJECTED,
):
    ALLOWED_TRANSITIONS[_terminal] = ALLOWED_TRANSITIONS[_terminal] | frozenset(
        {OpportunityStage.ARCHIVED}
    )


class SignalType(StrEnum):
    MARKET_TREND = "market_trend"
    CUSTOMER_COMPLAINT = "customer_complaint"
    INDUSTRY_DEVELOPMENT = "industry_development"
    TECHNOLOGY_CHANGE = "technology_change"
    EMERGING_MARKET = "emerging_market"
    COMPETITOR_WEAKNESS = "competitor_weakness"
    OPERATIONAL_PROBLEM = "operational_problem"
    PRODUCT_GAP = "product_gap"
    AI_OPPORTUNITY = "ai_opportunity"
    USER_SUPPLIED = "user_supplied"


class EvidenceKind(StrEnum):
    MARKET_SIZE = "market_size"
    MARKET_TREND = "market_trend"
    DEMAND_SIGNAL = "demand_signal"
    CUSTOMER_INTERVIEW = "customer_interview"
    CUSTOMER_FEEDBACK = "customer_feedback"
    COMPETITOR = "competitor"
    TECHNICAL = "technical"
    OPERATIONAL = "operational"
    REGULATORY = "regulatory"
    FINANCIAL = "financial"
    ASSUMPTION = "assumption"
    OTHER = "other"


class EvidenceQuality(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    ESTIMATE = "estimate"
    ASSUMPTION = "assumption"
    SYNTHESIS = "synthesis"

    @property
    def measured(self) -> bool:
        return self in {EvidenceQuality.PRIMARY, EvidenceQuality.SECONDARY}

    @property
    def counts_toward_validation(self) -> bool:
        return self in {
            EvidenceQuality.PRIMARY,
            EvidenceQuality.SECONDARY,
            EvidenceQuality.ESTIMATE,
        }


class TradeStatus(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SourceRef:
    id: str
    title: str
    url: str
    source_type: str = "web"
    publisher: str = ""
    retrieved_at: datetime = field(default_factory=utcnow)
    excerpt: str = ""
    supplied_by: str = ""

    def stale(self, *, now: datetime | None = None, max_age: timedelta = timedelta(days=90)) -> bool:
        return (now or utcnow()) - self.retrieved_at > max_age

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source_type": self.source_type,
            "publisher": self.publisher,
            "retrieved_at": _iso(self.retrieved_at),
            "excerpt": self.excerpt,
            "supplied_by": self.supplied_by,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceRef:
        retrieved = _dt(payload.get("retrieved_at"))
        if retrieved is None:
            # Never turn a missing provenance timestamp into "retrieved now".
            # The opportunity loader reports this row as a load error instead of
            # silently promoting an old source to current evidence.
            raise ValueError("Source provenance requires a valid retrieved_at timestamp.")
        return cls(
            id=str(payload.get("id") or new_id("src")),
            title=str(payload.get("title", "")),
            url=str(payload.get("url", "")),
            source_type=str(payload.get("source_type", "web")),
            publisher=str(payload.get("publisher", "")),
            retrieved_at=retrieved,
            excerpt=str(payload.get("excerpt", "")),
            supplied_by=str(payload.get("supplied_by", "")),
        )


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: str
    kind: EvidenceKind
    claim: str
    quality: EvidenceQuality
    source: SourceRef | None = None
    confidence: float = 0.5
    supports: tuple[str, ...] = ()
    recorded_at: datetime = field(default_factory=utcnow)
    supplied_by: str = ""
    value: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise ValueError("Evidence needs a claim.")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Evidence confidence must be between 0 and 1.")
        if self.quality.measured and self.source is None:
            raise ValueError("Measured evidence requires a source reference.")
        if self.quality is EvidenceQuality.PRIMARY and not self.supplied_by:
            raise ValueError("Primary customer evidence must name who supplied it.")

    def stale(self, *, now: datetime | None = None, max_age: timedelta = timedelta(days=90)) -> bool:
        return bool(self.source and self.source.stale(now=now, max_age=max_age))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "claim": self.claim,
            "quality": self.quality.value,
            "source": self.source.to_dict() if self.source else None,
            "confidence": round(float(self.confidence), 4),
            "supports": list(self.supports),
            "recorded_at": _iso(self.recorded_at),
            "supplied_by": self.supplied_by,
            "value": dict(self.value),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceRecord:
        source = payload.get("source")
        return cls(
            id=str(payload.get("id") or new_id("ev")),
            kind=EvidenceKind(str(payload.get("kind", "other"))),
            claim=str(payload.get("claim", "")),
            quality=EvidenceQuality(str(payload.get("quality", "assumption"))),
            source=SourceRef.from_dict(source) if isinstance(source, Mapping) else None,
            confidence=float(payload.get("confidence", 0.5) or 0.0),
            supports=tuple(str(item) for item in payload.get("supports", ()) or ()),
            recorded_at=_dt(payload.get("recorded_at")) or utcnow(),
            supplied_by=str(payload.get("supplied_by", "")),
            value=dict(payload.get("value") or {}),
            notes=str(payload.get("notes", "")),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    text: str
    source_ids: tuple[str, ...] = ()
    quality: EvidenceQuality = EvidenceQuality.SECONDARY
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source_ids": list(self.source_ids),
            "quality": self.quality.value,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class MarketSize:
    value: float | None
    currency: str = ""
    year: int | None = None
    status: TradeStatus = TradeStatus.UNAVAILABLE
    source_ids: tuple[str, ...] = ()
    method: str = ""
    note: str = "The market size was not supplied with a verifiable source."

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "currency": self.currency,
            "year": self.year,
            "status": self.status.value,
            "source_ids": list(self.source_ids),
            "method": self.method,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class MarketResearch:
    market_size: MarketSize
    segments: tuple[Finding, ...] = ()
    demand_indicators: tuple[Finding, ...] = ()
    pain_points: tuple[Finding, ...] = ()
    trends: tuple[Finding, ...] = ()
    growth_indicators: tuple[Finding, ...] = ()
    regulatory_considerations: tuple[Finding, ...] = ()
    geography: str = ""
    maturity: str = ""
    source_count: int = 0
    stale_source_count: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_size": self.market_size.to_dict(),
            "segments": [row.to_dict() for row in self.segments],
            "demand_indicators": [row.to_dict() for row in self.demand_indicators],
            "pain_points": [row.to_dict() for row in self.pain_points],
            "trends": [row.to_dict() for row in self.trends],
            "growth_indicators": [row.to_dict() for row in self.growth_indicators],
            "regulatory_considerations": [
                row.to_dict() for row in self.regulatory_considerations
            ],
            "geography": self.geography,
            "maturity": self.maturity,
            "source_count": self.source_count,
            "stale_source_count": self.stale_source_count,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CustomerValidation:
    segments: tuple[str, ...] = ()
    severity: tuple[Finding, ...] = ()
    frequency: tuple[Finding, ...] = ()
    alternatives: tuple[Finding, ...] = ()
    willingness_to_pay: tuple[Finding, ...] = ()
    unmet_demand: tuple[Finding, ...] = ()
    interview_count: int = 0
    feedback_count: int = 0
    primary_evidence_ids: tuple[str, ...] = ()
    current_primary_evidence_ids: tuple[str, ...] = ()
    secondary_evidence_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    note: str = ""
    gap: str = "No supplied customer interview, survey or feedback record exists."

    @property
    def has_primary(self) -> bool:
        """Whether any primary record exists, including a stale historical one."""
        return bool(self.primary_evidence_ids)

    @property
    def has_current_primary(self) -> bool:
        """Whether a primary record is inside the freshness window."""
        return bool(self.current_primary_evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": list(self.segments),
            "severity": [row.to_dict() for row in self.severity],
            "frequency": [row.to_dict() for row in self.frequency],
            "alternatives": [row.to_dict() for row in self.alternatives],
            "willingness_to_pay": [row.to_dict() for row in self.willingness_to_pay],
            "unmet_demand": [row.to_dict() for row in self.unmet_demand],
            "interview_count": self.interview_count,
            "feedback_count": self.feedback_count,
            "primary_evidence_ids": list(self.primary_evidence_ids),
            "current_primary_evidence_ids": list(self.current_primary_evidence_ids),
            "secondary_evidence_ids": list(self.secondary_evidence_ids),
            "assumptions": list(self.assumptions),
            "note": self.note,
            "gap": self.gap,
            "has_primary": self.has_primary,
            "has_current_primary": self.has_current_primary,
        }


@dataclass(frozen=True, slots=True)
class CompetitorLandscape:
    competitors: tuple[Mapping[str, Any], ...] = ()
    search_complete: bool = False
    query: str = ""
    note: str = (
        "No competitor evidence has been recorded. An incomplete search is not "
        "evidence that no competitors exist."
    )
    gaps: tuple[str, ...] = (
        "Competitor discovery is incomplete until at least one sourced competitor or alternative is recorded.",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitors": [dict(row) for row in self.competitors],
            "search_complete": self.search_complete,
            "query": self.query,
            "note": self.note,
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True, slots=True)
class FeasibilityDimension:
    name: str
    score: float | None
    rationale: str = ""
    evidence_ids: tuple[str, ...] = ()
    missing_reason: str = ""

    @property
    def available(self) -> bool:
        return self.score is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": None if self.score is None else round(float(self.score), 4),
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "missing_reason": self.missing_reason,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class FeasibilityAssessment:
    dimensions: tuple[FeasibilityDimension, ...]
    overall_score: float | None
    blockers: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    resource_snapshot: Mapping[str, Any] = field(default_factory=dict)
    requirements: Mapping[str, Any] = field(default_factory=dict)
    complete: bool = False
    computed_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": [row.to_dict() for row in self.dimensions],
            "overall_score": None
            if self.overall_score is None
            else round(float(self.overall_score), 4),
            "blockers": list(self.blockers),
            "gaps": list(self.gaps),
            "resource_snapshot": dict(self.resource_snapshot),
            "requirements": dict(self.requirements),
            "complete": self.complete,
            "computed_at": _iso(self.computed_at),
        }


@dataclass(frozen=True, slots=True)
class FinancialAssumptions:
    price: float | None = None
    units_per_month: float | None = None
    variable_cost_per_unit: float | None = None
    fixed_monthly_costs: float | None = None
    infrastructure_monthly: float | None = None
    marketing_monthly: float | None = None
    customer_acquisition_cost: float | None = None
    customer_lifetime_months: float | None = None
    initial_investment: float | None = None
    development_cost: float | None = None
    months_to_forecast: int = 12

    _REQUIRED = (
        "price",
        "units_per_month",
        "variable_cost_per_unit",
        "fixed_monthly_costs",
    )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> FinancialAssumptions:
        data = dict(payload or {})

        def number(key: str) -> float | None:
            raw = data.get(key)
            if raw is None or raw == "":
                return None
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number.")
            if value < 0:
                raise ValueError(f"{key} cannot be negative.")
            return value

        months = int(data.get("months_to_forecast", 12) or 12)
        if not 1 <= months <= 120:
            raise ValueError("months_to_forecast must be between 1 and 120.")
        return cls(
            price=number("price"),
            units_per_month=number("units_per_month"),
            variable_cost_per_unit=number("variable_cost_per_unit"),
            fixed_monthly_costs=number("fixed_monthly_costs"),
            infrastructure_monthly=number("infrastructure_monthly"),
            marketing_monthly=number("marketing_monthly"),
            customer_acquisition_cost=number("customer_acquisition_cost"),
            customer_lifetime_months=number("customer_lifetime_months"),
            initial_investment=number("initial_investment"),
            development_cost=number("development_cost"),
            months_to_forecast=months,
        )

    @property
    def missing_required(self) -> tuple[str, ...]:
        return tuple(name for name in self._REQUIRED if getattr(self, name) is None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "units_per_month": self.units_per_month,
            "variable_cost_per_unit": self.variable_cost_per_unit,
            "fixed_monthly_costs": self.fixed_monthly_costs,
            "infrastructure_monthly": self.infrastructure_monthly,
            "marketing_monthly": self.marketing_monthly,
            "customer_acquisition_cost": self.customer_acquisition_cost,
            "customer_lifetime_months": self.customer_lifetime_months,
            "initial_investment": self.initial_investment,
            "development_cost": self.development_cost,
            "months_to_forecast": self.months_to_forecast,
        }


@dataclass(frozen=True, slots=True)
class FinancialScenario:
    name: str
    assumptions: Mapping[str, Any]
    monthly_revenue: float | None = None
    monthly_gross_profit: float | None = None
    contribution_margin: float | None = None
    contribution_margin_pct: float | None = None
    monthly_operating_expenses: float | None = None
    monthly_net_profit: float | None = None
    break_even_units: float | None = None
    break_even_revenue: float | None = None
    cash_required: float | None = None
    customer_lifetime_value: float | None = None
    cac_payback_months: float | None = None
    forecast_revenue: float | None = None
    missing_inputs: tuple[str, ...] = ()
    methodology: tuple[str, ...] = ()
    calculated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "assumptions": dict(self.assumptions),
            "monthly_revenue": self.monthly_revenue,
            "monthly_gross_profit": self.monthly_gross_profit,
            "contribution_margin": self.contribution_margin,
            "contribution_margin_pct": self.contribution_margin_pct,
            "monthly_operating_expenses": self.monthly_operating_expenses,
            "monthly_net_profit": self.monthly_net_profit,
            "break_even_units": self.break_even_units,
            "break_even_revenue": self.break_even_revenue,
            "cash_required": self.cash_required,
            "customer_lifetime_value": self.customer_lifetime_value,
            "cac_payback_months": self.cac_payback_months,
            "forecast_revenue": self.forecast_revenue,
            "missing_inputs": list(self.missing_inputs),
            "methodology": list(self.methodology),
            "calculated": self.calculated,
        }


@dataclass(frozen=True, slots=True)
class FinancialAnalysis:
    currency: str
    scenarios: tuple[FinancialScenario, ...]
    missing_inputs: tuple[str, ...] = ()
    methodology: tuple[str, ...] = ()
    assumptions_note: str = (
        "All figures are modelled estimates from supplied assumptions, not actual "
        "revenue or guaranteed profit."
    )
    computed_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "scenarios": [row.to_dict() for row in self.scenarios],
            "missing_inputs": list(self.missing_inputs),
            "methodology": list(self.methodology),
            "assumptions_note": self.assumptions_note,
            "computed_at": _iso(self.computed_at),
        }


@dataclass(frozen=True, slots=True)
class ScoreDimension:
    name: str
    weight: float
    score: float | None
    contribution: float | None
    evidence_count: int = 0
    missing_reason: str = ""
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": round(float(self.weight), 4),
            "score": None if self.score is None else round(float(self.score), 4),
            "contribution": None
            if self.contribution is None
            else round(float(self.contribution), 4),
            "evidence_count": self.evidence_count,
            "missing_reason": self.missing_reason,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class OpportunityScore:
    weighted_score: float
    evidence_coverage: float
    threshold: float
    eligible: bool
    dimensions: tuple[ScoreDimension, ...] = ()
    missing: tuple[str, ...] = ()
    disqualifiers: tuple[str, ...] = ()
    weights: Mapping[str, float] = field(default_factory=dict)
    methodology: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "weighted_score": round(float(self.weighted_score), 4),
            "evidence_coverage": round(float(self.evidence_coverage), 4),
            "threshold": round(float(self.threshold), 4),
            "eligible": self.eligible,
            "dimensions": [row.to_dict() for row in self.dimensions],
            "missing": list(self.missing),
            "disqualifiers": list(self.disqualifiers),
            "weights": dict(self.weights),
            "methodology": self.methodology,
        }


@dataclass(frozen=True, slots=True)
class Experiment:
    id: str
    title: str
    hypothesis: str
    objective: str
    target_audience: str
    required_resources: tuple[str, ...]
    estimated_cost: float | None
    currency: str
    duration_days: int | None
    success_criteria: tuple[str, ...]
    failure_criteria: tuple[str, ...]
    measurement_method: str
    requires_approval: bool
    expected_evidence: tuple[str, ...]
    status: str = "proposed"
    approved_by: str = ""
    outcome: str = ""
    result: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "hypothesis": self.hypothesis,
            "objective": self.objective,
            "target_audience": self.target_audience,
            "required_resources": list(self.required_resources),
            "estimated_cost": self.estimated_cost,
            "currency": self.currency,
            "duration_days": self.duration_days,
            "success_criteria": list(self.success_criteria),
            "failure_criteria": list(self.failure_criteria),
            "measurement_method": self.measurement_method,
            "requires_approval": self.requires_approval,
            "expected_evidence": list(self.expected_evidence),
            "status": self.status,
            "approved_by": self.approved_by,
            "outcome": self.outcome,
            "result": dict(self.result),
            "created_at": _iso(self.created_at),
            "external_action_executed": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Experiment:
        return cls(
            id=str(payload.get("id") or new_id("exp")),
            title=str(payload.get("title", "")),
            hypothesis=str(payload.get("hypothesis", "")),
            objective=str(payload.get("objective", "")),
            target_audience=str(payload.get("target_audience", "")),
            required_resources=tuple(
                str(item) for item in payload.get("required_resources", ()) or ()
            ),
            estimated_cost=payload.get("estimated_cost"),
            currency=str(payload.get("currency", "USD")),
            duration_days=payload.get("duration_days"),
            success_criteria=tuple(
                str(item) for item in payload.get("success_criteria", ()) or ()
            ),
            failure_criteria=tuple(
                str(item) for item in payload.get("failure_criteria", ()) or ()
            ),
            measurement_method=str(payload.get("measurement_method", "")),
            requires_approval=bool(payload.get("requires_approval", True)),
            expected_evidence=tuple(
                str(item) for item in payload.get("expected_evidence", ()) or ()
            ),
            status=str(payload.get("status", "proposed")),
            approved_by=str(payload.get("approved_by", "")),
            outcome=str(payload.get("outcome", "")),
            result=dict(payload.get("result") or {}),
            created_at=_dt(payload.get("created_at")) or utcnow(),
        )


@dataclass(frozen=True, slots=True)
class ReportSection:
    key: str
    title: str
    body: str
    source_ids: tuple[str, ...] = ()
    status: str = "reported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "body": self.body,
            "source_ids": list(self.source_ids),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    generated_at: datetime
    verdict: str
    rationale: str
    sections: tuple[ReportSection, ...]
    evidence_gaps: tuple[str, ...]
    next_steps: tuple[str, ...]
    source_count: int
    primary_count: int
    stale_count: int
    score: OpportunityScore | None
    limitations: tuple[str, ...] = ()
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": _iso(self.generated_at),
            "verdict": self.verdict,
            "rationale": self.rationale,
            "sections": [row.to_dict() for row in self.sections],
            "evidence_gaps": list(self.evidence_gaps),
            "next_steps": list(self.next_steps),
            "source_count": self.source_count,
            "primary_count": self.primary_count,
            "stale_count": self.stale_count,
            "score": self.score.to_dict() if self.score else None,
            "limitations": list(self.limitations),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    at: datetime
    from_stage: str
    to_stage: str
    reason: str = ""
    actor: str = "operator"

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": _iso(self.at),
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "reason": self.reason,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LifecycleEvent:
        return cls(
            at=_dt(payload.get("at")) or utcnow(),
            from_stage=str(payload.get("from_stage", "")),
            to_stage=str(payload.get("to_stage", "")),
            reason=str(payload.get("reason", "")),
            actor=str(payload.get("actor", "operator")),
        )


@dataclass(frozen=True, slots=True)
class Opportunity:
    id: str
    title: str
    description: str
    industry: str
    category: str
    customer_problem: str
    proposed_solution: str
    target_audience: str
    signal_type: SignalType
    discovered_at: datetime
    updated_at: datetime
    stage: OpportunityStage = OpportunityStage.DISCOVERED
    source_refs: tuple[SourceRef, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    confidence: float = 0.0
    uncertainty: tuple[str, ...] = ()
    market: MarketResearch | None = None
    customer: CustomerValidation | None = None
    competition: CompetitorLandscape | None = None
    feasibility: FeasibilityAssessment | None = None
    financials: FinancialAnalysis | None = None
    score: OpportunityScore | None = None
    experiments: tuple[Experiment, ...] = ()
    report: ValidationReport | None = None
    verdict: str = ""
    verdict_rationale: str = ""
    history: tuple[LifecycleEvent, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    duplicate_key: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("An opportunity needs a title.")
        if not self.customer_problem.strip():
            raise ValueError("An opportunity needs the customer problem it addresses.")
        if not self.proposed_solution.strip():
            raise ValueError("An opportunity needs a proposed solution.")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Confidence must be between 0 and 1.")

    def source_map(self) -> dict[str, SourceRef]:
        rows = {row.id: row for row in self.source_refs}
        for evidence in self.evidence:
            if evidence.source is not None:
                rows[evidence.source.id] = evidence.source
        return rows

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "industry": self.industry,
            "category": self.category,
            "stage": self.stage.value,
            "verdict": self.verdict,
            "confidence": round(float(self.confidence), 4),
            "evidence_count": len(self.evidence),
            "source_count": len(self.source_map()),
            "primary_evidence_count": sum(
                1 for row in self.evidence if row.quality is EvidenceQuality.PRIMARY
            ),
            "discovered_at": _iso(self.discovered_at),
            "updated_at": _iso(self.updated_at),
            "target_audience": self.target_audience,
            "customer_problem": self.customer_problem,
            "score": None if self.score is None else self.score.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_summary(),
            "description": self.description,
            "proposed_solution": self.proposed_solution,
            "signal_type": self.signal_type.value,
            "uncertainty": list(self.uncertainty),
            "source_refs": [row.to_dict() for row in self.source_refs],
            "evidence": [row.to_dict() for row in self.evidence],
            "market": self.market.to_dict() if self.market else None,
            "customer": self.customer.to_dict() if self.customer else None,
            "competition": self.competition.to_dict() if self.competition else None,
            "feasibility": self.feasibility.to_dict() if self.feasibility else None,
            "financials": self.financials.to_dict() if self.financials else None,
            "score": self.score.to_dict() if self.score else None,
            "experiments": [row.to_dict() for row in self.experiments],
            "report": self.report.to_dict() if self.report else None,
            "verdict_rationale": self.verdict_rationale,
            "history": [row.to_dict() for row in self.history],
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "duplicate_key": self.duplicate_key,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Opportunity:
        market = payload.get("market")
        customer = payload.get("customer")
        competition = payload.get("competition")
        feasibility = payload.get("feasibility")
        financials = payload.get("financials")
        score = payload.get("score")
        report = payload.get("report")
        return cls(
            id=str(payload["id"]),
            title=str(payload.get("title", "")),
            description=str(payload.get("description", "")),
            industry=str(payload.get("industry", "")),
            category=str(payload.get("category", "")),
            customer_problem=str(payload.get("customer_problem", "")),
            proposed_solution=str(payload.get("proposed_solution", "")),
            target_audience=str(payload.get("target_audience", "")),
            signal_type=SignalType(str(payload.get("signal_type", "user_supplied"))),
            discovered_at=_dt(payload.get("discovered_at")) or utcnow(),
            updated_at=_dt(payload.get("updated_at")) or utcnow(),
            stage=OpportunityStage(str(payload.get("stage", "discovered"))),
            source_refs=tuple(
                SourceRef.from_dict(row)
                for row in payload.get("source_refs", ()) or ()
                if isinstance(row, Mapping)
            ),
            evidence=tuple(
                EvidenceRecord.from_dict(row)
                for row in payload.get("evidence", ()) or ()
                if isinstance(row, Mapping)
            ),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            uncertainty=tuple(str(item) for item in payload.get("uncertainty", ()) or ()),
            market=_market_from_dict(market) if isinstance(market, Mapping) else None,
            customer=_customer_from_dict(customer) if isinstance(customer, Mapping) else None,
            competition=_competition_from_dict(competition)
            if isinstance(competition, Mapping)
            else None,
            feasibility=_feasibility_from_dict(feasibility)
            if isinstance(feasibility, Mapping)
            else None,
            financials=_financials_from_dict(financials)
            if isinstance(financials, Mapping)
            else None,
            score=_score_from_dict(score) if isinstance(score, Mapping) else None,
            experiments=tuple(
                Experiment.from_dict(row)
                for row in payload.get("experiments", ()) or ()
                if isinstance(row, Mapping)
            ),
            report=_report_from_dict(report) if isinstance(report, Mapping) else None,
            verdict=str(payload.get("verdict", "")),
            verdict_rationale=str(payload.get("verdict_rationale", "")),
            history=tuple(
                LifecycleEvent.from_dict(row)
                for row in payload.get("history", ()) or ()
                if isinstance(row, Mapping)
            ),
            tags=tuple(str(item) for item in payload.get("tags", ()) or ()),
            metadata=dict(payload.get("metadata") or {}),
            duplicate_key=str(payload.get("duplicate_key", "")),
        )


def _findings(payload: Any) -> tuple[Finding, ...]:
    if not isinstance(payload, (list, tuple)):
        return ()
    rows: list[Finding] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            Finding(
                text=str(item.get("text", "")),
                source_ids=tuple(str(x) for x in item.get("source_ids", ()) or ()),
                quality=EvidenceQuality(str(item.get("quality", "secondary"))),
                stale=bool(item.get("stale", False)),
            )
        )
    return tuple(rows)


def _market_from_dict(payload: Mapping[str, Any]) -> MarketResearch:
    size = payload.get("market_size")
    market_size = MarketSize(
        value=None,
        status=TradeStatus.UNAVAILABLE,
    )
    if isinstance(size, Mapping):
        market_size = MarketSize(
            value=size.get("value"),
            currency=str(size.get("currency", "")),
            year=size.get("year"),
            status=TradeStatus(str(size.get("status", "unavailable"))),
            source_ids=tuple(str(x) for x in size.get("source_ids", ()) or ()),
            method=str(size.get("method", "")),
            note=str(size.get("note", "")),
        )
    return MarketResearch(
        market_size=market_size,
        segments=_findings(payload.get("segments")),
        demand_indicators=_findings(payload.get("demand_indicators")),
        pain_points=_findings(payload.get("pain_points")),
        trends=_findings(payload.get("trends")),
        growth_indicators=_findings(payload.get("growth_indicators")),
        regulatory_considerations=_findings(payload.get("regulatory_considerations")),
        geography=str(payload.get("geography", "")),
        maturity=str(payload.get("maturity", "")),
        source_count=int(payload.get("source_count", 0) or 0),
        stale_source_count=int(payload.get("stale_source_count", 0) or 0),
        note=str(payload.get("note", "")),
    )


def _customer_from_dict(payload: Mapping[str, Any]) -> CustomerValidation:
    primary_ids = tuple(
        str(x) for x in payload.get("primary_evidence_ids", ()) or ()
    )
    # Older persisted customer snapshots predate the current/historical split.
    # Keep them readable; a fresh assessment recomputes the split from evidence.
    current_raw = payload.get("current_primary_evidence_ids")
    current_ids = (
        tuple(str(x) for x in current_raw or ())
        if current_raw is not None
        else primary_ids
    )
    return CustomerValidation(
        segments=tuple(str(x) for x in payload.get("segments", ()) or ()),
        severity=_findings(payload.get("severity")),
        frequency=_findings(payload.get("frequency")),
        alternatives=_findings(payload.get("alternatives")),
        willingness_to_pay=_findings(payload.get("willingness_to_pay")),
        unmet_demand=_findings(payload.get("unmet_demand")),
        interview_count=int(payload.get("interview_count", 0) or 0),
        feedback_count=int(payload.get("feedback_count", 0) or 0),
        primary_evidence_ids=primary_ids,
        current_primary_evidence_ids=current_ids,
        secondary_evidence_ids=tuple(
            str(x) for x in payload.get("secondary_evidence_ids", ()) or ()
        ),
        assumptions=tuple(str(x) for x in payload.get("assumptions", ()) or ()),
        note=str(payload.get("note", "")),
        gap=str(payload.get("gap", "")),
    )


def _competition_from_dict(payload: Mapping[str, Any]) -> CompetitorLandscape:
    return CompetitorLandscape(
        competitors=tuple(
            dict(row) for row in payload.get("competitors", ()) or () if isinstance(row, Mapping)
        ),
        search_complete=bool(payload.get("search_complete", False)),
        query=str(payload.get("query", "")),
        note=str(payload.get("note", "")),
        gaps=tuple(str(x) for x in payload.get("gaps", ()) or ()),
    )


def _feasibility_from_dict(payload: Mapping[str, Any]) -> FeasibilityAssessment:
    dimensions = tuple(
        FeasibilityDimension(
            name=str(item.get("name", "")),
            score=item.get("score"),
            rationale=str(item.get("rationale", "")),
            evidence_ids=tuple(str(x) for x in item.get("evidence_ids", ()) or ()),
            missing_reason=str(item.get("missing_reason", "")),
        )
        for item in payload.get("dimensions", ()) or ()
        if isinstance(item, Mapping)
    )
    return FeasibilityAssessment(
        dimensions=dimensions,
        overall_score=payload.get("overall_score"),
        blockers=tuple(str(x) for x in payload.get("blockers", ()) or ()),
        gaps=tuple(str(x) for x in payload.get("gaps", ()) or ()),
        resource_snapshot=dict(payload.get("resource_snapshot") or {}),
        requirements=dict(payload.get("requirements") or {}),
        complete=bool(payload.get("complete", False)),
        computed_at=_dt(payload.get("computed_at")) or utcnow(),
    )


def _financials_from_dict(payload: Mapping[str, Any]) -> FinancialAnalysis:
    scenarios = tuple(
        FinancialScenario(
            name=str(item.get("name", "base")),
            assumptions=dict(item.get("assumptions") or {}),
            monthly_revenue=item.get("monthly_revenue"),
            monthly_gross_profit=item.get("monthly_gross_profit"),
            contribution_margin=item.get("contribution_margin"),
            contribution_margin_pct=item.get("contribution_margin_pct"),
            monthly_operating_expenses=item.get("monthly_operating_expenses"),
            monthly_net_profit=item.get("monthly_net_profit"),
            break_even_units=item.get("break_even_units"),
            break_even_revenue=item.get("break_even_revenue"),
            cash_required=item.get("cash_required"),
            customer_lifetime_value=item.get("customer_lifetime_value"),
            cac_payback_months=item.get("cac_payback_months"),
            forecast_revenue=item.get("forecast_revenue"),
            missing_inputs=tuple(str(x) for x in item.get("missing_inputs", ()) or ()),
            methodology=tuple(str(x) for x in item.get("methodology", ()) or ()),
            calculated=bool(item.get("calculated", False)),
        )
        for item in payload.get("scenarios", ()) or ()
        if isinstance(item, Mapping)
    )
    return FinancialAnalysis(
        currency=str(payload.get("currency", "USD")),
        scenarios=scenarios,
        missing_inputs=tuple(str(x) for x in payload.get("missing_inputs", ()) or ()),
        methodology=tuple(str(x) for x in payload.get("methodology", ()) or ()),
        assumptions_note=str(payload.get("assumptions_note", "")),
        computed_at=_dt(payload.get("computed_at")) or utcnow(),
    )


def _score_from_dict(payload: Mapping[str, Any]) -> OpportunityScore:
    dimensions = tuple(
        ScoreDimension(
            name=str(item.get("name", "")),
            weight=float(item.get("weight", 0.0) or 0.0),
            score=item.get("score"),
            contribution=item.get("contribution"),
            evidence_count=int(item.get("evidence_count", 0) or 0),
            missing_reason=str(item.get("missing_reason", "")),
            rationale=str(item.get("rationale", "")),
        )
        for item in payload.get("dimensions", ()) or ()
        if isinstance(item, Mapping)
    )
    return OpportunityScore(
        weighted_score=float(payload.get("weighted_score", 0.0) or 0.0),
        evidence_coverage=float(payload.get("evidence_coverage", 0.0) or 0.0),
        threshold=float(payload.get("threshold", 0.6) or 0.6),
        eligible=bool(payload.get("eligible", False)),
        dimensions=dimensions,
        missing=tuple(str(x) for x in payload.get("missing", ()) or ()),
        disqualifiers=tuple(str(x) for x in payload.get("disqualifiers", ()) or ()),
        weights={str(k): float(v) for k, v in dict(payload.get("weights") or {}).items()},
        methodology=str(payload.get("methodology", "")),
    )


def _report_from_dict(payload: Mapping[str, Any]) -> ValidationReport:
    sections = tuple(
        ReportSection(
            key=str(item.get("key", "")),
            title=str(item.get("title", "")),
            body=str(item.get("body", "")),
            source_ids=tuple(str(x) for x in item.get("source_ids", ()) or ()),
            status=str(item.get("status", "reported")),
        )
        for item in payload.get("sections", ()) or ()
        if isinstance(item, Mapping)
    )
    score = payload.get("score")
    return ValidationReport(
        generated_at=_dt(payload.get("generated_at")) or utcnow(),
        verdict=str(payload.get("verdict", "")),
        rationale=str(payload.get("rationale", "")),
        sections=sections,
        evidence_gaps=tuple(str(x) for x in payload.get("evidence_gaps", ()) or ()),
        next_steps=tuple(str(x) for x in payload.get("next_steps", ()) or ()),
        source_count=int(payload.get("source_count", 0) or 0),
        primary_count=int(payload.get("primary_count", 0) or 0),
        stale_count=int(payload.get("stale_count", 0) or 0),
        score=_score_from_dict(score) if isinstance(score, Mapping) else None,
        limitations=tuple(str(x) for x in payload.get("limitations", ()) or ()),
        version=int(payload.get("version", 1) or 1),
    )


def transition(
    current: Opportunity, target: OpportunityStage, *, reason: str, actor: str
) -> Opportunity:
    """Validate and record one lifecycle transition.

    ``PAUSED`` remembers the stage it came from in metadata; resuming without a
    remembered stage is refused rather than guessed.
    """
    now = utcnow()
    if target is OpportunityStage.PAUSED:
        if current.stage in {
            OpportunityStage.PAUSED,
            OpportunityStage.ARCHIVED,
            OpportunityStage.REJECTED,
            # A decided record is a recorded outcome; pausing it would hide the
            # verdict while still leaving it open to resume. Archive or create a
            # new record instead.
            OpportunityStage.VALIDATED,
            OpportunityStage.NOT_VIABLE,
        }:
            raise ValueError(f"Cannot pause an opportunity in {current.stage.value}.")
        event = LifecycleEvent(now, current.stage.value, target.value, reason, actor)
        return replace(
            current,
            stage=target,
            updated_at=now,
            history=(*current.history, event),
            metadata={**dict(current.metadata), "paused_from": current.stage.value},
        )
    if current.stage is OpportunityStage.PAUSED:
        previous = str(current.metadata.get("paused_from", ""))
        if not previous:
            raise ValueError("Paused opportunity has no remembered stage to resume.")
        allowed = {
            OpportunityStage(previous),
            OpportunityStage.REJECTED,
            OpportunityStage.ARCHIVED,
        }
        if target not in allowed:
            raise ValueError(
                f"Cannot move a paused opportunity from {previous} to {target.value}."
            )
    elif target not in ALLOWED_TRANSITIONS.get(current.stage, frozenset()):
        raise ValueError(
            f"Invalid opportunity transition: {current.stage.value} -> {target.value}."
        )
    if target is OpportunityStage.REJECTED and not reason.strip():
        raise ValueError("Rejecting an opportunity requires a reason.")
    if target is OpportunityStage.ARCHIVED and not reason.strip():
        raise ValueError("Archiving an opportunity requires a reason.")
    event = LifecycleEvent(now, current.stage.value, target.value, reason, actor)
    metadata = dict(current.metadata)
    metadata.pop("paused_from", None)
    return replace(
        current,
        stage=target,
        updated_at=now,
        history=(*current.history, event),
        metadata=metadata,
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CompetitorLandscape",
    "CustomerValidation",
    "EvidenceKind",
    "EvidenceQuality",
    "EvidenceRecord",
    "Experiment",
    "FeasibilityAssessment",
    "FeasibilityDimension",
    "FinancialAnalysis",
    "FinancialAssumptions",
    "FinancialScenario",
    "Finding",
    "LifecycleEvent",
    "MarketResearch",
    "MarketSize",
    "Opportunity",
    "OpportunityScore",
    "OpportunityStage",
    "ReportSection",
    "ScoreDimension",
    "SignalType",
    "SourceRef",
    "TradeStatus",
    "ValidationReport",
    "new_id",
    "transition",
    "utcnow",
]
