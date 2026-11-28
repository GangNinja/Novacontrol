"""Phase 37 — customer acquisition, sales and support service tests."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from novacontrol.customer import (
    CommunicationNotConfigured,
    ConsentRequired,
    CustomerBudgetExceeded,
    CustomerConflict,
    CustomerNotFound,
    CustomerRecordNotFound,
    CustomerService,
    CustomerValidationError,
    ProductNotLaunched,
)


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def read(self, namespace: str) -> dict[str, Any]:
        return copy.deepcopy(self.data.get(namespace, {}))

    def write(self, namespace: str, payload: dict[str, Any]) -> None:
        self.data[namespace] = copy.deepcopy(payload)


def product_detail(
    product_id: str = "prod-1",
    *,
    state: str = "APPROVED_FOR_LAUNCH",
    approved: bool = True,
) -> dict[str, Any]:
    return {
        "summary": {"name": "Invoice assistant", "state": state},
        "launch_approval": {"approved": approved, "by": "operator"},
        "prd": {
            "status": "approved",
            "version": "1.0",
            "vision": "Draft invoice reminders without leaving the desk.",
            "problem_statement": "Small agencies forget to follow up overdue invoices.",
            "target_users": ["Small design agencies"],
            "core_features": ["Draft an invoice reminder"],
            "explicit_exclusions": ["No payment processing"],
            "missing_information": [],
        },
        "architecture": {"status": "approved"},
        "releases": [{"version": "1.0.0", "notes": "Initial release"}],
        "opportunity_snapshot": {"financial": {"price": 29, "currency": "USD"}},
        "quality": {"failing_required": []},
    }


def service(
    *,
    sender=None,
    max_leads: int = 1000,
    reader=product_detail,
) -> tuple[CustomerService, MemoryStore]:
    store = MemoryStore()
    instance = CustomerService(
        store=store,
        products=lambda product_id: reader(product_id) if callable(reader) else reader,
        sender=sender,
        max_leads=max_leads,
    )
    return instance, store


def create_workspace(instance: CustomerService, product_id: str = "prod-1") -> dict[str, Any]:
    return asyncio.run(instance.create_workspace(product_id))


def add_customer(instance: CustomerService, **overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Acme Design",
        "category": "business",
        "contact": {"email": "buyer@acme.example"},
        "consent": {"status": "granted", "channels": ["email"], "source": "web form"},
        "location": "Pune",
    }
    payload.update(overrides)
    return instance.add_customer("prod-1", payload)


def add_lead(instance: CustomerService, **overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Beta Studio",
        "organization": "Beta",
        "source": "website_inquiry",
        "contact": {"email": "owner@beta.example"},
        "consent": {"status": "granted", "channels": ["email"]},
        "qualification_inputs": {
            "product_fit": {"score": 4, "evidence": "recorded discovery call"},
            "problem_relevance": {"score": 4, "evidence": "recorded discovery call"},
        },
    }
    payload.update(overrides)
    return instance.add_lead("prod-1", payload)


def test_workspace_requires_phase36_launch_authorization_and_does_not_invent_claims() -> None:
    instance, _ = service(reader=lambda _: product_detail(state="READY_FOR_RELEASE_REVIEW", approved=False))
    with pytest.raises(ProductNotLaunched):
        create_workspace(instance)
    assert instance.list_workspaces()["total"] == 0

    authorized, _ = service()
    detail = create_workspace(authorized)
    snapshot = detail["product_snapshot"]
    assert snapshot["launch_authorized"] is True
    assert snapshot["version"] == "1.0.0"
    assert snapshot["value_proposition"].startswith("Draft invoice")
    assert snapshot["pricing_assumptions"]["price"] == 29
    assert snapshot["approved_marketing_claims"] == []
    assert "no separately approved marketing-claims" in snapshot["marketing_claims_note"]


def test_segments_icps_and_research_distinguish_evidence_from_assumptions() -> None:
    instance, _ = service()
    create_workspace(instance)
    segment = instance.add_segment(
        "prod-1",
        {"name": "Small agencies", "category": "business", "needs": ["speed"], "assumptions": ["10-50 staff"]},
    )
    assert segment["verification_state"] == "inferred"
    with pytest.raises(CustomerValidationError):
        instance.add_segment("prod-1", {"name": "Verified only", "verified": True})
    icp = instance.add_icp(
        "prod-1",
        {"name": "Owner-led agency", "segment_id": segment["id"], "criteria": {"size": "10-50"}},
    )
    assert icp["segment_id"] == segment["id"]
    with pytest.raises(CustomerValidationError):
        instance.add_research("prod-1", {"title": "Unsourced", "summary": "A claim"})
    research = instance.add_research(
        "prod-1",
        {
            "title": "Public pricing page",
            "summary": "Competitor publishes a per-seat price.",
            "source_url": "https://example.test/pricing",
            "verification_status": "public_research",
            "confidence": 0.6,
        },
    )
    assert research["verified"] is False
    assert research["direct_validation"] is False


def test_customer_isolation_and_lead_qualification_are_explainable() -> None:
    first, _ = service()
    second, _ = service()
    create_workspace(first)
    create_workspace(second, "prod-2")
    customer = add_customer(first)
    assert customer["status"] == "active"
    assert add_customer(first, name="Second")["id"] != customer["id"]
    with pytest.raises(CustomerNotFound):
        second.detail("prod-1")
    lead = add_lead(first)
    result = first.qualify("prod-1", lead["id"])
    assert result["score"] == pytest.approx(0.8)
    assert result["recommended_status"] == "QUALIFIED"
    assert result["purchase_intent_verified"] is False
    assert "Purchase intent" in result["missing_information"]
    assert result["automated_recommendation"] is True
    assert result["reasons"]


def test_lead_lifecycle_requires_qualification_reason_and_uses_convert_action() -> None:
    instance, _ = service()
    create_workspace(instance)
    lead = add_lead(instance)
    with pytest.raises(CustomerValidationError):
        instance.set_lead_status("prod-1", lead["id"], "CONTACT_PENDING_APPROVAL")
    instance.qualify("prod-1", lead["id"])
    with pytest.raises(CustomerValidationError):
        instance.set_lead_status("prod-1", lead["id"], "LOST")
    lost = instance.set_lead_status("prod-1", lead["id"], "LOST", lost_reason="No budget this quarter")
    assert lost["status"] == "LOST"
    with pytest.raises(CustomerValidationError):
        instance.set_lead_status("prod-1", lead["id"], "CONVERTED")

    second = add_lead(instance, name="Gamma")
    instance.qualify("prod-1", second["id"])
    converted = instance.convert_lead("prod-1", second["id"])
    assert converted["lead_id"] == second["id"]
    assert instance.detail("prod-1")["leads"][1]["status"] == "CONVERTED"


def test_deal_transitions_and_realized_revenue_are_distinct_from_pipeline_estimates() -> None:
    instance, _ = service()
    create_workspace(instance)
    customer = add_customer(instance)
    deal = instance.create_deal(
        "prod-1",
        {"customer_id": customer["id"], "name": "Annual plan", "value": 1200, "currency": "USD", "probability": 0.4},
    )
    assert deal["value_is_estimate"] is True
    with pytest.raises(CustomerValidationError):
        instance.move_deal("prod-1", deal["id"], "closed_lost")
    with pytest.raises(CustomerValidationError):
        instance.move_deal("prod-1", deal["id"], "not_a_stage")
    won = instance.move_deal("prod-1", deal["id"], "closed_won", realized_amount=1100)
    assert won["realized_amount"] == 1100
    with pytest.raises(CustomerConflict):
        instance.move_deal("prod-1", deal["id"], "proposal")
    analytics = instance.analytics("prod-1")
    assert analytics["sales"]["realized_revenue"]["available"] is True
    assert analytics["sales"]["realized_revenue"]["value"] == 1100
    assert analytics["sales"]["pipeline_value"]["value"] is None
    assert analytics["sales"]["pipeline_value"]["available"] is False


def test_mixed_currency_totals_are_explicitly_unavailable() -> None:
    instance, _ = service()
    create_workspace(instance)
    first = add_customer(instance)
    second = add_customer(instance, name="Euro Co", contact={"email": "buyer@euro.example"})
    instance.create_deal("prod-1", {"customer_id": first["id"], "value": 100, "currency": "USD"})
    instance.create_deal("prod-1", {"customer_id": second["id"], "value": 200, "currency": "EUR"})
    instance.add_acquisition_cost("prod-1", {"amount": 10, "currency": "USD", "source": "recorded ad spend"})
    instance.add_acquisition_cost("prod-1", {"amount": 20, "currency": "EUR", "source": "recorded event spend"})
    with pytest.raises(CustomerValidationError):
        instance.create_deal("prod-1", {"customer_id": first["id"], "value": float("nan")})
    with pytest.raises(CustomerValidationError):
        instance.add_acquisition_cost("prod-1", {"amount": float("inf"), "currency": "USD", "source": "recorded spend"})
    analytics = instance.analytics("prod-1")
    assert analytics["sales"]["pipeline_value"]["available"] is False
    assert "more than one currency" in analytics["sales"]["pipeline_value"]["reason"]
    assert analytics["sales"]["deal_value"]["available"] is False
    assert analytics["acquisition"]["acquisition_cost"]["available"] is False
    assert "CAC" in analytics["acquisition"]["acquisition_cost"]["reason"]


def test_communication_consent_approval_and_unconfigured_sender_are_honest() -> None:
    instance, _ = service()
    create_workspace(instance)
    customer = add_customer(instance, consent={"status": "unknown"})
    draft = instance.draft_communication(
        "prod-1",
        {"related_type": "customer", "related_id": customer["id"], "channel": "email", "subject": "Hello", "body": "A note"},
    )
    assert draft["consent_ok"] is False
    with pytest.raises(ConsentRequired):
        instance.approve_communication("prod-1", draft["id"], approved=True, actor="op", note="approved")
    with pytest.raises(ConsentRequired):
        instance.send_communication("prod-1", draft["id"])
    record = instance.detail("prod-1")["communications"][0]
    assert record["status"] == "blocked"
    assert "consent" in record["failure"].lower()

    granted = add_customer(instance, name="Granted Co", contact={"email": "granted@example.test"})
    message = instance.draft_communication(
        "prod-1",
        {"related_type": "customer", "related_id": granted["id"], "channel": "email", "subject": "Hello", "body": "A note"},
    )
    instance.approve_communication("prod-1", message["id"], approved=True, actor="operator", note="consent checked")
    with pytest.raises(CommunicationNotConfigured):
        instance.send_communication("prod-1", message["id"])
    record = instance.detail("prod-1")["communications"][-1]
    assert record["status"] == "not_configured"
    assert "nothing was sent" in record["failure"]


def test_consent_withdrawal_is_rechecked_at_send_and_needs_provenance() -> None:
    sender = lambda record: {"sent": True, "message_id": "provider-consent"}  # noqa: E731
    instance, _ = service(sender=sender)
    create_workspace(instance)
    customer = add_customer(instance)
    draft = instance.draft_communication(
        "prod-1",
        {"related_type": "customer", "related_id": customer["id"], "channel": "email", "subject": "Hello", "body": "A note"},
    )
    instance.approve_communication("prod-1", draft["id"], approved=True, actor="operator", note="consent was checked")
    unverified = add_customer(instance, name="Needs Source", consent={"status": "unknown"})
    stale = instance.draft_communication(
        "prod-1",
        {"related_type": "customer", "related_id": unverified["id"], "channel": "email", "subject": "Stale draft", "body": "A note"},
    )
    assert stale["consent_ok"] is False
    with pytest.raises(CustomerValidationError):
        instance.update_consent("prod-1", "customer", unverified["id"], {"status": "granted"})
    instance.update_consent(
        "prod-1", "customer", unverified["id"], {"status": "granted", "source": "signed consent form"}
    )
    instance.set_automation_policy(
        "prod-1", {"enabled": True, "channels": ["email"], "max_recipients": 1, "daily_limit": 5},
        actor="operator", note="Narrow onboarding policy",
    )
    assert instance.send_communication("prod-1", stale["id"])["status"] == "sent"
    updated = instance.update_consent(
        "prod-1",
        "customer",
        customer["id"],
        {"status": "withdrawn", "source": "recorded opt-out"},
        actor="operator",
    )
    assert updated["consent"]["status"] == "withdrawn"
    assert updated["consent"]["withdrawn_at"]
    with pytest.raises(ConsentRequired):
        instance.send_communication("prod-1", draft["id"])
    record = instance.detail("prod-1")["communications"][0]
    assert record["status"] == "blocked"
    assert "withdrawn" in record["failure"].lower()
    assert record["consent_status"] == "withdrawn"

    restored = instance.update_consent(
        "prod-1",
        "customer",
        customer["id"],
        {"status": "granted", "channels": ["email"], "source": "signed consent form"},
        actor="operator",
    )
    assert restored["consent"]["status"] == "granted"
    fresh = instance.draft_communication(
        "prod-1",
        {"related_type": "customer", "related_id": customer["id"], "channel": "email", "subject": "Hello again", "body": "A note"},
    )
    instance.approve_communication("prod-1", fresh["id"], approved=True, actor="operator", note="fresh consent checked")
    assert instance.send_communication("prod-1", fresh["id"])["status"] == "sent"


def test_manual_lead_promotion_and_deal_links_cannot_bypass_qualification() -> None:
    instance, _ = service()
    create_workspace(instance)
    lead = add_lead(
        instance,
        disqualification_reason="Budget freeze was recorded",
        qualification_inputs={
            "product_fit": {"score": 1, "evidence": "recorded mismatch"},
            "problem_relevance": {"score": 1, "evidence": "recorded mismatch"},
        },
    )
    result = instance.qualify("prod-1", lead["id"])
    assert result["recommended_status"] == "UNQUALIFIED"
    assert any("Budget freeze" in reason for reason in result["reasons"])
    with pytest.raises((CustomerValidationError, CustomerConflict)):
        instance.set_lead_status("prod-1", lead["id"], "QUALIFIED")
    with pytest.raises((CustomerValidationError, CustomerConflict)):
        instance.set_lead_status("prod-1", lead["id"], "CONTACT_PENDING_APPROVAL")
    with pytest.raises(CustomerConflict):
        instance.create_deal("prod-1", {"lead_id": lead["id"], "value": 100})

    disqualified = add_lead(
        instance,
        name="High Score Disqualified",
        disqualification_reason="Recorded legal conflict",
        qualification_inputs={
            "product_fit": {"score": 5, "evidence": "strong fit"},
            "problem_relevance": {"score": 5, "evidence": "strong fit"},
        },
    )
    assert instance.qualify("prod-1", disqualified["id"])["recommended_status"] == "UNQUALIFIED"

    qualified = add_lead(instance, name="Qualified Co")
    instance.qualify("prod-1", qualified["id"])
    customer = instance.convert_lead("prod-1", qualified["id"])
    other = add_customer(instance, name="Other Co")
    with pytest.raises(CustomerConflict):
        instance.create_deal(
            "prod-1",
            {"lead_id": qualified["id"], "customer_id": other["id"], "value": 100},
        )
    deal = instance.create_deal("prod-1", {"lead_id": qualified["id"], "value": 100})
    assert deal["customer_id"] == customer["id"]
    with pytest.raises(CustomerValidationError):
        instance.move_deal("prod-1", deal["id"], "discovery", realized_amount=10)


def test_configured_sender_and_narrow_policy_can_send_without_inventing_delivery() -> None:
    sender = lambda record: {"sent": True, "message_id": "provider-1"}  # noqa: E731
    instance, _ = service(sender=sender)
    create_workspace(instance)
    customer = add_customer(instance)
    policy = instance.set_automation_policy(
        "prod-1",
        {"enabled": True, "channels": ["email"], "max_recipients": 1, "daily_limit": 5},
        actor="operator",
        note="Narrow onboarding follow-up policy",
    )
    assert policy["enabled"] is True
    draft = instance.draft_communication(
        "prod-1",
        {"related_type": "customer", "related_id": customer["id"], "channel": "email", "subject": "Welcome", "body": "Hello"},
    )
    sent = instance.send_communication("prod-1", draft["id"])
    assert sent["status"] == "sent"
    assert sent["message_id"] == "provider-1"
    assert sent["approved_by"] == ""
    assert sent["automation_policy"] is True


def test_sales_assistance_is_traceable_and_never_authorizes_terms() -> None:
    instance, _ = service()
    create_workspace(instance)
    segment = instance.add_segment("prod-1", {"name": "Agencies", "needs": ["reminder drafting"]})
    lead = add_lead(instance, segment_id=segment["id"])
    instance.qualify("prod-1", lead["id"])
    instance.add_research(
        "prod-1",
        {
            "title": "Competitor pricing",
            "summary": "A public pricing page exists.",
            "source_url": "https://example.test/pricing",
            "segment_id": segment["id"],
        },
    )
    customer = instance.convert_lead("prod-1", lead["id"])
    instance.add_interaction("prod-1", {"related_type": "customer", "related_id": customer["id"], "summary": "Discovery call notes recorded."})
    deal = instance.create_deal(
        "prod-1",
        {
            "customer_id": customer["id"],
            "value": 1000,
            "objections": ["Needs a reminder template"],
            "competitor_alternatives": ["Spreadsheet reminder"],
        },
    )
    instance.add_interaction("prod-1", {"related_type": "deal", "related_id": deal["id"], "summary": "Proposal call notes recorded."})
    instance.add_proposal("prod-1", deal["id"], {"title": "Initial proposal", "summary": "Draft only.", "source_reference": "recorded requirements"})
    brief = instance.assist_sales("prod-1", deal_id=deal["id"])
    assert brief["proposal_draft"]["draft_only"] is True
    assert brief["proposal_draft"]["authorized_discount"] is False
    assert brief["proposal_draft"]["authorized_contract_terms"] is False
    assert brief["sources"]
    assert any(row["type"] == "research" for row in brief["sources"])
    assert sum(1 for row in brief["sources"] if row["type"] == "interaction") >= 2
    assert any("Proposal call" in row for row in brief["conversation_summary"])
    assert brief["competitor_comparison"]["recorded_alternatives"] == ["Spreadsheet reminder"]
    assert any("not a claim" in row for row in [brief["product_fit"]["note"]])
    with pytest.raises(CustomerValidationError):
        instance.assist_sales("prod-1")
    with pytest.raises(CustomerRecordNotFound):
        instance.assist_sales("prod-1", lead_id="missing")


def test_support_grounding_sensitive_escalation_and_ticket_lifecycle() -> None:
    instance, _ = service()
    create_workspace(instance)
    customer = add_customer(instance)
    instance.add_knowledge(
        "prod-1",
        {
            "title": "Configure reminders",
            "body": "Open Settings and choose reminder timing.",
            "source": "release 1.0.0 documentation",
            "approved": True,
            "verified": True,
            "steps": ["Open Settings", "Choose reminder timing"],
        },
    )
    ticket = instance.create_ticket(
        "prod-1",
        {"customer_id": customer["id"], "subject": "Configure reminders", "description": "How do I configure reminders?", "category": "product_usage"},
    )
    result = instance.assist_ticket("prod-1", ticket["id"])
    assert result["grounded"] is True
    assert result["actions_performed"] == []
    assert "Open Settings" in result["troubleshooting_steps"]
    assert result["escalation_required"] is False

    with pytest.raises(CustomerValidationError):
        instance.transition_ticket("prod-1", ticket["id"], "RESOLVED")
    resolved = instance.transition_ticket("prod-1", ticket["id"], "RESOLVED", resolution="Sent the approved steps")
    assert resolved["status"] == "RESOLVED"
    reopened = instance.transition_ticket("prod-1", ticket["id"], "OPEN", reason="Customer needs more help")
    assert reopened["reopen_count"] == 1

    resolved_again = instance.transition_ticket("prod-1", ticket["id"], "RESOLVED", resolution="Sent the approved steps again")
    assert resolved_again["status"] == "RESOLVED"
    instance.add_ticket_message("prod-1", ticket["id"], {"body": "I still need help with the reminder time."})
    after_message = instance.ticket_detail("prod-1", ticket["id"])
    assert after_message["status"] == "OPEN"
    assert after_message["reopen_count"] == 2
    assert after_message["resolved_at"] == ""

    classified = instance.create_ticket(
        "prod-1",
        {"customer_id": customer["id"], "subject": "Refund my invoice", "description": "Please refund and change billing."},
    )
    assert classified["category"] == "billing_inquiry"

    billing = instance.create_ticket(
        "prod-1",
        {"customer_id": customer["id"], "subject": "Refund my invoice", "description": "Please refund and change billing.", "category": "billing_inquiry", "priority": "high"},
    )
    sensitive = instance.assist_ticket("prod-1", billing["id"])
    assert sensitive["human_review_required"] is True
    assert sensitive["escalation_required"] is True

    urgent = instance.create_ticket(
        "prod-1",
        {"customer_id": customer["id"], "subject": "Configure reminders", "description": "How do I configure reminders?", "category": "product_usage", "priority": "high"},
    )
    urgent_result = instance.assist_ticket("prod-1", urgent["id"])
    assert urgent_result["escalation_required"] is True
    assert urgent_result["human_review_required"] is True
    assert "human must review" in sensitive["draft_response"].lower()
    assert sensitive["actions_performed"] == []

    instance.add_knowledge(
        "prod-1",
        {
            "title": "Scheduling window",
            "body": "The scheduling window is described in the release notes.",
            "source": "release 1.0.0 documentation",
            "approved": True,
            "verified": True,
        },
    )
    no_steps = instance.create_ticket(
        "prod-1",
        {"customer_id": customer["id"], "subject": "Scheduling window", "description": "What is the scheduling window?", "category": "product_usage"},
    )
    no_steps_result = instance.assist_ticket("prod-1", no_steps["id"])
    assert no_steps_result["grounded"] is True
    assert no_steps_result["escalation_required"] is True
    assert any("no recorded troubleshooting steps" in reason for reason in no_steps_result["escalation_reasons"])

    ungrounded = instance.create_ticket(
        "prod-1",
        {"customer_id": customer["id"], "subject": "Unusual request", "description": "Something no documentation covers."},
    )
    fallback = instance.assist_ticket("prod-1", ungrounded["id"])
    assert fallback["grounded"] is False
    assert fallback["escalation_required"] is True
    assert "could not find approved" in fallback["draft_response"].lower()


def test_feedback_onboarding_success_churn_and_unavailable_metrics() -> None:
    instance, _ = service()
    create_workspace(instance)
    customer = add_customer(instance)
    onboarding = instance.start_onboarding(
        "prod-1",
        {"customer_id": customer["id"], "items": [{"id": "setup", "title": "Set up account"}], "adoption_score": 0.2},
    )
    with pytest.raises(CustomerValidationError):
        instance.update_onboarding("prod-1", onboarding["id"], {"item_id": "setup", "status": "complete"})
    completed = instance.update_onboarding(
        "prod-1",
        onboarding["id"],
        {"item_id": "setup", "status": "complete", "evidence": ["operator confirmed setup"], "adoption_score": 0.8},
    )
    assert completed["status"] == "complete"
    success = instance.refresh_success("prod-1", customer["id"])
    assert success["risk"] == "low"
    assert "not a prediction" in success["certainty_note"]

    instance.add_feedback(
        "prod-1",
        {"customer_id": customer["id"], "kind": "satisfaction", "summary": "Good first week", "rating": 4, "source": "recorded call", "verified": True},
    )
    analytics = instance.analytics("prod-1")
    assert analytics["support"]["customer_satisfaction"]["value"] == 4.0
    assert analytics["acquisition"]["acquisition_cost"]["available"] is False
    assert analytics["retention"]["retention_rate"] == 1.0

    churned = instance.mark_customer_churned("prod-1", customer["id"], reason="Budget was withdrawn")
    assert churned["status"] == "churned"
    after = instance.analytics("prod-1")
    assert after["retention"]["churn_rate"] == 1.0
    assert after["retention"]["retention_rate"] == 0.0
    assert after["retention"]["churn_feedback_records"] == 1


def test_customer_erasure_requires_approval_and_removes_related_records() -> None:
    instance, _ = service()
    create_workspace(instance)
    customer = add_customer(instance)
    instance.create_ticket("prod-1", {"customer_id": customer["id"], "subject": "Help", "description": "A request"})
    with pytest.raises(CustomerValidationError):
        instance.erase_customer("prod-1", customer["id"], approved=False, reason="requested")
    deal = instance.create_deal("prod-1", {"customer_id": customer["id"], "value": 100})
    instance.add_interaction("prod-1", {"related_type": "deal", "related_id": deal["id"], "summary": "Recorded deal conversation"})
    instance.assist_sales("prod-1", deal_id=deal["id"])
    result = instance.erase_customer("prod-1", customer["id"], approved=True, reason="verified erasure request", actor="operator")
    assert result["erased"] is True
    detail = instance.detail("prod-1")
    assert detail["customers"] == []
    assert detail["tickets"] == []
    assert detail["deals"] == []
    assert detail["interactions"] == []
    assert detail["sales_assists"] == []
    assert any(event["type"] == "customer_erased" for event in detail["events"])
    assert not any(row.get("customer_id") == customer["id"] for row in detail["success"])


def test_events_audit_and_memory_are_recorded_through_existing_seams() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    audits: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    instance = CustomerService(
        store=MemoryStore(),
        products=product_detail,
        publish=lambda event_type, payload: events.append((event_type, dict(payload))),
        audit=lambda **kwargs: audits.append(dict(kwargs)),
        memory=lambda **kwargs: memories.append(dict(kwargs)),
    )
    create_workspace(instance)
    customer = add_customer(instance)
    instance.update_consent(
        "prod-1", "customer", customer["id"], {"status": "withdrawn", "note": "recorded opt-out"}
    )
    assert [event for event, _ in events][:2] == ["customer.workspace_created", "customer.customer_created"]
    assert any(row["outcome"] == "customer_created" for row in audits)
    assert any(row["outcome"] == "consent_updated" for row in audits)
    assert memories and memories[-1]["key"] == "customer:prod-1:latest"


def test_refresh_records_revoked_launch_authorization_and_blocks_mutations() -> None:
    authorized = {"value": True}

    def reader(product_id: str) -> dict[str, Any]:
        if authorized["value"]:
            return product_detail(product_id)
        return product_detail(product_id, state="READY_FOR_RELEASE_REVIEW", approved=False)

    instance, _ = service(reader=reader)
    create_workspace(instance)
    authorized["value"] = False
    with pytest.raises(ProductNotLaunched):
        asyncio.run(instance.refresh_product("prod-1", actor="operator"))
    detail = instance.detail("prod-1")
    assert detail["product_snapshot"]["launch_authorized"] is False
    assert any(event["type"] == "product_authorization_revoked" for event in detail["events"])
    with pytest.raises(ProductNotLaunched):
        add_customer(instance)


def test_persistence_budgets_and_product_refresh() -> None:
    instance, store = service(max_leads=1)
    create_workspace(instance)
    add_lead(instance)
    with pytest.raises(CustomerBudgetExceeded):
        add_lead(instance, name="Too many")
    reloaded = CustomerService(store=store, products=product_detail)
    listing = reloaded.list_workspaces()
    assert listing["total"] == 1
    assert len(reloaded.detail("prod-1")["leads"]) == 1
    refreshed = asyncio.run(reloaded.refresh_product("prod-1", actor="operator"))
    assert refreshed["product_snapshot"]["launch_authorized"] is True
    assert any(event["type"] == "product_snapshot_refreshed" for event in refreshed["events"])
