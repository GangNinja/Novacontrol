"""Phase 38 HTTP surface: finance workspace, route contract and error mapping."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS

TOKEN = "phase38-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class FinanceApiTests(unittest.TestCase):
    """One app for the whole module; each test creates its own venture."""

    @classmethod
    def setUpClass(cls) -> None:
        from novacontrol.api import app as app_module
        from novacontrol.application import NovaControlApplication
        from novacontrol.browser import NoopBrowserRunner
        from novacontrol.desktop import NoopDesktopRunner

        cls.tmp = TemporaryDirectory()
        applications = []

        def build(**kwargs):
            kwargs["data_dir"] = Path(cls.tmp.name)
            application = NovaControlApplication(**kwargs)
            applications.append(application)
            return application

        cls.env = mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": TOKEN, "NOVACONTROL_DISABLE_OLLAMA": "1"})
        cls.env.start()
        cls.patches = [
            mock.patch.object(app_module, "NovaControlApplication", side_effect=build),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patch in cls.patches:
            patch.start()
        cls.client = TestClient(app_module.create_app())
        cls.client.__enter__()
        cls.app = cls.client.app
        cls.applications = applications

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        for patch in reversed(cls.patches):
            patch.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def create_venture(self, name: str = "HTTP Studio") -> dict:
        response = self.client.post(
            "/finance/ventures",
            headers=HEADERS,
            json={
                "name": name,
                "business_model": "saas",
                "reporting_currency": "USD",
                "linked_products": ["prod-http"],
                "customer_refs": ["cust-http"],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_finance_workspace_is_served_and_wired(self) -> None:
        page = self.client.get("/")
        assert page.status_code == 200
        html = page.text
        for marker in (
            'data-panel="financePanel"',
            'id="financePanel"',
            'id="financeVentureList"',
            'id="financeDashboard"',
            'id="financeJournalLines"',
            'src="/static/js/render-finance.js',
        ):
            assert marker in html, marker
        assert "?v=__NC_ASSET_VERSION__" not in html
        assert "?v=2026" not in html

        script = self.client.get("/static/js/render-finance.js")
        assert script.status_code == 200
        assert script.headers["cache-control"] == "no-cache, no-store, max-age=0, must-revalidate"
        assert "renderFinanceDashboard" in script.text
        assert "setupFinanceWorkspace();" in script.text

        utils = self.client.get("/static/js/render-utils.js")
        assert utils.status_code == 200
        assert 'type === "financeList"' in utils.text
        assert 'type === "financeDetail"' in utils.text
        assert 'type === "financeDashboard"' in utils.text

    def test_every_finance_route_requires_authentication(self) -> None:
        assert self.client.get("/finance/status").status_code == 401
        assert self.client.get("/finance/ventures").status_code == 401
        assert self.client.post("/finance/ventures", json={"name": "No auth"}).status_code == 401
        assert self.client.get("/finance/ventures/ven-x").status_code == 401
        assert self.client.get("/finance/status", headers=HEADERS).status_code == 200

    def test_route_registry_declares_every_finance_handler(self) -> None:
        declared = [
            row for row in ApiSurface.default().routes if row.path.startswith("/finance")
        ]
        actual = [
            route
            for route in self.app.routes
            if getattr(route, "path", "").startswith("/finance") and getattr(route, "methods", None)
        ]
        pairs = {
            (method, route.path)
            for route in actual
            for method in route.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        declared_pairs = {(row.method.value if hasattr(row.method, "value") else str(row.method), row.path) for row in declared}
        assert pairs == declared_pairs
        assert len(declared_pairs) == 49
        finance_consumers = [
            (method, path, consumer)
            for (method, path), consumer in ROUTE_CONSUMERS.items()
            if path.startswith("/finance")
        ]
        assert len(finance_consumers) == 49
        assert any(consumer == "renderFinanceDashboard" for _, _, consumer in finance_consumers)
        assert any(consumer == "renderFinanceList" for _, _, consumer in finance_consumers)

    def test_http_lifecycle_posts_balanced_statements(self) -> None:
        venture = self.create_venture("Lifecycle Studio")
        venture_id = venture["id"]
        base = f"/finance/ventures/{venture_id}"

        revenue = self.client.post(
            f"{base}/revenue",
            headers=HEADERS,
            json={
                "gross_amount": "1100.00",
                "tax": "110.00",
                "stage": "recognized",
                "transaction_date": "2026-11-01",
                "customer_ref": "cust-http",
                "product_ref": "prod-http",
                "idempotency_key": "http-rev-1",
            },
        )
        assert revenue.status_code == 200, revenue.text
        revenue_id = revenue.json()["id"]
        replay = self.client.post(
            f"{base}/revenue",
            headers=HEADERS,
            json={
                "gross_amount": "1100.00",
                "tax": "110.00",
                "stage": "recognized",
                "transaction_date": "2026-11-01",
                "customer_ref": "cust-http",
                "product_ref": "prod-http",
                "idempotency_key": "http-rev-1",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["deduplicated"] is True

        advanced = self.client.post(
            f"{base}/revenue/{revenue_id}/advance", headers=HEADERS, json={"stage": "collected"}
        )
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["stage"] == "collected"

        expense = self.client.post(
            f"{base}/expenses",
            headers=HEADERS,
            json={
                "amount": "200.00",
                "category": "infrastructure",
                "status": "actual",
                "transaction_date": "2026-11-05",
                "vendor": "Cloud Host",
            },
        )
        assert expense.status_code == 200, expense.text
        expense_body = expense.json()
        assert expense_body["payment_status"] == "unpaid"
        assert expense_body["approval_status"] == "pending"
        approval = self.client.post(
            f"{base}/expenses/{expense_body['id']}/approval",
            headers=HEADERS,
            json={"approved": True, "note": "receipt checked"},
        )
        assert approval.status_code == 200, approval.text
        payment = self.client.post(
            f"{base}/expenses/{expense_body['id']}/payment", headers=HEADERS, json={}
        )
        assert payment.status_code == 200, payment.text
        assert payment.json()["payment_status"] == "paid"

        summary = self.client.get(f"{base}/revenue/summary", headers=HEADERS)
        assert summary.status_code == 200
        assert summary.json()["recognized_revenue"] == 1100.0
        assert summary.json()["collected_cash"] == 1100.0
        assert summary.json()["outstanding_invoices"] == 0.0

        income = self.client.get(
            f"{base}/statements/income?start=2026-11-01&end=2026-11-30", headers=HEADERS
        )
        assert income.status_code == 200
        assert income.json()["revenue"] == 1100.0
        assert income.json()["operating_expenses"] == 200.0

        trial = self.client.get(f"{base}/trial-balance", headers=HEADERS)
        assert trial.status_code == 200
        assert trial.json()["balanced"] is True

        dashboard = self.client.get(f"{base}/dashboard", headers=HEADERS)
        assert dashboard.status_code == 200
        body = dashboard.json()
        for key in ("income_statement", "balance_sheet", "trial_balance", "cash_flow", "revenue", "expenses"):
            assert key in body
        assert body["balance_sheet"]["balanced"] is True
        assert body["boundaries"]["payments"] is False
        assert body["revenue"]["recognized_revenue"] == 1100.0
        assert body["legend"]["forecast"].startswith("Modelled")

        detail = self.client.get(base, headers=HEADERS)
        assert detail.status_code == 200
        assert detail.json()["boundaries"]["ledger_mutation"] is False
        assert detail.json()["links"]["customer_references"][0]["reader"] == "phase37"

        status = self.client.get("/finance/status", headers=HEADERS)
        assert status.status_code == 200
        assert status.json()["ventures"] >= 1
        assert status.json()["boundaries"]["payments"] is False

    def test_journal_period_budget_forecast_and_health_over_http(self) -> None:
        venture = self.create_venture("Controls Studio")
        venture_id = venture["id"]
        base = f"/finance/ventures/{venture_id}"

        entry = self.client.post(
            f"{base}/journal",
            headers=HEADERS,
            json={
                "date": "2026-11-02",
                "memo": "Owner funding",
                "lines": [
                    {"account_code": "1000", "debit": "2500.00", "credit": "0"},
                    {"account_code": "3000", "debit": "0", "credit": "2500.00"},
                ],
            },
        )
        assert entry.status_code == 200, entry.text
        journal_id = entry.json()["id"]
        reversal = self.client.post(
            f"{base}/journal/{journal_id}/reverse",
            headers=HEADERS,
            json={"reason": "duplicate entry", "date": "2026-11-06"},
        )
        assert reversal.status_code == 200, reversal.text
        assert reversal.json()["original"]["status"] == "reversed"
        again = self.client.post(
            f"{base}/journal/{journal_id}/reverse", headers=HEADERS, json={"reason": "again"}
        )
        assert again.status_code == 409

        period = self.client.post(
            f"{base}/periods/close",
            headers=HEADERS,
            json={"start": "2026-11-01", "end": "2026-11-30"},
        )
        assert period.status_code == 200, period.text
        closed = self.client.post(
            f"{base}/journal",
            headers=HEADERS,
            json={
                "date": "2026-11-20",
                "memo": "late",
                "lines": [
                    {"account_code": "1000", "debit": "1.00", "credit": "0"},
                    {"account_code": "3000", "debit": "0", "credit": "1.00"},
                ],
            },
        )
        assert closed.status_code == 409

        budget = self.client.post(
            f"{base}/budgets",
            headers=HEADERS,
            json={
                "name": "Infrastructure",
                "period_start": "2026-12-01",
                "period_end": "2026-12-31",
                "lines": [{"label": "Hosting", "account_code": "6300", "category": "infrastructure", "amount": "500.00"}],
            },
        )
        assert budget.status_code == 200, budget.text
        budget_id = budget.json()["id"]
        approved = self.client.post(
            f"{base}/budgets/{budget_id}/approval",
            headers=HEADERS,
            json={"approved": True, "note": "plan approved"},
        )
        assert approved.status_code == 200
        report = self.client.get(f"{base}/budgets/{budget_id}/report", headers=HEADERS)
        assert report.status_code == 200
        assert report.json()["available"] is True
        assert report.json()["total_actual"] == 0.0

        forecast = self.client.post(
            f"{base}/forecasts", headers=HEADERS, json={"horizon_months": 3, "scenario": "base"}
        )
        assert forecast.status_code == 200
        assert forecast.json()["available"] is False
        assert forecast.json()["kind"] == "forecast"

        scenarios = self.client.post(
            f"{base}/scenarios", headers=HEADERS, json={"horizon_months": 2}
        )
        assert scenarios.status_code == 200
        assert [row["scenario"] for row in scenarios.json()["scenarios"]] == [
            "conservative",
            "base",
            "optimistic",
            "custom",
        ]

        health = self.client.post(f"{base}/health/refresh", headers=HEADERS)
        assert health.status_code == 200, health.text
        # The period has postings (all reversed) so the data is fresh, but no
        # threshold alert is invented for an otherwise empty venture.
        assert health.json()["data_fresh"] is True
        alerts = self.client.get(f"{base}/alerts", headers=HEADERS)
        assert alerts.status_code == 200
        assert isinstance(alerts.json()["alerts"], list)

        management = self.client.post(
            f"{base}/reports",
            headers=HEADERS,
            json={"title": "Controls review", "period_start": "2026-11-01", "period_end": "2026-11-30"},
        )
        assert management.status_code == 200, management.text
        body = management.json()
        assert body["income_statement"]["statement"] == "income_statement"
        assert body["cash_flow"]["statement"] == "cash_flow"
        assert body["forecast"]["kind"] == "forecast"
        assert any(section["key"] == "profit_and_loss" for section in body["sections"])
        assert all(item["requires_human_authorization"] is True for item in body["recommendations"])
        assert body["limitations"]

    def test_api_error_mapping(self) -> None:
        assert self.client.get("/finance/ventures/ven-missing", headers=HEADERS).status_code == 404
        assert self.client.post(
            "/finance/ventures/ven-missing/revenue",
            headers=HEADERS,
            json={"gross_amount": "1.00", "transaction_date": "2026-11-01"},
        ).status_code == 404

        venture = self.create_venture("Errors Studio")
        base = f"/finance/ventures/{venture['id']}"
        malformed = self.client.post(
            f"{base}/revenue",
            headers=HEADERS,
            json={"gross_amount": "not-a-number", "transaction_date": "2026-11-01"},
        )
        assert malformed.status_code == 422
        missing_date = self.client.post(
            f"{base}/revenue", headers=HEADERS, json={"gross_amount": "10.00"}
        )
        assert missing_date.status_code == 422
        unauthorized_budget = self.client.post(
            f"{base}/budgets/missing/approval", headers=HEADERS, json={"approved": True, "note": "x"}
        )
        assert unauthorized_budget.status_code == 404
        bad_body = self.client.post(f"{base}/journal", headers=HEADERS, json={"lines": []})
        assert bad_body.status_code == 422

    def test_operations_and_agent_are_exposed_without_external_actions(self) -> None:
        venture = self.create_venture("Operations Studio")
        base = f"/finance/ventures/{venture['id']}"
        objective = self.client.post(
            f"{base}/objectives", headers=HEADERS, json={"title": "Launch", "outcome": "Ship"}
        )
        assert objective.status_code == 200
        task = self.client.post(
            f"{base}/tasks",
            headers=HEADERS,
            json={"title": "Build", "objective_id": objective.json()["id"], "due_date": "2026-11-20"},
        )
        assert task.status_code == 200
        updated = self.client.post(
            f"{base}/tasks/{task.json()['id']}",
            headers=HEADERS,
            json={"status": "complete", "delivery_days": 4},
        )
        assert updated.status_code == 200
        operations = self.client.get(f"{base}/operations", headers=HEADERS)
        assert operations.status_code == 200
        assert operations.json()["completed"] == 1
        assert operations.json()["completion_rate"]["value"] == 1.0

        agent_status = self.applications[0].business_finance.status()
        assert agent_status["boundaries"]["payments"] is False
        assert agent_status["boundaries"]["ledger_mutation"] is False
