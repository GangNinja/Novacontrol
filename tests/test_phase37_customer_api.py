"""HTTP integration for the Phase 37 Customer & Growth surface."""

from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner


def product_detail(product_id: str = "prod-1", *, state: str = "APPROVED_FOR_LAUNCH", approved: bool = True) -> dict[str, Any]:
    return {
        "summary": {"name": f"Product {product_id}", "state": state},
        "launch_approval": {"approved": approved},
        "prd": {
            "status": "approved",
            "version": "1.0",
            "vision": "Draft invoice reminders locally.",
            "problem_statement": "Small agencies forget follow-ups.",
            "target_users": ["Small agencies"],
            "core_features": ["Draft a reminder"],
            "explicit_exclusions": ["No payments"],
            "missing_information": [],
        },
        "architecture": {"status": "approved"},
        "releases": [{"version": "1.0.0"}],
        "opportunity_snapshot": {"financial": {"price": 29, "currency": "USD"}},
    }


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class CustomerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=self._isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()
        assert self._nova is not None
        self._nova.customer_growth._products = lambda product_id: product_detail(product_id)
        self._nova.customer_growth._sender = None

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    def _workspace(self, product_id: str = "prod-1") -> dict[str, Any]:
        response = self._client.post("/customer/workspaces", json={"product_id": product_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _customer(self, product_id: str = "prod-1", name: str = "Acme") -> dict[str, Any]:
        response = self._client.post(
            f"/customer/workspaces/{product_id}/customers",
            json={
                "name": name,
                "contact": {"email": "buyer@acme.example"},
                "consent": {"status": "granted", "channels": ["email"], "source": "web form"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_frontend_workspace_is_served_and_wired(self) -> None:
        page = self._client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("customerPanel", page.text)
        self.assertIn("Customer &amp; Growth", page.text)
        self.assertIn("render-customer.js", page.text)
        self.assertIn("customerTicketDetail", page.text)
        script = self._client.get("/static/js/render-customer.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("renderCustomerWorkspace", script.text)
        self.assertIn("setupCustomerWorkspace", script.text)
        self.assertIn("customerCustomerActions", script.text)
        self.assertIn("Add message", script.text)
        self.assertEqual(ROUTE_CONSUMERS[("GET", "/customer/workspaces")], "renderCustomerGrowth")

    def test_workspace_requires_launch_authorization(self) -> None:
        self._nova.customer_growth._products = lambda product_id: product_detail(
            product_id, state="READY_FOR_RELEASE_REVIEW", approved=False
        )
        response = self._client.post("/customer/workspaces", json={"product_id": "prod-blocked"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("not approved for launch", response.text)
        status = self._client.get("/customer/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["service"], "customer_growth")
        self.assertFalse(status.json()["boundaries"]["paid_advertising"])
        self.assertFalse(status.json()["configured"]["communication_sender"])
        application_status = self._client.get("/status")
        self.assertEqual(application_status.status_code, 200)
        self.assertIn("customer_growth", application_status.json()["app"])

    def test_end_to_end_lifecycle_through_http(self) -> None:
        workspace = self._workspace()
        self.assertTrue(workspace["summary"]["launch_authorized"])
        segment = self._client.post(
            "/customer/workspaces/prod-1/segments",
            json={"name": "Small agencies", "category": "business", "needs": ["speed"]},
        )
        self.assertEqual(segment.status_code, 200, segment.text)
        customer = self._customer()
        lead = self._client.post(
            "/customer/workspaces/prod-1/leads",
            json={
                "name": "Beta Studio",
                "source": "website_inquiry",
                "contact": {"email": "owner@beta.example"},
                "consent": {"status": "granted", "channels": ["email"]},
                "qualification_inputs": {"product_fit": 4, "problem_relevance": 4},
            },
        )
        self.assertEqual(lead.status_code, 200, lead.text)
        lead_id = lead.json()["id"]
        qualification = self._client.post(f"/customer/workspaces/prod-1/leads/{lead_id}/qualify", json={})
        self.assertEqual(qualification.status_code, 200, qualification.text)
        self.assertEqual(qualification.json()["recommended_status"], "QUALIFIED")
        self.assertFalse(qualification.json()["purchase_intent_verified"])
        converted = self._client.post(f"/customer/workspaces/prod-1/leads/{lead_id}/convert", json={})
        self.assertEqual(converted.status_code, 200, converted.text)

        deal = self._client.post(
            "/customer/workspaces/prod-1/deals",
            json={"customer_id": customer["id"], "value": 500, "currency": "USD", "probability": 0.5},
        )
        self.assertEqual(deal.status_code, 200, deal.text)
        deal_id = deal.json()["id"]
        won = self._client.post(
            f"/customer/workspaces/prod-1/deals/{deal_id}/stage",
            json={"stage": "closed_won", "realized_amount": 450},
        )
        self.assertEqual(won.status_code, 200, won.text)
        self.assertEqual(won.json()["realized_amount"], 450)

        draft = self._client.post(
            "/customer/workspaces/prod-1/communications",
            json={"related_type": "customer", "related_id": customer["id"], "channel": "email", "subject": "Welcome", "body": "Hello"},
        )
        self.assertEqual(draft.status_code, 200, draft.text)
        communication_id = draft.json()["id"]
        approval = self._client.post(
            f"/customer/workspaces/prod-1/communications/{communication_id}/approval",
            json={"approved": True, "note": "consent checked"},
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        send = self._client.post(f"/customer/workspaces/prod-1/communications/{communication_id}/send")
        self.assertEqual(send.status_code, 503, send.text)
        detail = self._client.get("/customer/workspaces/prod-1").json()
        self.assertEqual(detail["communications"][0]["status"], "not_configured")

        knowledge = self._client.post(
            "/customer/workspaces/prod-1/knowledge",
            json={
                "title": "Configure reminders",
                "body": "Open settings and choose reminder timing.",
                "source": "release 1.0.0",
                "approved": True,
                "verified": True,
                "steps": ["Open settings"],
            },
        )
        self.assertEqual(knowledge.status_code, 200, knowledge.text)
        ticket = self._client.post(
            "/customer/workspaces/prod-1/tickets",
            json={"customer_id": customer["id"], "subject": "Configure reminders", "description": "How do I configure reminders?"},
        )
        self.assertEqual(ticket.status_code, 200, ticket.text)
        ticket_id = ticket.json()["id"]
        assist = self._client.post(f"/customer/workspaces/prod-1/tickets/{ticket_id}/assist")
        self.assertEqual(assist.status_code, 200, assist.text)
        self.assertTrue(assist.json()["grounded"])
        self.assertIn("Open settings", assist.json()["troubleshooting_steps"])
        resolved = self._client.post(
            f"/customer/workspaces/prod-1/tickets/{ticket_id}/status",
            json={"status": "RESOLVED", "resolution": "Sent approved steps"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)

        brief = self._client.post(
            "/customer/workspaces/prod-1/sales/assist",
            json={"deal_id": deal_id},
        )
        self.assertEqual(brief.status_code, 200, brief.text)
        self.assertFalse(brief.json()["proposal_draft"]["authorized_contract_terms"])
        self.assertTrue(brief.json()["proposal_draft"]["draft_only"])

        analytics = self._client.get("/customer/workspaces/prod-1/analytics")
        self.assertEqual(analytics.status_code, 200, analytics.text)
        body = analytics.json()
        self.assertEqual(body["sales"]["realized_revenue"]["value"], 450)
        self.assertFalse(body["acquisition"]["acquisition_cost"]["available"])
        self.assertFalse(body["support"]["customer_satisfaction"]["available"])
        self.assertEqual(body["support"]["open_tickets"], 0)

    def test_malformed_payloads_and_missing_records_are_rejected(self) -> None:
        self._workspace()
        bad_category = self._client.post(
            "/customer/workspaces/prod-1/customers",
            json={"name": "Acme", "category": "alien"},
        )
        self.assertEqual(bad_category.status_code, 422, bad_category.text)
        missing_approval = self._client.post(
            "/customer/workspaces/prod-1/communications/missing/approval",
            json={"approved": True, "note": "x"},
        )
        self.assertEqual(missing_approval.status_code, 404, missing_approval.text)
        wrong_bool = self._client.post(
            "/customer/workspaces/prod-1/customers",
            json={"name": "Acme", "category": "business", "verified": "yes"},
        )
        self.assertEqual(wrong_bool.status_code, 422, wrong_bool.text)
        missing = self._client.get("/customer/workspaces/missing")
        self.assertEqual(missing.status_code, 404, missing.text)
        no_consent = self._client.post(
            "/customer/workspaces/prod-1/leads",
            json={"name": "Imported", "source": "manual_import"},
        )
        self.assertEqual(no_consent.status_code, 422, no_consent.text)

    def test_customer_consent_can_be_withdrawn_and_restored_over_http(self) -> None:
        self._workspace()
        customer = self._customer()
        withdrawn = self._client.post(
            f"/customer/workspaces/prod-1/customers/{customer['id']}/consent",
            json={"status": "withdrawn", "source": "recorded opt-out"},
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        self.assertEqual(withdrawn.json()["consent"]["status"], "withdrawn")

        missing_source = self._client.post(
            f"/customer/workspaces/prod-1/customers/{customer['id']}/consent",
            json={"status": "granted"},
        )
        self.assertEqual(missing_source.status_code, 422, missing_source.text)

        restored = self._client.post(
            f"/customer/workspaces/prod-1/customers/{customer['id']}/consent",
            json={"status": "granted", "channels": ["email"], "source": "signed consent form"},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["consent"]["status"], "granted")
        self.assertEqual(restored.json()["consent"]["channels"], ["email"])

        detail = self._client.get("/customer/workspaces/prod-1")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["customers"][0]["consent"]["status"], "granted")

    def test_customer_routes_require_the_configured_api_token(self) -> None:
        with mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": "phase37-test-token"}):
            client = TestClient(create_app())
            client.__enter__()
            try:
                unauthorized = client.get("/customer/status")
                self.assertEqual(unauthorized.status_code, 401, unauthorized.text)
                forbidden = client.post("/customer/workspaces", json={"product_id": "prod-1"}, headers={"Authorization": "Bearer wrong"})
                self.assertEqual(forbidden.status_code, 401, forbidden.text)
                authorized = client.get("/customer/status", headers={"Authorization": "Bearer phase37-test-token"})
                self.assertEqual(authorized.status_code, 200, authorized.text)
            finally:
                client.__exit__(None, None, None)

    def test_workspaces_isolate_product_customer_data(self) -> None:
        self._workspace("prod-1")
        self._workspace("prod-2")
        customer = self._customer("prod-1")
        first = self._client.get("/customer/workspaces/prod-1/customers").json()
        second = self._client.get("/customer/workspaces/prod-2/customers").json()
        self.assertEqual([row["id"] for row in first], [customer["id"]])
        self.assertEqual(second, [])
        cross = self._client.get(f"/customer/workspaces/prod-2/customers/{customer['id']}")
        self.assertEqual(cross.status_code, 404, cross.text)
        listing = self._client.get("/customer/workspaces").json()
        self.assertEqual(listing["total"], 2)


if __name__ == "__main__":
    unittest.main()
