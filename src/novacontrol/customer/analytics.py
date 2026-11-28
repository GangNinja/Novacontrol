"""Customer-growth analytics computed only from recorded state.

Every unavailable figure is returned as an explicit unavailable block. Pipeline
value is never called revenue, lead scores are never called demand, and customer
satisfaction/retention are unavailable until actual observations exist.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Mapping

from novacontrol.customer.models import (
    FeedbackKind,
    LeadStatus,
    TicketStatus,
)


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "value": None, "reason": reason}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _days_between(start: Any, end: Any) -> float | None:
    first = _date(start)
    last = _date(end)
    if first is None or last is None:
        return None
    return max(0.0, (last - first).total_seconds() / 86400.0)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def analytics_snapshot(workspace: Any) -> dict[str, Any]:
    """Return the four analytics families with provenance-aware availability."""
    leads = list(workspace.leads)
    customers = list(workspace.customers)
    deals = list(workspace.deals)
    tickets = list(workspace.tickets)
    feedback = list(workspace.feedback)
    costs = list(workspace.acquisition_costs)

    qualified_leads = [
        lead
        for lead in leads
        if lead.get("status")
        in {
            LeadStatus.QUALIFIED.value,
            LeadStatus.CONTACT_PENDING_APPROVAL.value,
            LeadStatus.IN_CONVERSATION.value,
            LeadStatus.CONVERTED.value,
        }
        or (lead.get("qualification") or {}).get("recommended_status")
        == LeadStatus.QUALIFIED.value
    ]
    converted = [
        lead for lead in leads if lead.get("status") == LeadStatus.CONVERTED.value
    ]
    source_counts = Counter(str(lead.get("source", "unknown")) for lead in leads)
    segment_counts = Counter(
        str(lead.get("segment_id") or "unassigned") for lead in leads
    )
    acquisition_cost = None
    cost_currency = ""
    valid_costs = [row for row in costs if _number(row.get("amount")) is not None]
    cost_currencies = {
        str(row.get("currency") or "").upper() for row in valid_costs
    }
    if len(cost_currencies) == 1:
        acquisition_cost = round(
            sum(_number(row.get("amount")) or 0.0 for row in valid_costs), 2
        )
        cost_currency = next(iter(cost_currencies))
    acquisition = {
        "leads_generated": len(leads),
        "lead_sources": dict(source_counts),
        "segment_distribution": dict(segment_counts),
        "qualified_leads": len(qualified_leads),
        "qualification_rate": _rate(len(qualified_leads), len(leads)),
        "converted_leads": len(converted),
        "acquisition_cost": (
            {
                "available": True,
                "value": acquisition_cost,
                "currency": cost_currency,
                "records": len(valid_costs),
                "note": "Sum of recorded actual acquisition costs only.",
            }
            if acquisition_cost is not None
            else _unavailable(
                "No single-currency actual acquisition-cost total exists; CAC is not fabricated."
                if cost_currencies
                else "No actual acquisition-cost records exist; CAC is not fabricated."
            )
        ),
    }

    open_deals = [
        deal
        for deal in deals
        if deal.get("stage") not in {"closed_won", "closed_lost"}
    ]
    pipeline_value = sum(
        value
        for value in (_number(deal.get("value")) for deal in open_deals)
        if value is not None
    )
    open_currencies = {str(deal.get("currency") or "").upper() for deal in open_deals}
    all_currencies = {str(deal.get("currency") or "").upper() for deal in deals}
    won = [deal for deal in deals if deal.get("stage") == "closed_won"]
    lost = [deal for deal in deals if deal.get("stage") == "closed_lost"]
    realized = [
        (deal, value)
        for deal in won
        for value in (_number(deal.get("realized_amount")),)
        if value is not None
    ]
    realized_values = [value for _deal, value in realized]
    realized_currencies = {
        str(deal.get("currency") or "").upper() for deal, _value in realized
    }
    stage_history: dict[str, int] = defaultdict(int)
    for deal in deals:
        seen: set[str] = set()
        for row in deal.get("stage_history") or []:
            stage = str(row.get("stage", ""))
            if stage and stage not in seen:
                seen.add(stage)
                stage_history[stage] += 1
    ordered = sorted(workspace.pipeline_stages, key=lambda row: int(row.get("order", 0)))
    stage_rates: list[dict[str, Any]] = []
    for previous, current in zip(ordered, ordered[1:]):
        before = stage_history.get(str(previous.get("id")), 0)
        after = stage_history.get(str(current.get("id")), 0)
        stage_rates.append(
            {
                "from": previous.get("id"),
                "to": current.get("id"),
                "rate": _rate(after, before),
                "from_count": before,
                "to_count": after,
            }
        )
    cycles = [
        days
        for days in (
            _days_between(deal.get("created_at"), deal.get("closed_at")) for deal in won + lost
        )
        if days is not None
    ]
    lost_reasons = Counter(
        str(deal.get("lost_reason", "not recorded")) for deal in lost
    )
    sales = {
        "pipeline_value": (
            {
                "available": True,
                "value": round(pipeline_value, 2),
                "currency": next(iter(open_currencies)),
                "deal_count": len(open_deals),
                "estimate": True,
                "note": "Sum of recorded open-deal estimates; not realized revenue.",
            }
            if open_deals and len(open_currencies) == 1
            else _unavailable(
                "Open deals use more than one currency; no conversion is assumed."
                if len(open_currencies) > 1
                else "No open deal is recorded."
            )
        ),
        "deal_value": (
            {
                "available": True,
                "value": round(sum(
                    value
                    for value in (_number(deal.get("value")) for deal in deals)
                    if value is not None
                ), 2),
                "estimate": True,
            }
            if deals and len(all_currencies) == 1
            else _unavailable(
                "Deals use more than one currency; no conversion is assumed."
                if len(all_currencies) > 1
                else "No deal is recorded."
            )
        ),
        "realized_revenue": (
            {
                "available": True,
                "value": round(sum(realized_values), 2),
                "currency": next(iter(realized_currencies)),
                "note": "Only explicitly recorded realized amounts are included.",
            }
            if realized_values and len(realized_currencies) == 1
            else _unavailable(
                "Realized amounts use more than one currency; no conversion is assumed."
                if len(realized_currencies) > 1
                else "Closed-won deal value is an estimate unless an actual realized amount is recorded."
            )
        ),
        "lead_to_qualified_conversion": _rate(len(qualified_leads), len(leads)),
        "qualified_to_customer_conversion": _rate(len(converted), len(qualified_leads)),
        "stage_conversion_rates": stage_rates,
        "sales_cycle_days": (
            round(sum(cycles) / len(cycles), 2)
            if cycles
            else _unavailable("No closed deal has both a creation and close timestamp.")
        ),
        "win_loss_ratio": (
            round(len(won) / len(lost), 4)
            if lost
            else _unavailable("No closed-lost deal has been recorded.")
        ),
        "won_deals": len(won),
        "lost_deals": len(lost),
        "lost_deal_reasons": dict(lost_reasons),
    }

    open_tickets = [
        ticket
        for ticket in tickets
        if ticket.get("status") not in {TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value}
    ]
    resolved = [
        ticket
        for ticket in tickets
        if ticket.get("status") in {TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value}
    ]
    resolution_days = [
        days
        for days in (
            _days_between(ticket.get("created_at"), ticket.get("resolved_at"))
            for ticket in resolved
        )
        if days is not None
    ]
    escalated = [
        ticket
        for ticket in tickets
        if ticket.get("status") == TicketStatus.ESCALATED.value
        or ticket.get("escalation_history")
    ]
    reopened = [ticket for ticket in tickets if int(ticket.get("reopen_count", 0) or 0) > 0]
    categories = Counter(str(ticket.get("category", "unknown")) for ticket in tickets)
    ratings = [
        _number(row.get("rating"))
        for row in feedback
        if row.get("kind") == FeedbackKind.SATISFACTION.value
        and _number(row.get("rating")) is not None
    ]
    satisfaction = (
        {
            "available": True,
            "value": round(sum(rating for rating in ratings if rating is not None) / len(ratings), 2),
            "responses": len(ratings),
            "scale": "recorded customer rating",
        }
        if ratings
        else _unavailable("No customer satisfaction rating has been recorded.")
    )
    support = {
        "open_tickets": len(open_tickets),
        "total_tickets": len(tickets),
        "resolution_time_days": (
            round(sum(resolution_days) / len(resolution_days), 2)
            if resolution_days
            else _unavailable("No resolved ticket has both required timestamps.")
        ),
        "escalation_rate": _rate(len(escalated), len(tickets)),
        "ticket_categories": dict(categories),
        "reopened_tickets": len(reopened),
        "customer_satisfaction": satisfaction,
    }

    churned = [customer for customer in customers if customer.get("status") == "churned"]
    onboarding = list(workspace.onboarding)
    complete_onboarding = [
        row for row in onboarding if row.get("status") == "complete"
    ]
    adoption_values: list[float] = []
    for row in onboarding:
        score = _number(row.get("adoption_score"))
        if score is not None:
            adoption_values.append(score)
    repeat_issue_customers = {
        str(ticket.get("customer_id"))
        for ticket in tickets
        if ticket.get("customer_id")
        and sum(1 for row in tickets if row.get("customer_id") == ticket.get("customer_id")) > 1
    }
    churn_feedback = [
        row for row in feedback if row.get("kind") == FeedbackKind.CHURN_REASON.value
    ]
    retention = {
        "customers": len(customers),
        "churned_customers": len(churned),
        "churn_rate": _rate(len(churned), len(customers)),
        "retention_rate": (
            round(1.0 - (len(churned) / len(customers)), 4) if customers else None
        ),
        "retention_rate_available": bool(customers),
        "retention_rate_reason": (
            "Retention is based on recorded customer status."
            if customers
            else "No customers are recorded; retention is unavailable."
        ),
        "onboarding_started": len(onboarding),
        "onboarding_completed": len(complete_onboarding),
        "adoption_score": (
            round(sum(adoption_values) / len(adoption_values), 2)
            if adoption_values
            else _unavailable("No adoption score has been recorded.")
        ),
        "repeat_support_issue_customers": len(repeat_issue_customers),
        "churn_feedback_records": len(churn_feedback),
        "retention_note": (
            "Churn/retention is based on recorded customer status, not a prediction."
            if customers
            else "No customers are recorded; retention is unavailable."
        ),
    }
    return {
        "product_id": workspace.product_id,
        "product_name": workspace.product_snapshot.get("name", ""),
        "generated_at": workspace.updated_at.isoformat(),
        "acquisition": acquisition,
        "sales": sales,
        "support": support,
        "retention": retention,
        "disclaimer": (
            "All figures derive from records in this workspace. Estimates are labelled; "
            "unavailable figures are not inferred, and no revenue, CAC, demand or "
            "satisfaction value is fabricated."
        ),
    }


__all__ = ["analytics_snapshot"]
