"""Explainable aggregation, conflict preservation and advisory decisions.

Aggregation is deliberately not a vote.  It weights independent evidence
families, discounts correlation, obeys authoritative risk/portfolio
constraints, and preserves unresolved disagreement.  A decision never receives
a numeric "probability of success": no calibration method for the composite
exists, so the result reports qualitative evidence strength instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from novacontrol.novamarket.multiagent.models import (
    HORIZON_GROUPS,
    AgentDefinition,
    AgentPerformance,
    AgentResponse,
    AgentRunStatus,
    ConflictKind,
    ConflictRecord,
    ConflictSeverity,
    DecisionRecord,
    EvidenceKind,
    EvidenceRecord,
    FinancialAgentRole,
    Freshness,
    RecommendationAction,
    ValidationStatus,
    canonical_horizon,
    stable_id,
    text,
)

TRADING_DECISION_DEFINITION = AgentDefinition(
    "fin-trading-decision",
    "Trading Decision Agent",
    FinancialAgentRole.TRADING_DECISION,
    (
        "collect validated specialist outputs",
        "weigh independent evidence without majority voting",
        "preserve conflicts and authoritative restrictions",
        "produce an advisory recommendation for the declared horizon",
    ),
    "validated AgentResponse set + evidence",
    "DecisionRecord",
    (),
    10,
)

REQUIRED_AGENT_IDS = (
    "fin-technical",
    "fin-prediction",
    "fin-regime",
    "fin-risk",
    "fin-portfolio",
)
DIRECTIONAL_ACTIONS = {
    RecommendationAction.BUY,
    RecommendationAction.SELL,
}
ROLE_WEIGHT = {
    FinancialAgentRole.TECHNICAL_ANALYSIS: 1.0,
    FinancialAgentRole.PREDICTION_INTELLIGENCE: 1.15,
    FinancialAgentRole.FUNDAMENTAL_ANALYSIS: 0.9,
    FinancialAgentRole.MARKET_REGIME: 0.75,
    FinancialAgentRole.RL_POLICY_INTELLIGENCE: 0.55,
}
ACTION_LABEL = {
    RecommendationAction.BUY: "BUY",
    RecommendationAction.SELL: "SELL",
    RecommendationAction.HOLD: "HOLD",
    RecommendationAction.WAIT: "WAIT",
    RecommendationAction.NO_ACTION: "NO_ACTION",
    RecommendationAction.ABSTAIN: "ABSTAIN",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str_list(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _risk_findings(responses: Sequence[AgentResponse]) -> Mapping[str, Any]:
    for item in responses:
        if item.role is FinancialAgentRole.RISK_ANALYSIS and item.usable:
            return item.findings
    return {}


def _portfolio_findings(responses: Sequence[AgentResponse]) -> Mapping[str, Any]:
    for item in responses:
        if item.role is FinancialAgentRole.PORTFOLIO_INTELLIGENCE and item.usable:
            return item.findings
    return {}


def _risk_restrictions(responses: Sequence[AgentResponse]) -> list[str]:
    findings = _risk_findings(responses)
    restrictions = _str_list(findings.get("existing_restrictions"))
    if not findings:
        return restrictions
    breaches = findings.get("breaches")
    has_breaches = False
    if isinstance(breaches, Mapping) or (
        isinstance(breaches, Sequence) and not isinstance(breaches, (str, bytes))
    ):
        has_breaches = bool(breaches)
    if has_breaches:
        restrictions.append("NM-08 reported one or more portfolio breaches")
    outcome = text(findings.get("risk_summary")).lower()
    if outcome and outcome not in {
        "approved",
        "accepted",
        "ok",
        "passed",
        "within_limits",
    }:
        restrictions.append(f"NM-08 risk outcome is {outcome}")
    return list(dict.fromkeys(restrictions))


@dataclass(frozen=True, slots=True)
class AggregationResult:
    decision: DecisionRecord
    conflicts: tuple[ConflictRecord, ...]


def _conflict(
    cycle_id: str,
    index: int,
    *,
    kind: ConflictKind,
    severity: ConflictSeverity,
    agents: Sequence[str],
    description: str,
    evidence_ids: Sequence[str],
    rule: str,
    resolved: bool,
    resolution: str,
    preserved: bool = True,
) -> ConflictRecord:
    return ConflictRecord(
        conflict_id=stable_id("mac_", cycle_id, kind.value, index, description),
        cycle_id=cycle_id,
        kind=kind,
        severity=severity,
        agents=tuple(agents),
        description=description,
        evidence_ids=tuple(dict.fromkeys(evidence_ids)),
        rule=rule,
        resolved=resolved,
        resolution=resolution,
        preserved=preserved,
    )


def detect_conflicts(
    *,
    cycle_id: str,
    responses: Sequence[AgentResponse],
    evidence: Sequence[EvidenceRecord],
    required_agents: Sequence[str],
    horizon: str,
) -> tuple[ConflictRecord, ...]:
    """Build structured conflicts without erasing disagreement."""
    conflicts: list[ConflictRecord] = []
    usable = [item for item in responses if item.usable]
    buy = [item.agent_id for item in usable if item.action is RecommendationAction.BUY]
    sell = [item.agent_id for item in usable if item.action is RecommendationAction.SELL]
    if buy and sell:
        conflicts.append(
            _conflict(
                cycle_id,
                len(conflicts),
                kind=ConflictKind.DIRECTION,
                severity=ConflictSeverity.MATERIAL,
                agents=(*buy, *sell),
                description=(
                    "specialist agents disagree on direction: "
                    + ", ".join(f"{item} BUY" for item in buy)
                    + "; "
                    + ", ".join(f"{item} SELL" for item in sell)
                ),
                evidence_ids=[
                    evidence_id
                    for item in usable
                    if item.action in DIRECTIONAL_ACTIONS
                    for evidence_id in (e.evidence_id for e in item.evidence)
                ],
                rule="conflicting forecasts and interpretations must remain visible",
                resolved=False,
                resolution="the final decision may WAIT/HOLD and records both sides",
            )
        )
    for item in usable:
        if (
            item.role is FinancialAgentRole.PREDICTION_INTELLIGENCE
            and item.findings.get("model_disagreement")
        ):
            conflicts.append(
                _conflict(
                    cycle_id,
                    len(conflicts),
                    kind=ConflictKind.FORECAST_DISAGREEMENT,
                    severity=ConflictSeverity.MATERIAL,
                    agents=(item.agent_id,),
                    description="stored forecast models disagree for the target horizon",
                    evidence_ids=[row.evidence_id for row in item.evidence],
                    rule="model disagreement must be preserved, not averaged away",
                    resolved=False,
                    resolution="uncertainty is reported and may force WAIT",
                )
            )
        if item.role is FinancialAgentRole.PREDICTION_INTELLIGENCE and (
            item.findings.get("forecast_uncertainty") is True
        ):
            conflicts.append(
                _conflict(
                    cycle_id,
                    len(conflicts),
                    kind=ConflictKind.HIGH_UNCERTAINTY,
                    severity=ConflictSeverity.MATERIAL,
                    agents=(item.agent_id,),
                    description="forecast evidence reports high uncertainty",
                    evidence_ids=[row.evidence_id for row in item.evidence],
                    rule="high uncertainty cannot be converted into high confidence",
                    resolved=False,
                    resolution="the recommendation is downgraded or abstains",
                )
            )
        if item.role is FinancialAgentRole.RL_POLICY_INTELLIGENCE and (
            item.action in DIRECTIONAL_ACTIONS
            and item.action
            not in {
                RecommendationAction.BUY
                if buy and not sell
                else RecommendationAction.SELL
                if sell and not buy
                else RecommendationAction.ABSTAIN
            }
        ):
                conflicts.append(
                    _conflict(
                        cycle_id,
                        len(conflicts),
                        kind=ConflictKind.RL_CONFLICT,
                        severity=ConflictSeverity.INFO,
                        agents=(item.agent_id,),
                        description="experimental RL proposal differs from other evidence",
                        evidence_ids=[row.evidence_id for row in item.evidence],
                        rule=(
                            "experimental policies are advisory and weighted below "
                            "verified evidence"
                        ),
                        resolved=True,
                        resolution="policy evidence is retained with a discounted weight",
                    )
                )
    stale = [
        row
        for row in evidence
        if row.freshness is Freshness.STALE
        or row.validation is ValidationStatus.INVALID
    ]
    if stale:
        conflicts.append(
            _conflict(
                cycle_id,
                len(conflicts),
                kind=ConflictKind.STALE_DATA,
                severity=ConflictSeverity.AUTHORITATIVE,
                agents=tuple(sorted({row.agent_id for row in stale})),
                description=f"{len(stale)} stale or invalid evidence item(s) are present",
                evidence_ids=[row.evidence_id for row in stale],
                rule="stale or invalid data must prevent an unsupported actionable recommendation",
                resolved=True,
                resolution="the final decision cannot be BUY or SELL",
            )
        )
    restrictions = _risk_restrictions(responses)
    if restrictions:
        conflicts.append(
            _conflict(
                cycle_id,
                len(conflicts),
                kind=ConflictKind.RISK_RESTRICTION,
                severity=ConflictSeverity.AUTHORITATIVE,
                agents=("fin-risk",),
                description="; ".join(restrictions),
                evidence_ids=[
                    row.evidence_id
                    for item in responses
                    if item.role is FinancialAgentRole.RISK_ANALYSIS
                    for row in item.evidence
                ],
                rule="NM-08 restrictions cannot be overridden by any agent",
                resolved=True,
                resolution="actionable BUY/SELL is blocked and NM-08 remains authoritative",
            )
        )
    portfolio = _portfolio_findings(responses)
    if _str_list(portfolio.get("portfolio_warnings")):
        hard = portfolio.get("opportunity_compatibility") is False or (
            float(portfolio.get("concentration") or 0.0) > 0.5
        )
        conflicts.append(
            _conflict(
                cycle_id,
                len(conflicts),
                kind=ConflictKind.PORTFOLIO_CONSTRAINT,
                severity=ConflictSeverity.AUTHORITATIVE
                if hard
                else ConflictSeverity.MATERIAL,
                agents=("fin-portfolio",),
                description="; ".join(_str_list(portfolio.get("portfolio_warnings"))),
                evidence_ids=[
                    row.evidence_id
                    for item in responses
                    if item.role is FinancialAgentRole.PORTFOLIO_INTELLIGENCE
                    for row in item.evidence
                ],
                rule="authoritative portfolio constraints must be respected",
                resolved=bool(hard),
                resolution="a new long action is blocked"
                if hard
                else "the final decision records the portfolio warning",
            )
        )
    failed_required = [
        item.agent_id
        for item in responses
        if item.agent_id in required_agents
        and item.status
        in {
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.INVALID,
            AgentRunStatus.SKIPPED,
        }
    ]
    insufficient_required = [
        item.agent_id
        for item in responses
        if item.agent_id in required_agents
        and item.status is AgentRunStatus.INSUFFICIENT_EVIDENCE
    ]
    missing_required = list(dict.fromkeys([*failed_required, *insufficient_required]))
    if missing_required:
        conflicts.append(
            _conflict(
                cycle_id,
                len(conflicts),
                kind=ConflictKind.AGENT_FAILURE
                if failed_required
                else ConflictKind.MISSING_EVIDENCE,
                severity=ConflictSeverity.AUTHORITATIVE,
                agents=tuple(missing_required),
                description="required agent evidence is unavailable: "
                + ", ".join(missing_required),
                evidence_ids=[],
                rule="a required failure must not be presented as successful analysis",
                resolved=True,
                resolution="the decision is NO_ACTION or WAIT",
            )
        )
    return tuple(conflicts)


def _freshness_factor(rows: Sequence[EvidenceRecord]) -> float:
    if not rows:
        return 0.0
    usable = [
        row
        for row in rows
        if row.validation
        not in {ValidationStatus.INVALID, ValidationStatus.UNVERIFIED}
    ]
    if not usable:
        return 0.0
    values = {
        Freshness.FRESH: 1.0,
        Freshness.DELAYED: 0.6,
        Freshness.STALE: 0.0,
        Freshness.UNKNOWN: 0.2,
    }
    return sum(values[row.freshness] for row in usable) / len(usable)


def _independence_factor(
    response: AgentResponse,
    seen_groups: Mapping[str, str],
) -> tuple[float, list[str]]:
    """Discount an agent that repeats another agent's evidence family.

    An agent with no provenance is treated as a hypothesis and cannot earn more
    than a small fraction of a verified, independent source.
    """
    groups: list[str] = []
    for row in response.evidence:
        group = row.correlation_group or row.source_identity
        if group:
            groups.append(group)
    if not groups:
        return 0.15, []
    unique = [group for group in groups if group not in seen_groups]
    if not unique:
        return 0.2, []
    independent_verified = any(
        row.kind is EvidenceKind.VERIFIED and row.independent for row in response.evidence
    )
    base = 1.0 if independent_verified else 0.55
    return base * (len(unique) / len(groups)), unique


def aggregate(
    *,
    cycle_id: str,
    instrument_id: str,
    exchange: str,
    horizon: str,
    responses: Sequence[AgentResponse],
    evidence: Sequence[EvidenceRecord],
    required_agents: Sequence[str],
    conflicts: Sequence[ConflictRecord],
    reliability: Mapping[str, AgentPerformance] | None = None,
    degraded: bool = False,
    decided_at: datetime | None = None,
) -> DecisionRecord:
    canonical_horizon(horizon)
    usable = [item for item in responses if item.usable]
    reliability = reliability or {}
    groups_seen: dict[str, str] = {}
    support_score = 0.0
    oppose_score = 0.0
    support_agents: list[str] = []
    contradict_agents: list[str] = []
    supporting_evidence: list[str] = []
    contradictory_evidence: list[str] = []
    weighted_components: list[dict[str, Any]] = []
    for item in usable:
        if item.action not in DIRECTIONAL_ACTIONS:
            continue
        factor, new_groups = _independence_factor(item, groups_seen)
        fresh = _freshness_factor(item.evidence)
        perf = reliability.get(item.agent_id)
        historical = (
            perf.historical_quality
            if perf is not None and perf.executions >= 5 and perf.historical_quality is not None
            else 0.6
        )
        weight = ROLE_WEIGHT.get(item.role, 0.5) * fresh * factor
        weight *= 0.75 + 0.5 * max(0.0, min(1.0, float(historical)))
        # One agent can only vote once, regardless of how many pieces of
        # correlated evidence it returns.
        weight = min(weight, 1.2)
        for group in new_groups:
            groups_seen.setdefault(group, item.agent_id)
        if item.action is RecommendationAction.BUY:
            support_score += weight
            support_agents.append(item.agent_id)
        else:
            oppose_score += weight
            contradict_agents.append(item.agent_id)
        weighted_components.append(
            {
                "agent_id": item.agent_id,
                "role": item.role.value,
                "action": item.action.value,
                "weight": round(weight, 6),
                "freshness_factor": round(fresh, 6),
                "independence_factor": round(factor, 6),
                "new_evidence_families": new_groups,
            }
        )
        target = (
            supporting_evidence
            if item.action is RecommendationAction.BUY
            else contradictory_evidence
        )
        target.extend(row.evidence_id for row in item.evidence)
    risk_restrictions = _risk_restrictions(responses)
    portfolio = _portfolio_findings(responses)
    held = bool(_mapping(portfolio.get("position_context")).get("held"))
    no_cash = portfolio.get("opportunity_compatibility") is False
    concentration = float(portfolio.get("concentration") or 0.0)
    exhausted = any(
        item.status is AgentRunStatus.FAILED for item in responses
    )
    missing_required = [
        item.agent_id
        for item in responses
        if item.agent_id in required_agents
        and item.status
        in {
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.INVALID,
            AgentRunStatus.SKIPPED,
            AgentRunStatus.INSUFFICIENT_EVIDENCE,
        }
    ]
    returned_agents = {item.agent_id for item in responses}
    missing_required.extend(
        agent_id for agent_id in required_agents if agent_id not in returned_agents
    )
    missing_required = list(dict.fromkeys(missing_required))
    stale_critical = any(
        row.freshness is Freshness.STALE
        or row.validation in {ValidationStatus.INVALID, ValidationStatus.UNVERIFIED}
        for row in evidence
    )
    high_uncertainty = any(
        item.findings.get("forecast_uncertainty") is True for item in usable
    )
    directional_conflict = support_score > 0 and oppose_score > 0
    independent_families = len(
        {
            row.correlation_group or row.source_identity
            for row in evidence
            if row.independent and row.kind is EvidenceKind.VERIFIED
            and row.validation in {ValidationStatus.VALID, ValidationStatus.PARTIAL}
            and row.freshness in {Freshness.FRESH, Freshness.DELAYED}
        }
    )
    if independent_families >= 4 and not high_uncertainty:
        evidence_strength = "strong"
    elif independent_families >= 2:
        evidence_strength = "moderate"
    elif independent_families >= 1:
        evidence_strength = "weak"
    else:
        evidence_strength = "insufficient"
    reason = ""
    if missing_required:
        action = RecommendationAction.NO_ACTION
        reason = "required evidence is missing or failed: " + ", ".join(missing_required)
    elif stale_critical:
        action = RecommendationAction.WAIT
        reason = "stale critical evidence prevents an actionable recommendation"
    elif high_uncertainty and (support_score or oppose_score):
        action = RecommendationAction.WAIT
        reason = "forecast uncertainty is too high for an actionable recommendation"
    elif risk_restrictions:
        action = RecommendationAction.HOLD if held else RecommendationAction.WAIT
        reason = "NM-08 restrictions block a new actionable recommendation"
    elif no_cash and support_score > oppose_score:
        action = RecommendationAction.HOLD if held else RecommendationAction.WAIT
        reason = "portfolio constraints block a new long action"
    elif concentration > 0.5 and support_score > oppose_score:
        action = RecommendationAction.HOLD
        reason = "portfolio concentration blocks further exposure"
    elif directional_conflict:
        action = RecommendationAction.HOLD if held else RecommendationAction.WAIT
        reason = "agents disagree on direction; disagreement is preserved"
    elif support_score > max(0.9, oppose_score * 1.3):
        action = RecommendationAction.BUY
        reason = "independent fresh evidence supports the upside case"
    elif oppose_score > max(0.9, support_score * 1.3):
        action = RecommendationAction.SELL
        reason = "independent fresh evidence supports the downside case"
    elif support_score or oppose_score:
        action = RecommendationAction.HOLD
        reason = "directional evidence is not strong enough to act"
    elif exhausted:
        action = RecommendationAction.NO_ACTION
        reason = "no specialist produced usable evidence"
    else:
        action = RecommendationAction.NO_ACTION
        reason = "evidence is insufficient for a recommendation"
    if action is RecommendationAction.BUY and risk_restrictions:
        action = RecommendationAction.WAIT
        reason = "NM-08 restriction vetoed an upside recommendation"
    if action is RecommendationAction.SELL and portfolio.get("position_context") and not held:
        action = RecommendationAction.WAIT
        reason = "no authoritative position exists to sell"
    unresolved = [item for item in conflicts if not item.resolved]
    support_set = tuple(dict.fromkeys(support_agents))
    contradict_set = tuple(dict.fromkeys(contradict_agents))
    portfolio_ctx = {
        "held": held,
        "available_capital": portfolio.get("available_capital"),
        "cash_weight": portfolio.get("cash_weight"),
        "concentration": portfolio.get("concentration"),
        "opportunity_compatibility": portfolio.get("opportunity_compatibility"),
    }
    explanation = (
        f"{ACTION_LABEL[action]} for {instrument_id} ({exchange}) on the {horizon} "
        f"({HORIZON_GROUPS[horizon]}) horizon. {reason}. "
        f"Independent verified evidence families: {independent_families}; "
        f"weighted upside {support_score:.2f} versus downside {oppose_score:.2f}. "
    )
    if unresolved:
        explanation += (
            f"{len(unresolved)} unresolved conflict(s) remain visible. "
        )
    explanation += (
        "This is advisory research only. NM-08 must reassess risk and PI-12/NM-10 "
        "remain the only path to a paper order; no order was submitted."
    )
    forecast_components = [
        {
            "agent_id": item.agent_id,
            "confidence": item.confidence,
            "confidence_kind": item.confidence_kind,
        }
        for item in usable
        if item.confidence is not None
    ]
    decision = DecisionRecord(
        decision_id=stable_id("mad_", cycle_id),
        cycle_id=cycle_id,
        instrument_id=instrument_id,
        exchange=exchange,
        horizon=horizon,
        action=action,
        supporting_agents=support_set,
        contradicting_agents=contradict_set,
        supporting_evidence=tuple(dict.fromkeys(supporting_evidence)),
        contradictory_evidence=tuple(dict.fromkeys(contradictory_evidence))
        + tuple(
            row.evidence_id
            for row in evidence
            if row.validation in {ValidationStatus.INVALID, ValidationStatus.UNVERIFIED}
            and row.evidence_id not in contradictory_evidence
        ),
        evidence_strength=evidence_strength,
        confidence=None,
        confidence_kind="unavailable_no_calibrated_decision_model",
        confidence_provenance={
            "decision_confidence": "not computed",
            "reason": (
                "no calibration maps heterogeneous agent evidence to a probability "
                "of profitable execution"
            ),
            "forecast_components": forecast_components,
            "component_probabilities_are_not_decision_probabilities": True,
            "evidence_strength": evidence_strength,
            "independent_verified_families": independent_families,
            "weighted_components": weighted_components,
        },
        uncertainty={
            "forecast_uncertainty": high_uncertainty,
            "cross_agent_disagreement": directional_conflict,
            "model_disagreement": any(
                item.findings.get("model_disagreement") for item in usable
            ),
            "missing_information": [
                item.agent_id for item in responses if item.missing
            ],
            "stale_evidence": stale_critical,
            "degraded_cycle": degraded or exhausted,
        },
        portfolio_context=portfolio_ctx,
        risk_restrictions=tuple(risk_restrictions),
        unresolved_conflicts=tuple(item.conflict_id for item in unresolved),
        explanation=explanation,
        decided_at=decided_at or datetime.now(UTC),
        degraded=degraded or exhausted,
        risk_review_required=True,
        handoff_required=True,
        order_submitted=False,
        production_authority=False,
    )
    return decision
