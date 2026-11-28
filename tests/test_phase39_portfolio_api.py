"""Phase 39 HTTP surface: portfolio workspace, route contract and error mapping."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS

TOKEN = "phase39-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class PortfolioApiTests(unittest.TestCase):
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

        cls.env = mock.patch.dict(
            os.environ,
            {"NOVACONTROL_API_TOKEN": TOKEN, "NOVACONTROL_DISABLE_OLLAMA": "1"},
        )
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

    def create_venture(self, name: str = "Portfolio HTTP Studio") -> dict:
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

    def create_portfolio(self, name: str = "HTTP Portfolio") -> dict:
        response = self.client.post(
            "/portfolios",
            headers=HEADERS,
            json={
                "name": name,
                "base_currency": "USD",
                "objectives": ["cash discipline"],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_portfolio_workspace_is_served_and_wired(self) -> None:
        page = self.client.get("/")
        assert page.status_code == 200
        html = page.text
        for marker in (
            'data-panel="portfolioPanel"',
            'id="portfolioPanel"',
            'id="portfolioList"',
            'id="portfolioDashboard"',
            'id="portfolioCreateButton"',
            'id="portfolioEvaluateButton"',
            'src="/static/js/render-portfolio.js',
        ):
            assert marker in html, marker
        assert "?v=__NC_ASSET_VERSION__" not in html
        script = self.client.get("/static/js/render-portfolio.js")
        assert script.status_code == 200
        assert script.headers["cache-control"] == "no-cache, no-store, max-age=0, must-revalidate"
        assert "renderPortfolioDashboard" in script.text
        assert "setupPortfolioWorkspace();" in script.text
        utils = self.client.get("/static/js/render-utils.js")
        assert 'type === "portfolioList"' in utils.text
        assert 'type === "portfolioDetail"' in utils.text
        assert 'type === "portfolioDashboard"' in utils.text

    def test_every_portfolio_route_requires_authentication(self) -> None:
        assert self.client.get("/portfolio/status").status_code == 401
        assert self.client.get("/portfolios").status_code == 401
        assert self.client.post("/portfolios", json={"name": "No auth"}).status_code == 401
        assert self.client.get("/portfolios/pf-x/dashboard").status_code == 401
        assert self.client.get("/portfolio/status", headers=HEADERS).status_code == 200

    def test_route_registry_declares_every_portfolio_handler(self) -> None:
        declared = [row for row in ApiSurface.default().routes if row.path.startswith("/portfolio")]
        actual = [
            route
            for route in self.app.routes
            if getattr(route, "path", "").startswith("/portfolio")
            and getattr(route, "methods", None)
        ]
        pairs = {
            (method, route.path)
            for route in actual
            for method in route.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        declared_pairs = {
            (row.method.value if hasattr(row.method, "value") else str(row.method), row.path)
            for row in declared
        }
        assert pairs == declared_pairs
        assert len(declared_pairs) == 49
        consumers = [
            (method, path, consumer)
            for (method, path), consumer in ROUTE_CONSUMERS.items()
            if path.startswith("/portfolio")
        ]
        assert len(consumers) == 49
        assert any(consumer == "renderPortfolioDashboard" for _, _, consumer in consumers)

    def test_http_portfolio_registry_and_isolation(self) -> None:
        portfolio = self.create_portfolio("HTTP Registry")
        venture = self.create_venture("HTTP Registered Venture")
        registered = self.client.post(
            f"/portfolios/{portfolio['id']}/ventures",
            headers=HEADERS,
            json={"venture_id": venture["id"]},
        )
        assert registered.status_code == 200, registered.text
        detail = self.client.get(f"/portfolios/{portfolio['id']}", headers=HEADERS)
        assert detail.status_code == 200
        assert detail.json()["counts"]["ventures"] == 1
        duplicate = self.client.post(
            f"/portfolios/{portfolio['id']}/ventures",
            headers=HEADERS,
            json={"venture_id": venture["id"]},
        )
        assert duplicate.status_code == 409
        analytics = self.client.get(f"/portfolios/{portfolio['id']}/analytics", headers=HEADERS)
        assert analytics.status_code == 200
        assert analytics.json()["totals"]["revenue"]["value"] == 0.0
        assert (
            analytics.json()["inter_venture"]["duplicate_venture_binding"]
            == "rejected at registration"
        )

    def test_lifecycle_and_financial_actions_cannot_bypass_approval(self) -> None:
        portfolio = self.create_portfolio("HTTP Gates")
        venture = self.create_venture("HTTP Gate Venture")
        registered = self.client.post(
            f"/portfolios/{portfolio['id']}/ventures",
            headers=HEADERS,
            json={"venture_id": venture["id"]},
        )
        assert registered.status_code == 200, registered.text
        refused = self.client.post(
            f"/portfolios/{portfolio['id']}/ventures/{venture['id']}/lifecycle",
            headers=HEADERS,
            json={"target": "CUSTOMER_ACQUISITION", "reason": "skip gates", "approved": True},
        )
        assert refused.status_code == 409
        pause = self.client.post(
            f"/portfolios/{portfolio['id']}/ventures/{venture['id']}/lifecycle",
            headers=HEADERS,
            json={"target": "PAUSED", "reason": "review", "approved": False},
        )
        assert pause.status_code == 409
        status = self.client.get("/portfolio/status", headers=HEADERS)
        assert status.status_code == 200
        assert status.json()["boundaries"]["funds_transfer"] is False

    def test_http_evaluation_risk_alert_and_report_workflow(self) -> None:
        portfolio = self.create_portfolio("HTTP Intelligence")
        venture = self.create_venture("HTTP Intelligence Venture")
        self.client.post(
            f"/portfolios/{portfolio['id']}/ventures",
            headers=HEADERS,
            json={"venture_id": venture["id"]},
        )
        ranking = self.client.get(f"/portfolios/{portfolio['id']}/ranking", headers=HEADERS)
        assert ranking.status_code == 200
        assert ranking.json()["ranking"]
        assert ranking.json()["ranking"][0]["eligible_for_ranking"] is False
        risks = self.client.post(f"/portfolios/{portfolio['id']}/risks/assess", headers=HEADERS)
        assert risks.status_code == 200
        alerts = self.client.post(f"/portfolios/{portfolio['id']}/alerts/refresh", headers=HEADERS)
        assert alerts.status_code == 200
        report = self.client.post(
            f"/portfolios/{portfolio['id']}/reports",
            headers=HEADERS,
            json={"period": "monthly"},
        )
        assert report.status_code == 200, report.text
        body = report.json()
        assert len(body["sections"]) == 12
        dashboard = self.client.get(f"/portfolios/{portfolio['id']}/dashboard", headers=HEADERS)
        assert dashboard.status_code == 200
        assert dashboard.json()["overview"]["total_ventures"] == 1

    def test_http_error_mapping_and_custom_report_validation(self) -> None:
        assert self.client.get("/portfolios/pf-missing", headers=HEADERS).status_code == 404
        portfolio = self.create_portfolio("HTTP Errors")
        invalid = self.client.post(
            f"/portfolios/{portfolio['id']}/reports",
            headers=HEADERS,
            json={"period": "custom"},
        )
        assert invalid.status_code == 422
        assert "period_start" in invalid.json()["detail"]
        invalid_rule = self.client.post(
            f"/portfolios/{portfolio['id']}/knowledge",
            headers=HEADERS,
            json={
                "kind": "verified_fact",
                "title": "No evidence",
                "body": "Unsupported claim.",
                "evidence": [],
            },
        )
        assert invalid_rule.status_code == 422
        assert "evidence" in invalid_rule.json()["detail"]

    def test_scenario_is_labelled_simulated_and_never_applied(self) -> None:
        portfolio = self.create_portfolio("HTTP Scenario")
        venture = self.create_venture("HTTP Scenario Venture")
        self.client.post(
            f"/portfolios/{portfolio['id']}/ventures",
            headers=HEADERS,
            json={"venture_id": venture["id"]},
        )
        response = self.client.post(
            f"/portfolios/{portfolio['id']}/scenarios",
            headers=HEADERS,
            json={
                "type": "revenue_shortfall",
                "venture_id": venture["id"],
                "horizon_months": 6,
                "assumptions": {"shortfall_pct": 20},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "simulated"
        assert body["applied"] is False
        assert body["requires_human_authorization"] is True
