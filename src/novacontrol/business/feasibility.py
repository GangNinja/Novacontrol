"""Evidence-aware feasibility assessment (Phase 35).

Every dimension either has a real input (a supplied assessment, a measured
hardware reading, a computed financial result, or recorded customer evidence)
or is reported as unavailable. A missing dimension is never silently scored as
"average", and a legal/technical blocker reaches the final verdict.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from novacontrol.business.financial import financial_feasibility_score
from novacontrol.business.models import (
    CustomerValidation,
    EvidenceKind,
    EvidenceRecord,
    FeasibilityAssessment,
    FeasibilityDimension,
    FinancialAnalysis,
    MarketResearch,
    utcnow,
)


def _score(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return max(0.0, min(1.0, score))


def _ids(evidence: tuple[EvidenceRecord, ...], *kinds: EvidenceKind) -> tuple[str, ...]:
    """Current, sourced, non-assumption ids for the named evidence kinds."""
    wanted = set(kinds)
    return tuple(
        row.id
        for row in evidence
        if row.kind in wanted
        and row.source is not None
        and not row.stale()
        and row.quality.counts_toward_validation
    )


def assess_feasibility(
    *,
    evidence: tuple[EvidenceRecord, ...],
    market: MarketResearch | None,
    customer: CustomerValidation | None,
    financials: FinancialAnalysis | None,
    resource_snapshot: Mapping[str, Any] | None,
    inputs: Mapping[str, Any] | None = None,
) -> FeasibilityAssessment:
    data = dict(inputs or {})
    resources = dict(resource_snapshot or {})
    dimensions: list[FeasibilityDimension] = []
    blockers: list[str] = []
    gaps: list[str] = []

    # Technical: compare declared requirements against the machine actually
    # measured by the existing hardware monitor. No declared requirement means
    # the honest answer is "not supplied", not a made-up middle score.
    required_ram = data.get("technical_required_ram_gb")
    required_ram_value: float | None = None
    if required_ram is not None:
        try:
            required_ram_value = float(required_ram)
        except (TypeError, ValueError):
            required_ram_value = None
        if required_ram_value is not None and not math.isfinite(required_ram_value):
            required_ram_value = None
        if required_ram_value is None:
            gaps.append("the declared RAM requirement is not a finite number")
    requires_gpu = bool(data.get("technical_requires_gpu", False))
    technical_complexity = _score(data.get("technical_complexity"))
    technical_id = _ids(evidence, EvidenceKind.TECHNICAL)
    if (
        required_ram is None
        and technical_complexity is None
        and not technical_id
        and not requires_gpu
    ):
        dimensions.append(
            FeasibilityDimension(
                name="technical",
                score=None,
                missing_reason="no technical requirements or technical evidence was supplied",
            )
        )
        gaps.append("technical feasibility has no declared requirements or technical evidence")
    else:
        technical = 0.7
        rationale: list[str] = []
        if required_ram is not None and required_ram_value is None:
            dimensions.append(
                FeasibilityDimension(
                    name="technical",
                    score=None,
                    missing_reason="the declared RAM requirement is not a number",
                )
            )
        elif required_ram_value is not None:
            available = resources.get("available_ram_gb")
            if available is None:
                dimensions.append(
                    FeasibilityDimension(
                        name="technical",
                        score=None,
                        missing_reason="required RAM was supplied but no live memory reading is available",
                    )
                )
                gaps.append("technical feasibility lacks a live memory reading")
            else:
                try:
                    available_value = float(available)
                except (TypeError, ValueError):
                    available_value = None
                if available_value is not None and not math.isfinite(available_value):
                    available_value = None
                if available_value is None:
                    dimensions.append(
                        FeasibilityDimension(
                            name="technical",
                            score=None,
                            missing_reason="the live memory reading is not a number",
                        )
                    )
                    gaps.append("technical feasibility has an unreadable memory measurement")
                else:
                    fits = required_ram_value <= available_value
                    technical = 1.0 if fits else 0.05
                    rationale.append(
                        f"requires {required_ram_value:.2f} GB RAM; {available_value:.2f} GB is available"
                    )
                    if not fits:
                        blockers.append(
                            "declared RAM requirement exceeds available measured memory"
                        )
        if requires_gpu:
            gpu = bool(resources.get("gpu_available", False))
            technical = min(technical, 1.0 if gpu else 0.15)
            rationale.append("GPU required and " + ("available" if gpu else "not available"))
            if not gpu:
                blockers.append("the opportunity declares a GPU requirement, but no GPU is available")
        if technical_complexity is not None:
            technical = min(technical, 1.0 - technical_complexity)
            rationale.append(f"declared technical complexity {technical_complexity:.2f}")
        if not any(row.name == "technical" and row.score is None for row in dimensions):
            dimensions.append(
                FeasibilityDimension(
                    name="technical",
                    score=technical,
                    rationale="; ".join(rationale) or "technical evidence recorded",
                    evidence_ids=technical_id,
                )
            )

    # Operational and legal are supplied judgements, not inferred from a probe.
    for name, key in (("operational", "operational_score"), ("legal", "legal_score")):
        value = _score(data.get(key))
        evidence_ids = _ids(
            evidence,
            EvidenceKind.OPERATIONAL if name == "operational" else EvidenceKind.REGULATORY,
        )
        if value is None and not evidence_ids:
            dimensions.append(
                FeasibilityDimension(
                    name=name,
                    score=None,
                    missing_reason=f"no {name} assessment was supplied",
                )
            )
            gaps.append(f"{name} feasibility has no supplied assessment")
        else:
            if value is None:
                gaps.append(
                    f"{name} feasibility has recorded evidence but no supplied judgement"
                )
            dimensions.append(
                FeasibilityDimension(
                    name=name,
                    score=value,
                    rationale=data.get(f"{name}_notes", ""),
                    evidence_ids=evidence_ids,
                    missing_reason=(
                        ""
                        if value is not None
                        else f"no {name} judgement was supplied; evidence is recorded"
                    ),
                )
            )
    legal_blockers = data.get("legal_blockers") or []
    if isinstance(legal_blockers, str):
        legal_blockers = [legal_blockers]
    for blocker in legal_blockers:
        text = str(blocker).strip()
        if text:
            blockers.append(f"legal/regulatory: {text}")

    # Market feasibility derives from actual customer/market evidence, never
    # from the existence of a market-size figure alone.
    market_score: float | None = None
    market_rationale = ""
    if customer and customer.has_current_primary:
        market_score = 0.85 if customer.interview_count >= 3 else 0.65
        market_rationale = f"{customer.interview_count} current primary customer interview(s)"
    elif market and any(row.quality.measured for row in market.demand_indicators):
        market_score = 0.5
        market_rationale = "secondary demand evidence only; no primary customer record"
    elif market and market.demand_indicators:
        market_score = 0.3
        market_rationale = "estimated demand evidence only; no measured demand or primary customer record"
    elif market and market.trends:
        market_score = 0.35
        market_rationale = "market trend evidence only"
    if market_score is None:
        dimensions.append(
            FeasibilityDimension(
                name="market",
                score=None,
                missing_reason="no customer or market demand evidence was recorded",
            )
        )
        gaps.append("market feasibility has no demand evidence")
    else:
        dimensions.append(
            FeasibilityDimension(
                name="market",
                score=market_score,
                rationale=market_rationale,
                evidence_ids=_ids(
                    evidence,
                    EvidenceKind.CUSTOMER_INTERVIEW,
                    EvidenceKind.DEMAND_SIGNAL,
                ),
            )
        )

    # Resource fit is measured, not guessed.
    resource_score: float | None = None
    resource_rationale: list[str] = []
    available = resources.get("available_ram_gb")
    cpu = resources.get("cpu_percent")
    if available is not None:
        try:
            available_f = float(available)
            if not math.isfinite(available_f):
                raise ValueError
            resource_score = 1.0 if available_f >= 4.0 else (0.7 if available_f >= 2.0 else 0.3)
            resource_rationale.append(f"{available_f:.2f} GB RAM is currently available")
        except (TypeError, ValueError):
            resource_score = None
    if cpu is not None:
        try:
            cpu_f = float(cpu)
            if not math.isfinite(cpu_f):
                raise ValueError
            resource_rationale.append(f"CPU is at {cpu_f:.1f}%")
            if cpu_f >= 90:
                resource_score = min(resource_score or 1.0, 0.2)
        except (TypeError, ValueError):
            pass
    if resource_score is None:
        dimensions.append(
            FeasibilityDimension(
                name="resources",
                score=None,
                missing_reason="no live hardware reading is available",
            )
        )
        gaps.append("resource feasibility lacks a measured hardware reading")
    else:
        dimensions.append(
            FeasibilityDimension(
                name="resources",
                score=resource_score,
                rationale="; ".join(resource_rationale),
            )
        )

    financial_score, financial_reason = financial_feasibility_score(financials)
    if financial_score is None:
        dimensions.append(
            FeasibilityDimension(
                name="financial",
                score=None,
                missing_reason=financial_reason,
            )
        )
        gaps.append("financial feasibility is missing required assumptions")
    else:
        dimensions.append(
            FeasibilityDimension(
                name="financial",
                score=financial_score,
                rationale=financial_reason,
                evidence_ids=_ids(evidence, EvidenceKind.FINANCIAL),
            )
        )

    execution_complexity = _score(data.get("execution_complexity"))
    if execution_complexity is None:
        dimensions.append(
            FeasibilityDimension(
                name="execution",
                score=None,
                missing_reason="no execution-complexity judgement was supplied",
            )
        )
        gaps.append("execution complexity has no supplied judgement")
    else:
        dimensions.append(
            FeasibilityDimension(
                name="execution",
                score=1.0 - execution_complexity,
                rationale=f"supplied execution complexity {execution_complexity:.2f} (score is the inverse)",
            )
        )

    dependency_risks = data.get("dependency_risks") or []
    if isinstance(dependency_risks, str):
        dependency_risks = [dependency_risks]
    dependency_risks = [str(row).strip() for row in dependency_risks if str(row).strip()]
    if not dependency_risks and "dependency_risks" not in data:
        dimensions.append(
            FeasibilityDimension(
                name="dependencies",
                score=None,
                missing_reason="dependency risks were not supplied",
            )
        )
        gaps.append("dependency risk has no supplied assessment")
    else:
        score = max(0.0, 1.0 - (0.2 * len(dependency_risks)))
        dimensions.append(
            FeasibilityDimension(
                name="dependencies",
                score=score,
                rationale=(
                    "; ".join(dependency_risks)
                    if dependency_risks
                    else "no dependency risks were declared"
                ),
            )
        )
        if len(dependency_risks) >= 4:
            blockers.append("four or more declared dependency risks")

    available_scores = [row.score for row in dimensions if row.score is not None]
    overall = sum(available_scores) / len(available_scores) if available_scores else None
    complete = bool(dimensions) and all(row.score is not None for row in dimensions)
    return FeasibilityAssessment(
        dimensions=tuple(dimensions),
        overall_score=overall,
        blockers=tuple(dict.fromkeys(blockers)),
        gaps=tuple(dict.fromkeys(gaps)),
        resource_snapshot=resources,
        requirements=data,
        complete=complete,
        computed_at=utcnow(),
    )


__all__ = ["assess_feasibility"]
