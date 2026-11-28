"""Phase 38 service tests: business operations and financial intelligence."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from novacontrol.agents.models import AgentRole, AgentTask, AgentTaskStatus
from novacontrol.finops import (
    FinOpsAuthorizationError,
    FinOpsBudgetExceeded,
    FinOpsConflict,
    FinOpsValidationError,
    FinOpsService,
    PeriodClosed,
    VentureNotFound,
)
from novacontrol.finops.agent import build_finance_agent

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


def make_service(**kwargs) -> FinOpsService:
    kwargs.setdefault("clock", lambda: FIXED)
    return FinOpsService(store=MemoryStore(), **kwargs)


def venture(service: FinOpsService, **overrides) -> dict:
    payload = {
        "name": "North Star Studio",
        "business_model": "saas",
        "reporting_currency": "USD",
        "linked_products": ["prod-1"],
        "customer_refs": ["cust-1"],
    }
    payload.update(overrides)
    return service.create_venture(payload, actor="owner")


def recognized_revenue(service: FinOpsService, venture_id: str, **overrides) -> dict:
    payload = {
        "gross_amount": "1100.00",
        "tax": "110.00",
        "stage": "recognized",
        "transaction_date": "2026-11-01",
        "customer_ref": "cust-1",
        "product_ref": "prod-1",
        "idempotency_key": overrides.pop("idempotency_key", "rev-1"),
    }
    payload.update(overrides)
    return service.add_revenue(venture_id, payload, actor="owner")


def actual_expense(service: FinOpsService, venture_id: str, **overrides) -> dict:
    payload = {
        "amount": "200.00",
        "category": "infrastructure",
        "status": "actual",
        "payment_status": "unpaid",
        "transaction_date": "2026-11-05",
        "vendor": "Cloud Host",
        "product_ref": "prod-1",
    }
    payload.update(overrides)
    return service.add_expense(venture_id, payload, actor="owner")


def test_venture_isolation_roles_and_currency_validation() -> None:
    service = make_service()
    first = venture(service, reporting_currency="USD")
    second = venture(service, name="Euro Studio", reporting_currency="EUR")
    assert first["id"] != second["id"]
    assert first["reporting_currency"] == "USD"
    assert second["reporting_currency"] == "EUR"

    revenue = recognized_revenue(service, first["id"])
    assert revenue["currency"] == "USD"
    assert service.revenue_summary(first["id"], actor="owner")["recognized_revenue"] == 1100.0
    assert service.revenue_summary(second["id"], actor="owner")["available"] is False

    service.add_member(first["id"], {"actor": "viewer", "role": "viewer"}, actor="owner")
    service.add_member(first["id"], {"actor": "approver", "role": "approver"}, actor="owner")
    assert service.detail(first["id"], actor="viewer")["venture"]["id"] == first["id"]
    with pytest.raises(FinOpsAuthorizationError):
        service.add_revenue(first["id"], {"gross_amount": "10.00", "transaction_date": "2026-11-01"}, actor="viewer")
    with pytest.raises(FinOpsAuthorizationError):
        service.approve_expense(first["id"], "missing", {"approved": True, "note": "x"}, actor="analyst")
    with pytest.raises(FinOpsAuthorizationError):
        service.detail(first["id"], actor="stranger")
    with pytest.raises(VentureNotFound):
        service.detail("ven-missing", actor="owner")

    with pytest.raises((FinOpsValidationError, ValueError)):
        service.create_venture({"name": "Bad", "reporting_currency": "US"}, actor="owner")


def test_revenue_lifecycle_posts_only_its_own_stage_and_preserves_cash_and_ar() -> None:
    service = make_service()
    row = venture(service)
    revenue = recognized_revenue(service, row["id"])
    assert revenue["stage"] == "recognized"
    summary = service.revenue_summary(row["id"], actor="owner")
    assert summary["booked_revenue"] == 1100.0
    assert summary["invoiced_revenue"] == 1100.0
    assert summary["recognized_revenue"] == 1100.0
    assert summary["collected_cash"] == 0.0
    assert summary["deferred_revenue"] == 0.0
    assert summary["outstanding_invoices"] == 1100.0
    assert summary["taxes"] == 110.0
    assert summary["counts"]["recognized"] == 1

    collected = service.advance_revenue(row["id"], revenue["id"], "collected", actor="owner")
    assert collected["stage"] == "collected"
    summary = service.revenue_summary(row["id"], actor="owner")
    assert summary["collected_cash"] == 1100.0
    assert summary["outstanding_invoices"] == 0.0
    assert service.balance_sheet(row["id"], actor="owner")["balanced"] is True

    with pytest.raises(FinOpsConflict):
        service.advance_revenue(row["id"], revenue["id"], "invoiced", actor="owner")


def test_booked_revenue_is_not_revenue_and_an_idempotent_retry_is_not_duplicated() -> None:
    service = make_service()
    row = venture(service)
    payload = {"gross_amount": "500.00", "stage": "booked", "transaction_date": "2026-11-01", "idempotency_key": "book-1"}
    first = service.add_revenue(row["id"], payload, actor="owner")
    second = service.add_revenue(row["id"], payload, actor="owner")
    assert second["deduplicated"] is True
    assert second["id"] == first["id"]
    summary = service.revenue_summary(row["id"], actor="owner")
    assert summary["booked_revenue"] == 500.0
    assert summary["recognized_revenue"] == 0.0
    assert service.detail(row["id"], actor="owner")["trial_balance"]["available"] is False

    with pytest.raises(FinOpsConflict):
        service.add_revenue(
            row["id"],
            {"gross_amount": "10.00", "transaction_date": "2026-11-01", "source_ref": "invoice-1"},
            actor="owner",
        )
        service.add_revenue(
            row["id"],
            {"gross_amount": "10.00", "transaction_date": "2026-11-01", "source_ref": "invoice-1"},
            actor="owner",
        )


def test_refund_is_contra_revenue_and_retries_do_not_double_refund() -> None:
    service = make_service()
    row = venture(service)
    revenue = service.add_revenue(
        row["id"],
        {
            "gross_amount": "1100.00",
            "tax": "110.00",
            "stage": "collected",
            "transaction_date": "2026-11-01",
        },
        actor="owner",
    )
    refund_payload = {"amount": "500.00", "reason": "service credit", "date": "2026-11-10"}
    first = service.refund_revenue(row["id"], revenue["id"], refund_payload, actor="owner")
    second = service.refund_revenue(row["id"], revenue["id"], refund_payload, actor="owner")
    assert second["deduplicated"] is True
    assert second["journal_entry_ids"] == first["journal_entry_ids"]

    summary = service.revenue_summary(row["id"], actor="owner")
    assert summary["recognized_revenue"] == 1100.0
    assert summary["refunds"] == 500.0
    before_refund = service.revenue_summary(row["id"], actor="owner", as_of=__import__("datetime").date(2026, 11, 9))
    assert before_refund["refunds"] == 0.0
    income = service.income_statement(row["id"], actor="owner")
    assert income["gross_revenue"] == 1100.0
    assert income["contra_revenue"] == 500.0
    assert income["revenue"] == 600.0
    assert service.balance_sheet(row["id"], actor="owner")["balanced"] is True

    with pytest.raises(FinOpsValidationError):
        service.refund_revenue(
            row["id"], revenue["id"], {"amount": "800.00", "reason": "too much"}, actor="owner"
        )
    booked = service.add_revenue(
        row["id"],
        {"gross_amount": "50.00", "stage": "booked", "transaction_date": "2026-11-02"},
        actor="owner",
    )
    with pytest.raises(FinOpsConflict):
        service.refund_revenue(row["id"], booked["id"], {"amount": "10.00", "reason": "early"}, actor="owner")


def test_expense_estimates_never_post_and_approval_payment_post_once() -> None:
    service = make_service()
    row = venture(service)
    estimated = service.add_expense(
        row["id"],
        {"amount": "900.00", "category": "marketing", "status": "committed", "transaction_date": "2026-11-03"},
        actor="owner",
    )
    assert estimated["status"] == "committed"
    assert service.detail(row["id"], actor="owner")["trial_balance"]["available"] is False

    expense = actual_expense(service, row["id"])
    assert expense["approval_status"] == "pending"
    summary = service.expense_summary(row["id"], actor="owner")
    assert summary["actual"] == 0.0
    assert summary["unapproved_actual"] == 200.0

    approved = service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "receipt checked"}, actor="owner")
    assert approved["approval_status"] == "approved"
    summary = service.expense_summary(row["id"], actor="owner")
    assert summary["actual"] == 200.0
    assert summary["unapproved_actual"] == 0.0
    balance = service.balance_sheet(row["id"], actor="owner")
    assert balance["balanced"] is True
    assert balance["total_liabilities"] == 200.0

    # Re-approval must not duplicate the accrual.
    service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "receipt checked again"}, actor="owner")
    accruals = [entry for entry in service.list_ventures()["ventures"] if entry["id"] == row["id"]]
    assert accruals
    journal = service.journal(row["id"], actor="owner")
    assert len([entry for entry in journal["entries"] if entry["idempotency_key"].endswith(":accrual")]) == 1

    paid = service.pay_expense(row["id"], expense["id"], {}, actor="owner")
    assert paid["payment_status"] == "paid"
    replay = service.pay_expense(row["id"], expense["id"], {}, actor="owner")
    assert replay["deduplicated"] is True
    assert replay["journal_entry_ids"] == paid["journal_entry_ids"]
    summary = service.expense_summary(row["id"], actor="owner")
    assert summary["actual"] == 200.0
    assert service.working_capital(row["id"], actor="owner")["cash_balance"] == -200.0


def test_expense_failed_approval_leaves_record_retryable() -> None:
    service = make_service()
    row = venture(service)
    expense = actual_expense(service, row["id"], transaction_date="2026-09-05")
    service.close_period(row["id"], {"start": "2026-09-01", "end": "2026-09-30"}, actor="owner")
    with pytest.raises(PeriodClosed):
        service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "late"}, actor="owner")
    stored = next(item for item in service.list_expenses(row["id"], actor="owner") if item["id"] == expense["id"])
    assert stored["approval_status"] == "pending"
    assert service.detail(row["id"], actor="owner")["trial_balance"]["available"] is False


def test_journal_validation_reversal_and_immutability() -> None:
    service = make_service()
    row = venture(service)
    good = service.post_journal(
        row["id"],
        {
            "date": "2026-11-04",
            "memo": "Owner funding",
            "lines": [
                {"account_code": "1000", "debit": "2500.00", "credit": "0"},
                {"account_code": "3000", "debit": "0", "credit": "2500.00"},
            ],
            "idempotency_key": "funding-1",
        },
        actor="owner",
    )
    duplicate = service.post_journal(
        row["id"],
        {
            "date": "2026-11-04",
            "memo": "Owner funding",
            "lines": [
                {"account_code": "1000", "debit": "2500.00", "credit": "0"},
                {"account_code": "3000", "debit": "0", "credit": "2500.00"},
            ],
            "idempotency_key": "funding-1",
        },
        actor="owner",
    )
    assert duplicate["deduplicated"] is True
    assert len(service.journal(row["id"], actor="owner")["entries"]) == 1

    for lines in (
        [{"account_code": "1000", "debit": "10.00"}, {"account_code": "3000", "credit": "9.00"}],
        [{"account_code": "9999", "debit": "10.00", "credit": "0"}, {"account_code": "3000", "debit": "0", "credit": "10.00"}],
        [{"account_code": "1000", "debit": "-10.00", "credit": "0"}, {"account_code": "3000", "debit": "0", "credit": "10.00"}],
    ):
        with pytest.raises((FinOpsValidationError, ValueError)):
            service.post_journal(row["id"], {"date": "2026-11-04", "memo": "bad", "lines": lines}, actor="owner")

    reversal = service.reverse_journal(row["id"], good["id"], {"reason": "recorded twice", "date": "2026-11-06"}, actor="owner")
    assert reversal["reversal"]["reverses"] == good["id"]
    original = next(item for item in service.journal(row["id"], actor="owner")["entries"] if item["id"] == good["id"])
    assert original["memo"] == "Owner funding"
    assert original["status"] == "reversed"
    with pytest.raises(FinOpsConflict):
        service.reverse_journal(row["id"], good["id"], {"reason": "again"}, actor="owner")
    with pytest.raises(FinOpsConflict):
        service.reverse_journal(row["id"], reversal["reversal"]["id"], {"reason": "reverse reversal"}, actor="owner")
    assert service.trial_balance(row["id"], actor="owner")["balanced"] is True


def test_closed_periods_refuse_postings_but_allow_open_period_reversals() -> None:
    service = make_service()
    row = venture(service)
    revenue = service.add_revenue(
        row["id"],
        {"gross_amount": "100.00", "stage": "recognized", "transaction_date": "2026-09-05"},
        actor="owner",
    )
    period = service.close_period(row["id"], {"start": "2026-09-01", "end": "2026-09-30"}, actor="owner")
    assert period["status"] == "closed"
    assert period["snapshot"]["income_statement"]["revenue"] == 100.0

    with pytest.raises(PeriodClosed):
        service.post_journal(
            row["id"],
            {
                "date": "2026-09-20",
                "memo": "late entry",
                "lines": [
                    {"account_code": "1000", "debit": "1.00", "credit": "0"},
                    {"account_code": "3000", "debit": "0", "credit": "1.00"},
                ],
            },
            actor="owner",
        )
    # A backdated pending expense is a record, not a posting: it may be entered.
    # Approval is what posts, so the closed period refuses it there.
    late = service.add_expense(
        row["id"],
        {"amount": "5.00", "status": "actual", "transaction_date": "2026-09-21", "category": "other"},
        actor="owner",
    )
    with pytest.raises(PeriodClosed):
        service.approve_expense(row["id"], late["id"], {"approved": True, "note": "late"}, actor="owner")
    with pytest.raises(FinOpsConflict):
        service.close_period(row["id"], {"start": "2026-09-15", "end": "2026-10-15"}, actor="owner")

    entry = service.journal(row["id"], actor="owner")["entries"][0]
    reversal = service.reverse_journal(row["id"], entry["id"], {"reason": "correcting", "date": "2026-10-02"}, actor="owner")
    assert reversal["reversal"]["date"] == "2026-10-02"
    from datetime import date as _date

    assert service.income_statement(row["id"], actor="owner", start=_date(2026, 9, 1), end=_date(2026, 9, 30))["revenue"] == 100.0
    # Reversing the invoice entry moves AR/deferred in October, not revenue;
    # reverse the recognition entry too to observe the revenue reversal there.
    recognition = next(
        item
        for item in service.journal(row["id"], actor="owner")["entries"]
        if any(line["account_code"] == "4000" for line in item["lines"])
    )
    service.reverse_journal(row["id"], recognition["id"], {"reason": "recognized twice", "date": "2026-10-03"}, actor="owner")
    assert service.income_statement(row["id"], actor="owner", start=_date(2026, 10, 1), end=_date(2026, 10, 31))["revenue"] == -100.0
    assert revenue["stage"] == "recognized"


def test_statements_working_capital_and_profitability_are_derived_from_posted_entries() -> None:
    service = make_service()
    row = venture(service)
    revenue = recognized_revenue(service, row["id"])
    service.advance_revenue(row["id"], revenue["id"], "collected", actor="owner")
    expense = actual_expense(service, row["id"])
    service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "checked"}, actor="owner")
    service.pay_expense(row["id"], expense["id"], {}, actor="owner")

    income = service.income_statement(row["id"], actor="owner")
    assert income["revenue"] == 1100.0
    assert income["operating_expenses"] == 200.0
    assert income["net_profit"] == 900.0
    assert income["source"] == "posted_journal_entries"
    assert income["available"] is True

    balance = service.balance_sheet(row["id"], actor="owner")
    assert balance["total_assets"] == 1010.0
    assert balance["total_liabilities"] == 110.0
    assert balance["current_period_earnings"] == 900.0
    assert balance["balanced"] is True

    trial = service.trial_balance(row["id"], actor="owner")
    assert trial["balanced"] is True
    assert trial["difference"] == 0.0

    capital = service.working_capital(row["id"], actor="owner")
    assert capital["cash_balance"] == 1010.0  # 1210 collected - 200 paid; tax is a liability
    assert capital["current_assets"] == 1010.0
    assert capital["current_liabilities"] == 110.0
    assert capital["working_capital"] == 900.0
    assert capital["current_ratio"]["available"] is True
    assert capital["cash_runway_months"]["available"] is False

    profit = service.profitability(row["id"], actor="owner")
    assert profit["revenue"] == 1100.0
    assert profit["net_profit"] == 900.0
    assert profit["gross_margin"] > 0
    assert profit["customer_acquisition_cost"]["available"] is False
    assert profit["revenue_per_product"][0]["product_ref"] == "prod-1"


def test_cash_flow_classification_and_reconciliation() -> None:
    service = make_service()
    row = venture(service)
    service.post_journal(
        row["id"],
        {
            "date": "2026-11-02",
            "memo": "Equipment purchase",
            "cash_flow_category": "investing",
            "lines": [
                {"account_code": "1500", "debit": "400.00", "credit": "0"},
                {"account_code": "1000", "debit": "0", "credit": "400.00"},
            ],
        },
        actor="owner",
    )
    service.post_journal(
        row["id"],
        {
            "date": "2026-11-03",
            "memo": "Owner funding",
            "cash_flow_category": "financing",
            "lines": [
                {"account_code": "1000", "debit": "1000.00", "credit": "0"},
                {"account_code": "3000", "debit": "0", "credit": "1000.00"},
            ],
        },
        actor="owner",
    )
    cash = service.cash_flow(row["id"], actor="owner")
    assert cash["available"] is True
    assert cash["investing"] == -400.0
    assert cash["financing"] == 1000.0
    assert cash["net_change"] == 600.0
    assert cash["closing_cash"] == 600.0

    matched = service.reconcile(row["id"], {"account_code": "1000", "statement_balance": "600.00"}, actor="owner")
    assert matched["status"] == "matched"
    assert matched["difference"] == 0.0
    unmatched = service.reconcile(row["id"], {"account_code": "1000", "statement_balance": "575.00"}, actor="owner")
    assert unmatched["status"] == "unmatched"
    assert unmatched["difference"] == -25.0
    with pytest.raises(FinOpsValidationError):
        service.reconcile(row["id"], {"account_code": "9999", "statement_balance": "1.00"}, actor="owner")


def test_budget_actuals_approval_and_revision_without_transient_events() -> None:
    service = make_service()
    row = venture(service)
    expense = actual_expense(service, row["id"], amount="300.00")
    service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "checked"}, actor="owner")
    budget = service.create_budget(
        row["id"],
        {
            "name": "November infrastructure",
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "lines": [
                {"label": "Hosting", "account_code": "6300", "category": "infrastructure", "amount": "500.00"},
            ],
        },
        actor="owner",
    )
    assert budget["status"] == "draft"
    report = service.budget_report(row["id"], budget["id"], actor="owner")
    assert report["available"] is False
    assert report["total_budget"] == 500.0
    assert report["total_actual"] == 300.0
    assert report["total_variance"] == 200.0

    service.approve_budget(row["id"], budget["id"], {"approved": True, "note": "plan approved"}, actor="owner")
    report = service.budget_report(row["id"], budget["id"], actor="owner")
    assert report["available"] is True
    assert report["approved"] is True

    revised = service.revise_budget(
        row["id"],
        budget["id"],
        {
            "reason": "scope increased",
            "lines": [
                {"label": "Hosting", "account_code": "6300", "category": "infrastructure", "amount": "700.00"},
            ],
        },
        actor="owner",
    )
    assert revised["version"] == 2
    assert revised["revises"] == budget["id"]
    assert revised["status"] == "draft"
    assert revised["total_budget"] == "700.00"
    assert revised["approved_by"] == ""
    events = [event["type"] for event in service.detail(row["id"], actor="owner")["venture"]["events"]]
    assert events.count("budget_created") == 1
    assert events.count("budget_revised") == 1


def test_forecasts_require_observed_history_and_remain_estimates() -> None:
    service = make_service()
    row = venture(service)
    empty = service.create_forecast(row["id"], {"horizon_months": 3}, actor="owner")
    assert empty["available"] is False
    assert len(empty["missing_inputs"]) == 2
    assert empty["rows"] == []

    for month, amount in (("2026-09-05", "1000.00"), ("2026-10-05", "1200.00")):
        service.add_revenue(
            row["id"],
            {"gross_amount": amount, "stage": "recognized", "transaction_date": month},
            actor="owner",
        )
    for month, amount in (("2026-09-10", "300.00"), ("2026-10-10", "400.00")):
        expense = service.add_expense(
            row["id"],
            {
                "amount": amount,
                "category": "hosting",
                "status": "actual",
                "transaction_date": month,
                "product_ref": "prod-1",
            },
            actor="owner",
        )
        service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "observed"}, actor="owner")

    forecast = service.create_forecast(row["id"], {"horizon_months": 3, "scenario": "base"}, actor="owner")
    assert forecast["available"] is True
    assert forecast["kind"] == "forecast"
    assert forecast["scenario"] == "base"
    assert len(forecast["rows"]) == 3
    assert [row["month"] for row in forecast["rows"]] == [1, 2, 3]
    assert forecast["rows"][0]["forecast_revenue"] == 1100.0
    assert forecast["rows"][0]["forecast_expenses"] == 350.0
    assert forecast["assumptions"]["starting_cash"] == 0.0
    assert "not actual" in " ".join(forecast["limitations"]).lower()
    assert forecast["confidence"] in {"low", "medium", "medium-high"}

    scenarios = service.scenario_analysis(row["id"], {"horizon_months": 2, "custom": {"revenue_factor": 2.0}}, actor="owner")
    names = [row["scenario"] for row in scenarios["scenarios"]]
    assert names == ["conservative", "base", "optimistic", "custom"]
    assert scenarios["scenarios"][3]["rows"][0]["forecast_revenue"] > scenarios["scenarios"][1]["rows"][0]["forecast_revenue"]

    summary = service.revenue_summary(row["id"], actor="owner")
    assert summary["recognized_revenue"] == 2200.0  # forecasts never enter revenue.


def test_health_gate_reports_missing_and_stale_data_without_alerts() -> None:
    service = make_service()
    row = venture(service)
    empty = service.refresh_health(row["id"], actor="owner")
    assert empty["data_fresh"] is False
    assert empty["alerts"] == []
    assert empty["metrics"] == {}
    assert "No financial record" in empty["freshness"]["reason"]
    assert service.list_alerts(row["id"], actor="owner") == []

    old = make_service(clock=lambda: datetime(2027, 5, 15, 12, 0, tzinfo=UTC))
    old_row = venture(old)
    recognized_revenue(old, old_row["id"])
    stale = old.refresh_health(old_row["id"], actor="owner")
    assert stale["data_fresh"] is False
    assert stale["alerts"] == []
    assert "older than" in stale["freshness"]["reason"]


def test_health_threshold_alert_is_evidence_backed_and_resolvable() -> None:
    service = make_service()
    row = venture(service)
    revenue = recognized_revenue(service, row["id"])
    expense = actual_expense(service, row["id"])
    service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "checked"}, actor="owner")
    result = service.refresh_health(row["id"], actor="owner")
    assert result["data_fresh"] is True
    assert result["alerts"]
    concentration = next(alert for alert in result["alerts"] if alert["metric"] == "revenue_concentration")
    assert concentration["observed_value"] == 1.0
    assert concentration["evidence"]
    assert concentration["recommended_investigation"]
    stored = service.list_alerts(row["id"], actor="owner", status="open")
    assert stored and stored[0]["id"]
    resolved = service.resolve_alert(row["id"], stored[0]["id"], {"note": "contract diversification started"}, actor="owner")
    assert resolved["status"] == "resolved"
    assert service.status()["open_alerts"] >= 0
    assert revenue["stage"] == "recognized"


def test_operations_dashboard_and_task_dependencies() -> None:
    service = make_service()
    row = venture(service)
    objective = service.add_operational_objective(row["id"], {"title": "Launch v2", "outcome": "Public launch"}, actor="owner")
    first = service.add_operational_task(
        row["id"],
        {"title": "Build", "objective_id": objective["id"], "due_date": "2026-11-10", "status": "pending"},
        actor="owner",
    )
    service.add_operational_task(
        row["id"],
        {"title": "Publish", "objective_id": objective["id"], "dependencies": [first["id"]]},
        actor="owner",
    )
    service.update_operational_task(row["id"], first["id"], {"status": "complete", "delivery_days": 4, "resource_utilization": 0.5}, actor="owner")
    service.add_milestone(row["id"], {"title": "Beta"}, actor="owner")
    service.add_operational_risk(row["id"], {"title": "Vendor delay", "severity": "high"}, actor="owner")
    service.add_workflow(row["id"], {"name": "Release", "steps": ["build", "publish"]}, actor="owner")
    operations = service.operations(row["id"], actor="owner")
    assert operations["tasks"] == 2
    assert operations["completed"] == 1
    assert operations["completion_rate"]["value"] == 0.5
    assert operations["average_delivery_days"]["value"] == 4.0
    assert operations["open_risks"] == 1
    assert operations["milestones"] == 1
    assert operations["workflows"] == 1
    assert operations["objectives"] == 1
    with pytest.raises(FinOpsValidationError):
        service.add_operational_task(row["id"], {"title": "Bad", "dependencies": ["missing"]}, actor="owner")
    with pytest.raises(FinOpsValidationError):
        service.add_operational_objective(row["id"], {"title": "Bad", "status": "unknown"}, actor="owner")


def test_phase37_customer_reader_supplies_profitability_and_health_but_is_not_copied() -> None:
    snapshot = {
        "summary": {"customers": 2},
        "customers": [{"id": "cust-1"}, {"id": "cust-2"}],
        "analytics": {
            "acquisition": {"acquisition_cost": {"available": True, "value": 150.0}},
            "retention": {"churn_rate": 0.03},
        },
    }
    service = make_service(customers=lambda _ref: snapshot)
    row = venture(service)
    revenue = recognized_revenue(service, row["id"])
    expense = actual_expense(service, row["id"])
    service.approve_expense(row["id"], expense["id"], {"approved": True, "note": "checked"}, actor="owner")
    profit = service.profitability(row["id"], actor="owner")
    assert profit["customer_acquisition_cost"]["available"] is True
    assert profit["customer_acquisition_cost"]["value"] == 150.0
    assert profit["average_revenue_per_customer"]["value"] == 550.0
    assert profit["customer_count"] == 2
    detail = service.detail(row["id"], actor="owner")
    assert detail["links"]["customer_references"][0]["reader"] == "phase37"
    assert all(record["source"] != "phase37_customer" for record in service.list_revenue(row["id"], actor="owner"))
    assert revenue["source"] == "operator_entry"


def test_async_customer_reader_is_reported_unavailable_not_blocking() -> None:
    async def reader(_ref: str) -> dict:
        return {"analytics": {}}

    service = make_service(customers=reader)
    row = venture(service)
    recognized_revenue(service, row["id"], customer_ref="")
    profit = service.profitability(row["id"], actor="owner")
    assert profit["customer_acquisition_cost"]["available"] is False
    health = service.refresh_health(row["id"], actor="owner")
    assert health["data_fresh"] is True
    assert health["alerts"] == []


def test_management_report_is_grounded_and_marks_authorization() -> None:
    service = make_service()
    row = venture(service)
    recognized_revenue(service, row["id"])
    report = service.generate_management_report(
        row["id"],
        {"title": "November review", "period_start": "2026-11-01", "period_end": "2026-11-30"},
        actor="owner",
    )
    assert report["title"] == "November review"
    assert report["revenue"]["recognized_revenue"] == 1100.0
    assert report["executive_summary"]
    assert all(section.get("kind") in {"actual", "estimate", "forecast", "observation"} for section in report["sections"])
    assert all(item.get("requires_human_authorization") is True for item in report["recommendations"])
    assert service.list_reports(row["id"], actor="owner")[0]["id"] == report["id"]


def test_budget_capacity_and_secret_rejection() -> None:
    service = make_service(max_ventures=1)
    row = venture(service)
    with pytest.raises(FinOpsBudgetExceeded):
        venture(service, name="Second")
    with pytest.raises(FinOpsValidationError):
        service.add_revenue(row["id"], {"gross_amount": "10.00", "transaction_date": "2026-11-01", "api_key": "x"}, actor="owner")
    with pytest.raises(FinOpsValidationError):
        service.add_expense(row["id"], {"amount": "10.00", "transaction_date": "2026-11-01", "card_number": "4111"}, actor="owner")
    with pytest.raises(FinOpsValidationError):
        service.create_venture({"name": "Secrets", "reporting_currency": "USD", "private_key": "x"}, actor="owner")


def test_persistence_recovery_and_audit_memory_seams() -> None:
    store = MemoryStore()
    events: list[tuple[str, dict]] = []
    audits: list[dict] = []
    memories: list[dict] = []

    def publish(event: str, payload) -> None:
        events.append((event, dict(payload)))

    def audit(**payload) -> None:
        audits.append(payload)

    def remember(**payload) -> None:
        memories.append(payload)

    service = FinOpsService(store=store, publish=publish, audit=audit, memory=remember, clock=lambda: FIXED)
    row = venture(service)
    recognized_revenue(service, row["id"])
    service.close_period(row["id"], {"start": "2026-11-01", "end": "2026-11-30"}, actor="owner")

    recovered = FinOpsService(store=store, clock=lambda: FIXED)
    detail = recovered.detail(row["id"], actor="owner")
    assert detail["venture"]["name"] == "North Star Studio"
    assert recovered.revenue_summary(row["id"], actor="owner")["recognized_revenue"] == 1100.0
    assert recovered.trial_balance(row["id"], actor="owner")["balanced"] is True
    assert events and events[0][0] == "finance.venture_created"
    assert any(event[0] == "finance.period_closed" for event in events)
    assert any(item["outcome"] == "venture_created" for item in audits)
    assert any(item["outcome"] == "period_closed" for item in audits)
    assert memories and all("key" in item and "importance" in item for item in memories)


def test_status_and_boundaries_are_honest() -> None:
    service = make_service()
    row = venture(service)
    status = service.status()
    assert status["service"] == "business_finance"
    assert status["ventures"] == 1
    assert status["configured"]["customer_reader"] is False
    assert status["boundaries"]["payments"] is False
    assert status["boundaries"]["purchases"] is False
    assert status["boundaries"]["banking_integration"] is False
    assert status["boundaries"]["ledger_mutation"] is False
    detail = service.detail(row["id"], actor="owner")
    assert detail["boundaries"]["external_action_executed"] is False
    assert detail["links"]["products"][0]["available"] is False


def test_financial_agent_is_advisory_and_never_claims_an_action() -> None:
    service = make_service()
    rows = service.list_ventures()["ventures"]
    assert rows == []

    agent = build_finance_agent(service)
    missing = asyncio.run(agent.handle_task(AgentTask(goal="review", metadata={})))
    assert missing.status == AgentTaskStatus.COMPLETED
    assert "Provide venture_id" in missing.content

    row = venture(service)
    recognized_revenue(service, row["id"])
    response = asyncio.run(
        agent.handle_task(
            AgentTask(
                goal="review",
                metadata={"venture_id": row["id"]},
            )
        )
    )
    assert response.status == AgentTaskStatus.COMPLETED
    assert response.role == AgentRole.BUSINESS
    assert response.metadata["advisory_only"] is True
    assert response.metadata["external_action_executed"] is False
    assert "No payment" in response.content
    assert response.metadata["forecast_is_actual"] is False

    failed = asyncio.run(
        agent.handle_task(
            AgentTask(goal="review", metadata={"venture_id": "ven-missing"})
        )
    )
    assert failed.status == AgentTaskStatus.FAILED
    assert "not found" in failed.content
