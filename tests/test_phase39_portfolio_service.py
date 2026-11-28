"""Phase 39 portfolio service integration tests."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from novacontrol.agents.models import AgentRole, AgentTask, AgentTaskStatus
from novacontrol.core.events import Event, EventBus
from novacontrol.finops import FinOpsService
from novacontrol.portfolio import (
    ApprovalRequired,
    LifecycleGateError,
    PortfolioAuthorizationError,
    PortfolioConflict,
    PortfolioService,
    PortfolioValidationError,
)
from novacontrol.portfolio.agent import build_portfolio_agent
from novacontrol.portfolio.runtime import PortfolioModule

FIXED = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.writes = 0

    def read(self, namespace: str) -> dict:
        return copy.deepcopy(self.data.get(namespace, {}))

    def write(self, namespace: str, payload: dict) -> None:
        self.writes += 1
        self.data[namespace] = copy.deepcopy(payload)


def make_finance() -> FinOpsService:
    return FinOpsService(store=MemoryStore(), clock=lambda: FIXED, actor="operator")


def create_venture(
    finance: FinOpsService,
    name: str,
    currency: str = "USD",
    *,
    linked: bool = True,
) -> dict:
    payload = {
        "name": name,
        "business_model": "saas",
        "reporting_currency": currency,
    }
    if linked:
        payload.update({"linked_products": ["prod-1"], "customer_refs": ["cust-1"]})
    row = finance.create_venture(payload, actor="owner")
    finance.add_revenue(
        row["id"],
        {
            "gross_amount": "1100.00",
            "tax": "110.00",
            "stage": "recognized",
            "transaction_date": "2026-11-01",
            "customer_ref": "cust-1",
            "product_ref": "prod-1",
            "idempotency_key": f"{row['id']}-rev",
        },
        actor="owner",
    )
    return row


PRODUCT = {
    "id": "prod-1",
    "opportunity_id": "opp-1",
    "state": "APPROVED_FOR_LAUNCH",
    "validation_state": "validated",
    "summary": {
        "id": "prod-1",
        "name": "North Star",
        "state": "APPROVED_FOR_LAUNCH",
        "open_defects": 0,
        "readiness": "APPROVED_FOR_LAUNCH",
    },
    "readiness": {
        "status": "APPROVED_FOR_LAUNCH",
        "release_approved": True,
        "launch_approval": {"approved": True, "actor": "owner"},
    },
    "progress": {"tasks": 4, "completed": 3, "ratio": 0.75},
    "tests": [{"id": "test-1", "status": "passed"}],
    "launch_approval": {"approved": True},
}
OPPORTUNITY = {
    "id": "opp-1",
    "title": "North Star",
    "stage": "validated",
    "score": {"weighted_score": 78.0, "coverage": 0.8},
}
CUSTOMER = {
    "id": "prod-1",
    "launch_authorized": True,
    "product_snapshot": {"id": "prod-1", "name": "North Star", "launch_authorized": True},
    "summary": {
        "id": "prod-1",
        "product_id": "prod-1",
        "customers": 3,
        "open_tickets": 1,
    },
    "analytics": {
        "acquisition": {"qualified_leads": 5, "qualification_rate": 0.5},
        "sales": {"conversion_rate": 0.22},
        "retention": {"churn_rate": 0.03},
        "support": {"open_tickets": 1},
    },
}


def make_portfolio(
    finance: FinOpsService,
    *,
    store: MemoryStore | None = None,
    products: dict | None = None,
    opportunities: dict | None = None,
    customers: dict | None = None,
) -> PortfolioService:
    product_map = {"prod-1": copy.deepcopy(PRODUCT)}
    product_map.update(products or {})
    opp_map = {"opp-1": copy.deepcopy(OPPORTUNITY)}
    opp_map.update(opportunities or {})
    customer_map = {"prod-1": copy.deepcopy(CUSTOMER)}
    customer_map.update(customers or {})
    return PortfolioService(
        store=store or MemoryStore(),
        finance=finance,
        products=lambda key: product_map.get(key, {}),
        opportunities=lambda key: opp_map.get(key, {}),
        customers=lambda key: customer_map.get(key, {}),
        actor="operator",
        clock=lambda: FIXED,
    )


def portfolio_and_venture(
    finance: FinOpsService | None = None,
) -> tuple[PortfolioService, dict, dict]:
    finance = finance or make_finance()
    venture = create_venture(finance, "North Star Studio")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio(
        {
            "name": "Holding One",
            "base_currency": "USD",
            "objectives": ["durable cash generation"],
            "risk_preferences": {"min_cash_runway_months": 3, "max_budget_overrun": 0.1},
        },
        actor="owner",
    )
    ref = service.add_venture(
        portfolio["id"],
        {
            "venture_id": venture["id"],
            "product_id": "prod-1",
            "opportunity_id": "opp-1",
            "lifecycle_status": "OPERATING",
            "approved": True,
            "reason": "validated product and actual operations",
        },
        actor="owner",
    )
    return service, portfolio, ref


def test_multiple_portfolios_and_duplicate_venture_binding_are_isolated() -> None:
    finance = make_finance()
    first = create_venture(finance, "First")
    second = create_venture(finance, "Second")
    service = make_portfolio(finance)
    one = service.create_portfolio({"name": "One", "base_currency": "USD"}, actor="owner")
    two = service.create_portfolio({"name": "Two", "base_currency": "USD"}, actor="other")
    assert service.list_portfolios(actor="owner")["total"] == 1
    assert service.list_portfolios(actor="other")["total"] == 1
    service.add_venture(one["id"], {"venture_id": first["id"]}, actor="owner")
    with pytest.raises(PortfolioConflict):
        service.add_venture(one["id"], {"venture_id": first["id"]}, actor="owner")
    with pytest.raises(PortfolioConflict):
        service.add_venture(two["id"], {"venture_id": first["id"]}, actor="other")
    service.add_venture(two["id"], {"venture_id": second["id"]}, actor="other")
    assert service.detail(one["id"], actor="owner")["counts"]["ventures"] == 1
    assert service.detail(two["id"], actor="other")["counts"]["ventures"] == 1


def test_venture_access_restriction_blocks_cross_venture_data() -> None:
    service, portfolio, ref = portfolio_and_venture()
    service.add_member(portfolio["id"], {"actor": "viewer", "role": "viewer"}, actor="owner")
    service.add_member(portfolio["id"], {"actor": "outsider", "role": "viewer"}, actor="owner")
    service._get(portfolio["id"])["ventures"][0]["visibility"] = "restricted"
    service._get(portfolio["id"])["ventures"][0]["allowed_members"] = ["viewer"]
    service._persist()
    assert service.detail(portfolio["id"], actor="viewer")["counts"]["visible_ventures"] == 1
    assert service.detail(portfolio["id"], actor="outsider")["counts"]["visible_ventures"] == 0
    with pytest.raises(PortfolioAuthorizationError):
        service.venture_detail(portfolio["id"], ref["id"], actor="outsider")
    analytics = service.portfolio_analytics(portfolio["id"], actor="outsider")
    assert analytics["venture_count"] == 0
    assert analytics["data_completeness"]["hidden"] == 1


def test_lifecycle_gates_require_real_phase_evidence_and_approval() -> None:
    finance = make_finance()
    venture = create_venture(finance, "Gate Test", linked=False)
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Gates"}, actor="owner")
    with pytest.raises(LifecycleGateError):
        service.add_venture(
            portfolio["id"],
            {
                "venture_id": venture["id"],
                "lifecycle_status": "OPERATING",
                "approved": True,
                "reason": "shortcut",
            },
            actor="owner",
        )
    ref = service.add_venture(
        portfolio["id"],
        {"venture_id": venture["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    with pytest.raises(ApprovalRequired):
        service.transition_lifecycle(
            portfolio["id"], ref["id"], "OPERATING", reason="not approved", actor="owner"
        )
    changed = service.transition_lifecycle(
        portfolio["id"],
        ref["id"],
        "OPERATING",
        reason="launch and customer gates verified",
        approved=True,
        actor="owner",
    )
    assert changed["lifecycle_status"] == "OPERATING"
    history = service.venture_detail(portfolio["id"], ref["id"], actor="owner")["lifecycle"]
    assert history["history"][-1]["evidence"]["launch_authorized"] is True
    assert history["history"][-1]["approved"] is True
    with pytest.raises(ApprovalRequired):
        service.transition_lifecycle(
            portfolio["id"], ref["id"], "PAUSED", reason="needs approval", actor="owner"
        )


def test_unvalidated_opportunity_cannot_be_operating() -> None:
    finance = make_finance()
    venture = create_venture(finance, "Unvalidated")
    service = make_portfolio(
        finance,
        opportunities={"opp-1": {"id": "opp-1", "stage": "discovered"}},
        products={
            "prod-1": {"id": "prod-1", "state": "DEVELOPMENT", "summary": {"state": "DEVELOPMENT"}}
        },
        customers={"prod-1": {}},
    )
    portfolio = service.create_portfolio({"name": "Gate"}, actor="owner")
    ref = service.add_venture(
        portfolio["id"],
        {"venture_id": venture["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    with pytest.raises(LifecycleGateError):
        service.transition_lifecycle(
            portfolio["id"],
            ref["id"],
            "PRODUCT_DEVELOPMENT",
            reason="not validated",
            approved=True,
            actor="owner",
        )


def test_consolidated_reporting_is_currency_safe_and_does_not_double_count() -> None:
    finance = make_finance()
    usd = create_venture(finance, "USD Venture", "USD")
    eur = create_venture(finance, "EUR Venture", "EUR")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio(
        {"name": "Consolidated", "base_currency": "USD"}, actor="owner"
    )
    service.add_venture(portfolio["id"], {"venture_id": usd["id"]}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": eur["id"]}, actor="owner")
    analytics = service.portfolio_analytics(portfolio["id"], actor="owner")
    assert analytics["totals"]["available"] is False
    assert analytics["by_currency"]["USD"]["revenue"] == 1100.0
    assert analytics["by_currency"]["EUR"]["revenue"] == 1100.0
    service.add_fx_rate(
        portfolio["id"],
        {"currency": "EUR", "rate": 1.2, "as_of": "2026-11-01", "source": "test fixture"},
        actor="owner",
    )
    analytics = service.portfolio_analytics(portfolio["id"], actor="owner")
    assert analytics["totals"]["available"] is True
    assert analytics["totals"]["revenue"]["value"] == 2420.0
    assert analytics["data_completeness"]["included"] == 2
    assert analytics["inter_venture"]["policy"] == "separate_legal_entities_no_elimination"


def test_missing_fx_rate_excludes_venture_and_period_comparison_uses_prior_period() -> None:
    finance = make_finance()
    first = create_venture(finance, "Repeat")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Periods", "base_currency": "USD"}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": first["id"]}, actor="owner")
    analytics = service.portfolio_analytics(
        portfolio["id"],
        actor="owner",
        start=datetime(2026, 11, 1).date(),
        end=datetime(2026, 11, 15).date(),
    )
    assert analytics["totals"]["revenue"]["value"] == 1100.0
    growth = analytics["ventures"][0]["revenue_growth"]
    assert growth is None
    assert analytics["ventures"][0]["revenue_growth"] is None


def test_ranking_penalizes_missing_coverage_and_supports_policy_changes() -> None:
    finance = make_finance()
    first = create_venture(finance, "Complete")
    second = create_venture(finance, "Sparse")
    service = make_portfolio(
        finance,
        products={"prod-1": PRODUCT, "prod-2": {"id": "prod-2", "state": "", "summary": {}}},
        opportunities={"opp-1": OPPORTUNITY, "opp-2": {"id": "opp-2", "stage": ""}},
        customers={"prod-1": CUSTOMER, "prod-2": {}},
    )
    portfolio = service.create_portfolio({"name": "Ranking"}, actor="owner")
    service.add_venture(
        portfolio["id"],
        {"venture_id": first["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    service.add_venture(
        portfolio["id"],
        {"venture_id": second["id"], "product_id": "prod-2", "opportunity_id": "opp-2"},
        actor="owner",
    )
    result = service.evaluate(portfolio["id"], {"policy": "balanced"}, actor="owner")
    by_id = {row["venture_id"]: row for row in result["evaluations"]}
    assert by_id[first["id"]]["eligible_for_ranking"] is True
    assert by_id[second["id"]]["eligible_for_ranking"] is False
    assert by_id[second["id"]]["score"] is not None
    ranking = service.ranking(portfolio["id"], actor="owner")
    assert ranking["ranking"][0]["venture_id"] == first["id"]
    growth = service.evaluate(portfolio["id"], {"policy": "growth"}, actor="owner")
    assert growth["policy"] == "growth"


def test_risk_assessment_detects_concentration_and_correlated_risk() -> None:
    finance = make_finance()
    first = create_venture(finance, "Risky One")
    second = create_venture(finance, "Risky Two")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Risk"}, actor="owner")
    service.add_venture(
        portfolio["id"],
        {"venture_id": first["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    service.add_venture(
        portfolio["id"],
        {"venture_id": second["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    result = service.assess_risks(portfolio["id"], actor="owner")
    categories = {row["category"] for row in result["risks"]}
    assert "revenue_concentration" in categories
    correlated = result["correlated_risks"]
    assert correlated
    assert correlated[0]["scope"] == "portfolio"
    assert correlated[0]["correlation_basis"].startswith("The same recorded risk category")


def test_manual_risk_requires_evidence_and_resolution() -> None:
    service, portfolio, ref = portfolio_and_venture()
    with pytest.raises(PortfolioValidationError):
        service.record_risk(
            portfolio["id"],
            {
                "category": "vendor_dependency",
                "severity": "high",
                "observed": "one vendor",
                "mitigation": "contract",
            },
            actor="owner",
        )
    risk = service.record_risk(
        portfolio["id"],
        {
            "category": "vendor_dependency",
            "severity": "high",
            "observed": "one vendor supplies the main runtime",
            "evidence": ["The vendor is the only recorded provider."],
            "mitigation": "Document an exit plan.",
            "venture_id": ref["id"],
        },
        actor="owner",
    )
    assert service.list_risks(portfolio["id"], actor="owner")["risks"][0]["id"] == risk["id"]
    resolved = service.resolve_risk(
        portfolio["id"], risk["id"], {"note": "Exit plan accepted."}, actor="owner"
    )
    assert resolved["status"] == "resolved"


def test_secret_and_customer_contact_fields_are_rejected() -> None:
    finance = make_finance()
    service, portfolio, ref = portfolio_and_venture(finance)
    with pytest.raises(PortfolioValidationError):
        service.create_portfolio({"name": "Unsafe", "api_key": "not-a-real-key"}, actor="owner")
    with pytest.raises(PortfolioValidationError):
        service.record_risk(
            portfolio["id"],
            {
                "category": "financial",
                "title": "Unsafe contact",
                "description": "A contact must be a reference, not copied data.",
                "severity": "high",
                "likelihood": "high",
                "impact": "high",
                "evidence": ["A source record exists."],
                "mitigation": "Use a customer reference.",
                "venture_id": ref["id"],
                "contact_email": "buyer@example.test",
            },
            actor="owner",
        )


def test_stale_data_suppresses_alerts_and_duplicate_alerts_are_updated() -> None:
    finance = make_finance()
    venture = create_venture(finance, "Alert Test")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Alerts"}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": venture["id"]}, actor="owner")
    service._clock = lambda: FIXED + timedelta(days=2)
    stale = service.refresh_alerts(portfolio["id"], actor="owner")
    assert stale["suppressed_stale_ventures"] == 1
    assert stale["open"] == 0
    service._clock = lambda: FIXED
    service._get(portfolio["id"])["monitoring"]["stale_after_hours"] = 100000
    first = service.refresh_alerts(portfolio["id"], actor="owner")
    assert first["open"] >= 1
    open_before = first["open"]
    occurrences_before = max(
        int(row.get("occurrences") or 0)
        for row in service._get(portfolio["id"])["alerts"]
        if row.get("status") == "open"
    )
    second = service.refresh_alerts(portfolio["id"], actor="owner")
    assert second["open"] == open_before
    assert second["updated"] >= 1
    assert (
        max(
            int(row.get("occurrences") or 0)
            for row in service._get(portfolio["id"])["alerts"]
            if row.get("status") == "open"
        )
        == occurrences_before + 1
    )


def test_runtime_monitor_refreshes_alerts_without_agent_mutation_rights() -> None:
    finance = make_finance()
    service, portfolio, _ref = portfolio_and_venture(finance)
    assert service._role(portfolio, "operator") == "viewer"

    async def run() -> list[Event]:
        bus = EventBus()
        module = PortfolioModule(service)
        seen: list[Event] = []

        async def collect(event: Event) -> None:
            seen.append(event)

        await bus.subscribe("portfolio.monitoring_completed", collect)
        await bus.subscribe("portfolio.monitor_failed", collect)
        await module.start(bus)
        await bus.publish(
            Event(
                type="portfolio.monitor_requested",
                payload={"portfolio_id": portfolio["id"]},
                source="test",
            )
        )
        await module.stop()
        return seen

    events = asyncio.run(run())
    types = {event.type for event in events}
    assert "portfolio.monitoring_completed" in types
    assert "portfolio.monitor_failed" not in types


def test_allocation_and_scenario_are_recommendations_only() -> None:
    finance = make_finance()
    venture = create_venture(finance, "Allocation Test")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Allocation"}, actor="owner")
    service.add_venture(
        portfolio["id"],
        {"venture_id": venture["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    batch = service.propose_allocations(portfolio["id"], actor="owner")
    assert batch["status"] == "proposed"
    assert batch["items"][0]["approval_required"] is True
    assert batch["items"][0]["applied"] is False
    before = finance.detail(venture["id"], actor="owner")["venture"]["budgets"]
    decided = service.decide_allocation(
        portfolio["id"], batch["id"], {"approved": True, "note": "reviewed"}, actor="owner"
    )
    after = finance.detail(venture["id"], actor="owner")["venture"]["budgets"]
    assert decided["status"] == "approved"
    assert decided["applied"] is False
    assert before == after
    scenario = service.simulate_scenario(
        portfolio["id"],
        {
            "type": "revenue_shortfall",
            "venture_id": venture["id"],
            "assumptions": {"shortfall_pct": 20},
            "horizon_months": 12,
        },
        actor="owner",
    )
    assert scenario["kind"] == "simulated"
    assert scenario["projected"]["revenue"] < scenario["baseline"]["period_revenue"]
    assert scenario["applied"] is False


def test_strategic_recommendation_cannot_become_a_decision_without_approval() -> None:
    service, portfolio, ref = portfolio_and_venture()
    recs = service.generate_recommendations(portfolio["id"], actor="owner")
    assert recs["recommendations"]
    rec = recs["recommendations"][0]
    assert rec["applied"] is False
    decision = service.propose_decision(
        portfolio["id"],
        {
            "kind": "continue",
            "title": "Continue operating",
            "venture_id": ref["id"],
            "evidence": ["Posted revenue exists."],
        },
        actor="owner",
    )
    assert decision["status"] == "proposed"
    with pytest.raises(PortfolioAuthorizationError):
        service.decide(
            portfolio["id"], decision["id"], {"approved": True, "note": "yes"}, actor="operator"
        )
    approved = service.decide(
        portfolio["id"], decision["id"], {"approved": True, "note": "approved"}, actor="owner"
    )
    assert approved["status"] == "approved"
    assert approved["applied"] is False
    assert "nothing was executed" in approved["application_note"]


def test_shared_resource_conflict_and_dependency_cycle_are_detected() -> None:
    finance = make_finance()
    first = create_venture(finance, "A")
    second = create_venture(finance, "B")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Coordination"}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": first["id"]}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": second["id"]}, actor="owner")
    resource = service.add_resource(
        portfolio["id"], {"name": "GPU hours", "capacity": 100, "unit": "hour"}, actor="owner"
    )
    service.allocate_resource(
        portfolio["id"], resource["id"], {"venture_id": first["id"], "quantity": 80}, actor="owner"
    )
    with pytest.raises(PortfolioConflict):
        service.allocate_resource(
            portfolio["id"],
            resource["id"],
            {"venture_id": second["id"], "quantity": 30},
            actor="owner",
        )
    service.add_dependency(
        portfolio["id"],
        {
            "from_venture_id": first["id"],
            "to_venture_id": second["id"],
            "description": "shared API",
        },
        actor="owner",
    )
    service.add_dependency(
        portfolio["id"],
        {
            "from_venture_id": second["id"],
            "to_venture_id": first["id"],
            "description": "shared data",
        },
        actor="owner",
    )
    board = service.coordination(portfolio["id"], actor="owner")
    assert board["dependency_cycles"]
    assert board["resources"]["resources"][0]["utilization"] == 0.8


def test_knowledge_provenance_and_confidential_promotion() -> None:
    service, portfolio, ref = portfolio_and_venture()
    row = service.add_knowledge(
        portfolio["id"],
        {
            "kind": "historical_observation",
            "title": "Onboarding friction",
            "body": "Two customers needed manual onboarding.",
            "visibility": "venture",
            "venture_id": ref["id"],
            "evidence": ["Support ticket record."],
        },
        actor="owner",
    )
    assert (
        service.list_knowledge(portfolio["id"], actor="owner", venture_id=ref["id"])["knowledge"][
            0
        ]["kind"]
        == "historical_observation"
    )
    with pytest.raises(PortfolioValidationError):
        service.add_knowledge(
            portfolio["id"],
            {"kind": "verified_fact", "title": "No source", "body": "x", "evidence": []},
            actor="owner",
        )
    confidential = service.add_knowledge(
        portfolio["id"],
        {
            "kind": "verified_fact",
            "title": "Customer name",
            "body": "A confidential customer detail.",
            "classification": "confidential_customer",
            "evidence": ["Restricted source."],
        },
        actor="owner",
    )
    with pytest.raises(PortfolioAuthorizationError):
        service.promote_knowledge(
            portfolio["id"], confidential["id"], {"note": "share"}, actor="owner"
        )
    promoted = service.promote_knowledge(
        portfolio["id"],
        confidential["id"],
        {"note": "share with portfolio", "authorized_for_cross_venture": True},
        actor="owner",
    )
    assert promoted["promoted"] is True
    assert row["id"] != confidential["id"]


def test_report_has_twelve_sections_and_is_immutable_after_source_change() -> None:
    finance = make_finance()
    service, portfolio, ref = portfolio_and_venture(finance)
    report = service.generate_report(portfolio["id"], {"period": "monthly"}, actor="owner")
    assert [section["number"] for section in report["sections"]] == list(range(1, 13))
    assert report["source_timestamps"]
    assert report["snapshot_hash"]
    before = service.get_report(portfolio["id"], report["id"], actor="owner")
    finance.add_revenue(
        ref["id"],
        {
            "gross_amount": "50.00",
            "stage": "recognized",
            "transaction_date": "2026-11-02",
            "idempotency_key": "post-report",
        },
        actor="owner",
    )
    after = service.get_report(portfolio["id"], report["id"], actor="owner")
    assert before["snapshot_hash"] == after["snapshot_hash"]
    assert (
        before["analytics"]["totals"]["revenue"]["value"]
        == after["analytics"]["totals"]["revenue"]["value"]
    )


def test_persistence_restores_portfolios_and_lifecycle_history() -> None:
    finance = make_finance()
    store = MemoryStore()
    service, portfolio, ref = portfolio_and_venture(finance)
    service._store = store
    service._persist()
    restored = make_portfolio(finance, store=store)
    row = restored.detail(portfolio["id"], actor="owner")
    assert row["counts"]["ventures"] == 1
    assert row["venture_summary"][0]["drill_down"].endswith(ref["id"])


def test_agent_is_advisory_and_read_only() -> None:
    service, portfolio, _ref = portfolio_and_venture()
    agent = build_portfolio_agent(service)
    response = asyncio.run(
        agent.handle_task(
            AgentTask(goal="review portfolio", metadata={"portfolio_id": portfolio["id"]})
        )
    )
    assert response.status == AgentTaskStatus.COMPLETED
    assert response.role == AgentRole.BUSINESS
    assert response.metadata["advisory_only"] is True
    assert response.metadata["external_action_executed"] is False


def test_full_lifecycle_progression_archives_terminally_with_history() -> None:
    finance = make_finance()
    venture = create_venture(finance, "Lifecycle Journey")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Journey"}, actor="owner")
    ref = service.add_venture(
        portfolio["id"],
        {"venture_id": venture["id"], "product_id": "prod-1", "opportunity_id": "opp-1"},
        actor="owner",
    )
    assert ref["lifecycle_status"] == "OPPORTUNITY_DISCOVERY"
    for target in (
        "VALIDATION",
        "PRODUCT_DEVELOPMENT",
        "INTERNAL_TESTING",
        "READY_FOR_LAUNCH",
        "CUSTOMER_ACQUISITION",
        "OPERATING",
        "PAUSED",
        "RESTRUCTURING",
        "WINDING_DOWN",
        "ARCHIVED",
    ):
        ref = service.transition_lifecycle(
            portfolio["id"],
            ref["id"],
            target,
            reason=f"advance to {target}",
            approved=target
            in {
                "CUSTOMER_ACQUISITION",
                "OPERATING",
                "PAUSED",
                "RESTRUCTURING",
                "WINDING_DOWN",
                "ARCHIVED",
            },
            actor="owner",
        )
        assert ref["lifecycle_status"] == target
    history = service.venture_detail(portfolio["id"], ref["id"], actor="owner")["lifecycle"][
        "history"
    ]
    assert [row["to"] for row in history][-1] == "ARCHIVED"
    assert all(row["actor"] == "owner" and row["reason"] for row in history)
    with pytest.raises(LifecycleGateError):
        service.transition_lifecycle(
            portfolio["id"], ref["id"], "VALIDATION", reason="reopen", actor="owner"
        )


def test_evaluation_explains_components_missing_inputs_and_confidence() -> None:
    service, portfolio, ref = portfolio_and_venture()
    result = service.evaluate(portfolio["id"], {"policy": "balanced"}, actor="owner")
    row = next(item for item in result["evaluations"] if item["venture_id"] == ref["id"])
    assert row["methodology"]
    assert row["components"]
    assert all("method" in component for component in row["components"])
    assert all("confidence" in component for component in row["components"])
    assert row["coverage"] > 0
    assert row["weights"]
    assert row["evaluated_at"] == FIXED.isoformat()


def test_custom_report_period_validation_and_snapshot_period() -> None:
    service, portfolio, _ref = portfolio_and_venture()
    with pytest.raises(PortfolioValidationError):
        service.generate_report(portfolio["id"], {"period": "custom"}, actor="owner")
    report = service.generate_report(
        portfolio["id"],
        {
            "period": "custom",
            "period_start": "2026-11-01",
            "period_end": "2026-11-10",
        },
        actor="owner",
    )
    assert report["period"] == {
        "start": "2026-11-01",
        "end": "2026-11-10",
    }
    assert report["period_kind"] == "custom"
    assert report["sections"][2]["title"]


def test_member_role_update_is_persisted() -> None:
    service, portfolio, _ref = portfolio_and_venture()
    service.add_member(portfolio["id"], {"actor": "reviewer", "role": "viewer"}, actor="owner")
    service.add_member(portfolio["id"], {"actor": "reviewer", "role": "analyst"}, actor="owner")
    updated = service.add_member(
        portfolio["id"], {"actor": "reviewer", "role": "approver"}, actor="owner"
    )
    assert updated["role"] == "approver"
    stored = next(
        row for row in service._get(portfolio["id"])["members"] if row["actor"] == "reviewer"
    )
    assert stored["role"] == "approver"
    assert stored["all_ventures"] is False


def test_unavailable_source_metric_is_not_consolidated_as_zero() -> None:
    service, portfolio, _ref = portfolio_and_venture()
    original = service._finance_call

    def unavailable_income(method, *args, **kwargs):
        if method == "income_statement":
            return {"__error__": "income statement unavailable"}
        return original(method, *args, **kwargs)

    service._finance_call = unavailable_income  # type: ignore[method-assign]
    analytics = service.portfolio_analytics(portfolio["id"], actor="owner")
    assert analytics["totals"]["complete"] is False
    assert analytics["totals"]["revenue"]["available"] is False
    assert analytics["totals"]["revenue"]["value"] is None
    assert "not consolidated as zero" in analytics["totals"]["revenue"]["reason"]
    row = analytics["ventures"][0]
    assert row["eligible_for_consolidation"] is False
    assert "revenue" in row["missing_metrics"]
    assert analytics["data_completeness"]["incomplete"] == 1


def test_launch_delay_and_pause_projections_keep_the_right_costs_and_revenue() -> None:
    finance = make_finance()
    venture = create_venture(finance, "Scenario Math")
    expense = finance.add_expense(
        venture["id"],
        {
            "category": "software_subscriptions",
            "amount": "220.00",
            "status": "actual",
            "transaction_date": "2026-11-01",
            "idempotency_key": "scenario-expense",
        },
        actor="owner",
    )
    finance.approve_expense(
        venture["id"], expense["id"], {"approved": True, "note": "approved"}, actor="owner"
    )
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Scenarios"}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": venture["id"]}, actor="owner")

    delayed = service.simulate_scenario(
        portfolio["id"],
        {
            "type": "launch_delay",
            "venture_id": venture["id"],
            "assumptions": {"delay_months": 3},
            "horizon_months": 12,
        },
        actor="owner",
    )
    assert delayed["projected"]["revenue"] == 9900.0
    assert delayed["projected"]["expenses"] == 2640.0
    assert delayed["ventures"][0]["simulated_monthly_revenue"] == 1100.0

    paused = service.simulate_scenario(
        portfolio["id"],
        {
            "type": "pause_venture",
            "venture_id": venture["id"],
            "assumptions": {"pause_months": 3, "pause_cost": 300},
            "horizon_months": 12,
        },
        actor="owner",
    )
    assert paused["projected"]["revenue"] == 9900.0
    assert paused["projected"]["expenses"] == 2940.0


def test_new_venture_scenario_is_one_hypothetical_venture_not_a_copy_per_existing_one() -> None:
    service, portfolio, _ref = portfolio_and_venture()
    scenario = service.simulate_scenario(
        portfolio["id"],
        {
            "type": "new_venture",
            "assumptions": {
                "name": "New line",
                "initial_investment": 50000,
                "expected_monthly_revenue": 2000,
                "monthly_operating_cost": 1500,
            },
            "horizon_months": 12,
        },
        actor="owner",
    )
    assert scenario["available"] is True
    assert len(scenario["ventures"]) == 1
    assert scenario["ventures"][0]["venture_id"] == ""
    assert scenario["ventures"][0]["actual_monthly_revenue"] == 0.0
    assert scenario["projected"] == {
        "revenue": 24000.0,
        "expenses": 18000.0,
        "net_cash_flow": 6000.0,
        "ending_cash": 56000.0,
        "risk_exposure": "low",
    }


def test_resource_policy_cannot_enable_automatic_reallocation_or_disable_approval() -> None:
    _service, portfolio, _ref = portfolio_and_venture()
    service = _service
    updated = service.update_portfolio(
        portfolio["id"],
        {
            "resource_policy": {
                "require_approval": False,
                "allow_automatic_reallocation": True,
            }
        },
        actor="owner",
    )
    assert updated["resource_policy"]["require_approval"] is True
    assert updated["resource_policy"]["allow_automatic_reallocation"] is False

    created = service.create_portfolio(
        {
            "name": "Policy Create",
            "resource_policy": {
                "require_approval": False,
                "allow_automatic_reallocation": True,
            },
        },
        actor="owner",
    )
    assert created["resource_policy"]["require_approval"] is True
    assert created["resource_policy"]["allow_automatic_reallocation"] is False


def test_reallocation_rejection_updates_every_item() -> None:
    finance = make_finance()
    first = create_venture(finance, "First allocation")
    second = create_venture(finance, "Second allocation")
    service = make_portfolio(finance)
    portfolio = service.create_portfolio({"name": "Allocations"}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": first["id"]}, actor="owner")
    service.add_venture(portfolio["id"], {"venture_id": second["id"]}, actor="owner")
    batch = service.propose_allocations(portfolio["id"], actor="owner")
    approved = service.decide_allocation(
        portfolio["id"], batch["id"], {"approved": True, "note": "try"}, actor="owner"
    )
    assert all(item["status"] == "approved" for item in approved["items"])
    rejected = service.decide_allocation(
        portfolio["id"], batch["id"], {"approved": False, "note": "reversed"}, actor="owner"
    )
    assert rejected["status"] == "rejected"
    assert all(item["status"] == "rejected" for item in rejected["items"])
    assert all(item["applied"] is False for item in rejected["items"])


def test_loaded_duplicate_venture_binding_is_repaired_and_reported() -> None:
    finance = make_finance()
    service, portfolio, ref = portfolio_and_venture(finance)
    second = service.create_portfolio({"name": "Second"}, actor="owner")
    service._get(second["id"])["ventures"].append(copy.deepcopy(ref))
    service._bindings[ref["id"]] = portfolio["id"]
    service._persist()
    restored = make_portfolio(finance, store=service._store)
    second_row = restored.detail(second["id"], actor="owner")
    assert second_row["counts"]["ventures"] == 0
    assert any("already bound" in error for error in restored._load_errors)
    assert restored._bindings[ref["id"]] == portfolio["id"]


def test_restricted_venture_is_removed_from_detail_reports_dashboard_and_lists() -> None:
    service, portfolio, ref = portfolio_and_venture()
    service.add_member(portfolio["id"], {"actor": "outsider", "role": "viewer"}, actor="owner")
    stored = service._get(portfolio["id"])["ventures"][0]
    stored["visibility"] = "restricted"
    stored["allowed_members"] = []
    service._persist()
    service.transition_lifecycle(
        portfolio["id"],
        ref["id"],
        "PAUSED",
        reason="restricted pause",
        approved=True,
        actor="owner",
    )
    service.add_knowledge(
        portfolio["id"],
        {
            "kind": "historical_observation",
            "title": "Restricted observation",
            "body": "Restricted body.",
            "venture_id": ref["id"],
            "visibility": "portfolio",
            "evidence": ["Restricted evidence."],
        },
        actor="owner",
    )
    service.record_risk(
        portfolio["id"],
        {
            "category": "financial",
            "severity": "high",
            "venture_id": ref["id"],
            "evidence": ["Restricted evidence."],
            "observed": "Restricted condition.",
            "mitigation": "Restricted mitigation.",
        },
        actor="owner",
    )
    decision = service.propose_decision(
        portfolio["id"],
        {
            "kind": "pause_development",
            "title": "Restricted decision",
            "venture_id": ref["id"],
            "evidence": ["Restricted evidence."],
        },
        actor="owner",
    )
    service.simulate_scenario(
        portfolio["id"],
        {
            "type": "revenue_shortfall",
            "venture_id": ref["id"],
            "assumptions": {"shortfall_pct": 20},
        },
        actor="owner",
    )
    allocations = service.propose_allocations(portfolio["id"], actor="owner")
    recommendations = service.generate_recommendations(portfolio["id"], actor="owner")
    report = service.generate_report(portfolio["id"], {"period": "monthly"}, actor="owner")

    detail = service.detail(portfolio["id"], actor="outsider")
    assert "ventures" not in detail["portfolio"]
    assert ref["id"] not in json.dumps(detail)
    assert detail["counts"]["open_alerts"] == 0

    dashboard = service.dashboard(portfolio["id"], actor="outsider")
    assert ref["id"] not in json.dumps(dashboard)
    assert dashboard["alerts"]["open"] == 0
    assert dashboard["venture_comparison"] == []

    assert service.list_risks(portfolio["id"], actor="outsider")["risks"] == []
    assert service.list_decisions(portfolio["id"], actor="outsider")["decisions"] == []
    assert service.list_scenarios(portfolio["id"], actor="outsider")["scenarios"] == []
    assert service.list_alerts(portfolio["id"], actor="outsider")["alerts"] == []
    assert service.list_reports(portfolio["id"], actor="outsider")["reports"] == []
    assert service.list_knowledge(portfolio["id"], actor="outsider")["knowledge"] == []
    assert ref["id"] not in json.dumps(service.list_allocations(portfolio["id"], actor="outsider"))
    assert ref["id"] not in json.dumps(
        service.list_recommendations(portfolio["id"], actor="outsider")
    )
    assert ref["id"] not in json.dumps(service.history(portfolio["id"], actor="outsider"))
    assert service.list_portfolios(actor="outsider")["portfolios"][0]["open_alerts"] == 0
    with pytest.raises(PortfolioAuthorizationError):
        service.get_report(portfolio["id"], report["id"], actor="outsider")

    assert decision["venture_id"] == ref["id"]
    assert allocations["items"]
    assert recommendations["recommendations"]
