"""Deterministic, traceable sales assistance.

The brief is assembled only from recorded product, qualification, research,
interaction, objection and proposal data. It does not invent capabilities,
discounts, contractual terms or competitor claims.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.customer.models import new_id, utcnow


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(row).strip() for row in value if str(row).strip()]
    text = _text(value)
    return [text] if text else []


def _source(kind: str, record_id: Any, label: str) -> dict[str, str]:
    return {"type": kind, "id": str(record_id or ""), "label": _text(label)}


_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "no",
    "not",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _text(value).lower())
        if len(token) > 1 and token not in _STOP_WORDS
    }


def sales_brief(
    workspace: Any,
    *,
    lead: Mapping[str, Any] | None = None,
    deal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one sales brief from records already in the workspace."""
    if lead is None and deal is None:
        raise ValueError("A sales brief needs a lead or a deal.")
    if lead is None and deal is not None and deal.get("lead_id"):
        lead = workspace.lead(str(deal.get("lead_id")))
    customer_id = str((deal or {}).get("customer_id") or (lead or {}).get("customer_id") or "")
    customer = workspace.customer(customer_id) if customer_id else None
    segment_id = str((lead or {}).get("segment_id") or (customer or {}).get("segment_id") or "")
    segment = workspace.segment(segment_id) if segment_id else None
    icps = [row for row in workspace.icps if row.get("segment_id") == segment_id] if segment_id else []
    qualification = (lead or {}).get("qualification") or {}
    sources: list[dict[str, str]] = []
    requirements: list[str] = []
    if qualification:
        sources.append(_source("lead_qualification", qualification.get("id"), "Recorded qualification"))
        for row in qualification.get("criteria") or []:
            evidence = _text(row.get("evidence"))
            if evidence:
                requirements.append(f"{row.get('name')}: {evidence}")
    if segment:
        sources.append(_source("customer_segment", segment.get("id"), segment.get("name", "")))
        requirements.extend(_strings(segment.get("needs")))
        requirements.extend(_strings(segment.get("pain_points")))
    if customer:
        sources.append(_source("customer", customer.get("id"), customer.get("name", "")))
    for icp in icps:
        sources.append(_source("icp", icp.get("id"), icp.get("name", "")))
        requirements.extend(_strings(icp.get("needs")))
        requirements.extend(_strings(icp.get("pain_points")))
    if not requirements:
        requirements = ["No verified customer requirement is recorded for this lead/deal."]
        requirement_state = "missing"
    else:
        requirement_state = "recorded"
    features = _strings(workspace.product_snapshot.get("features"))
    fit_rows = []
    for requirement in requirements[:12]:
        requirement_tokens = _tokens(requirement)
        matches = [
            feature for feature in features if requirement_tokens & _tokens(feature)
        ]
        fit_rows.append(
            {
                "requirement": requirement,
                "documented_features": matches,
                "fit": "documented" if matches else "not documented",
            }
        )
    research = []
    for row in workspace.research:
        if segment_id and row.get("segment_id") not in {"", segment_id}:
            continue
        research.append(
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "summary": row.get("summary"),
                "source": row.get("source_url") or row.get("source_reference"),
                "verification_status": row.get("verification_status"),
                "confidence": row.get("confidence"),
            }
        )
        sources.append(_source("research", row.get("id"), row.get("title", "")))
    related_ids = {
        str((lead or {}).get("id") or ""),
        str((deal or {}).get("id") or ""),
        customer_id,
    }
    related_ids.discard("")
    interactions = [
        row for row in workspace.interactions if str(row.get("related_id")) in related_ids
    ][-5:]
    for row in interactions:
        sources.append(_source("interaction", row.get("id"), row.get("kind", "interaction")))
    objections = _strings((deal or {}).get("objections"))
    competitors = _strings((deal or {}).get("competitor_alternatives"))
    proposals = list((deal or {}).get("proposal_history") or [])[-2:]
    for row in proposals:
        sources.append(_source("proposal", row.get("id"), row.get("title", "Proposal")))
    missing = _strings(qualification.get("missing_information"))
    discovery = [f"What evidence would confirm {item.lower()}?" for item in missing[:6]]
    if not discovery:
        discovery = [
            "Which outcome matters most, and how is it measured today?",
            "Who makes the decision and who influences it?",
            "What budget and timeline are actually recorded?",
        ]
    pricing = workspace.product_snapshot.get("pricing_assumptions") or {}
    if pricing:
        pricing_line = f"Recorded pricing assumption: {pricing}."
    else:
        pricing_line = "No pricing is recorded; no price or discount is proposed."
    proposal_lines = [
        f"Product: {workspace.product_snapshot.get('name', '')} "
        f"version {workspace.product_snapshot.get('version') or 'not recorded'}.",
        "Documented capabilities: " + ("; ".join(features) if features else "not recorded") + ".",
        pricing_line,
        "Commercial terms, discounts and contractual commitments are not authorized by this draft.",
    ]
    if proposals:
        proposal_lines.append(
            "Prior proposal records: "
            + "; ".join(f"{row.get('title')} (value {row.get('value')})" for row in proposals)
        )
    follow_up = _strings((lead or {}).get("next_action")) or _strings((deal or {}).get("next_action"))
    if missing:
        follow_up.append("Obtain missing qualification evidence: " + "; ".join(missing[:6]))
    if not follow_up:
        follow_up = ["Record the next action explicitly; none is stored."]
    summary = [
        f"{row.get('created_at')}: {row.get('kind')} — {row.get('summary')}"
        for row in interactions
    ]
    if not summary:
        summary = ["No interaction is recorded; no conversation summary is inferred."]
    return {
        "id": new_id("sales-brief"),
        "product_id": workspace.product_id,
        "lead_id": str((lead or {}).get("id", "")),
        "deal_id": str((deal or {}).get("id", "")),
        "customer_id": customer_id,
        "customer_requirement_analysis": {
            "state": requirement_state,
            "requirements": requirements[:12],
            "note": "Derived from recorded qualification evidence and segment/ICP needs.",
        },
        "product_fit": {
            "documented_features": features,
            "rows": fit_rows,
            "note": "An undocumented fit is a gap to verify, not a claim that the product cannot do it.",
        },
        "sales_research": {
            "records": research[:6],
            "note": "Only recorded research is shown; public research is not direct validation.",
        },
        "meeting_preparation": {
            "agenda": [
                "Confirm the recorded problem and desired outcome.",
                "Review documented capabilities against the recorded requirements.",
                "Identify missing decision, budget and timeline evidence.",
                "Agree one explicit next action.",
            ],
            "discovery_questions": discovery,
        },
        "discovery_questions": discovery,
        "objection_analysis": [
            {
                "objection": objection,
                "recorded_response": "No response is recorded. Confirm the concern and cite only approved documentation.",
            }
            for objection in objections
        ],
        "proposal_draft": {
            "lines": proposal_lines,
            "source_references": [row.get("source_reference") for row in proposals if row.get("source_reference")],
            "draft_only": True,
            "authorized_discount": False,
            "authorized_contract_terms": False,
        },
        "follow_up_recommendations": follow_up,
        "competitor_comparison": {
            "recorded_alternatives": competitors,
            "note": (
                "Only recorded alternatives are listed. No comparative superiority claim is made."
                if competitors
                else "No competitor alternative is recorded for this deal."
            ),
        },
        "conversation_summary": summary,
        "sources": sources,
        "automated_recommendation": True,
        "generated_by": "deterministic_sales_assistance",
        "boundary": {
            "invented_capabilities": False,
            "unauthorized_discount": False,
            "contractual_commitment": False,
        },
        "created_at": utcnow().isoformat(),
    }


__all__ = ["sales_brief"]
