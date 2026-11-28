"""HTTP integration for the Phase 36 product pipeline."""

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
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.product.workspace import ProductWorkspace


def _detail(*, verdict: str = "validated", stage: str = "validated", conditions=()) -> dict[str, Any]:
    return {
        "id": "opp-api-1",
        "title": "API intake product",
        "description": "Product created through the HTTP surface.",
        "customer_problem": "A real recorded problem.",
        "proposed_solution": "A local workflow.",
        "target_audience": "Small teams",
        "industry": "Software",
        "category": "automation",
        "stage": stage,
        "report": {
            "verdict": verdict,
            "rationale": "Sourced evidence is recorded.",
            "evidence_gaps": list(conditions),
            "sections": [],
            "source_count": 2,
            "primary_count": 1,
            "stale_count": 0,
            "version": 2,
        },
        "score": {"weighted_score": 0.7, "evidence_coverage": 0.6},
        "evidence": [],
        "source_refs": [],
        "uncertainty": [],
        "tags": [],
    }


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class ProductApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()
        assert self._nova is not None
        self._nova.product_pipeline._opportunities = lambda opportunity_id: _detail()
        self._nova.product_pipeline._workspace = ProductWorkspace(
            self._tmp.name + "/product-api-workspace"
        )

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

    def _create(self) -> dict[str, Any]:
        response = self._client.post(
            "/product/projects",
            json={
                "opportunity_id": "opp-api-1",
                "name": "API Product",
                "environment": {
                    "language": "python",
                    "build_command": "python -m compileall src",
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_frontend_workspace_is_served_and_wired(self) -> None:
        page = self._client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("productPanel", page.text)
        self.assertIn("Product Development", page.text)
        self.assertIn("render-product.js", page.text)
        script = self._client.get("/static/js/render-product.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("renderProductProject", script.text)
        self.assertIn("setupProductWorkspace", script.text)
        self.assertIn("productPrdDependencies", page.text)
        self.assertIn("productPrdDependencies", script.text)
        self.assertIn("conditions/resolve", script.text)

    def test_status_list_create_detail_and_history(self) -> None:
        status = self._client.get("/product/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["boundaries"]["external_launch_executed"])
        assert self._nova is not None
        self.assertEqual(self._nova.agent_registry.get("product-pipeline-agent").role.value, "project_manager")
        self.assertTrue(status.json()["configured"]["opportunity_loader"])
        self.assertTrue(status.json()["configured"]["agent_registry"])
        created = self._create()
        listing = self._client.get("/product/projects").json()
        self.assertEqual(listing["total"], 1)
        detail = self._client.get(f"/product/projects/{created['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["opportunity_id"], "opp-api-1")
        history = self._client.get(f"/product/projects/{created['id']}/history")
        self.assertEqual(history.status_code, 200)
        self.assertTrue(history.json()["events"])
        duplicate = self._client.post(
            "/product/projects", json={"opportunity_id": "opp-api-1"}
        )
        self.assertEqual(duplicate.status_code, 409)
        missing = self._client.get("/product/projects/missing")
        self.assertEqual(missing.status_code, 404)

    def test_approval_endpoints_require_a_note_and_correct_bools(self) -> None:
        created = self._create()
        product_id = created["id"]
        prd = self._client.post(
            f"/product/projects/{product_id}/prd",
            json={
                "vision": "A tested product",
                "functional_requirements": ["Do the work"],
            },
        )
        self.assertEqual(prd.status_code, 200, prd.text)
        no_note = self._client.post(
            f"/product/projects/{product_id}/prd/approval",
            json={"approved": True, "note": ""},
        )
        self.assertEqual(no_note.status_code, 422)
        approved = self._client.post(
            f"/product/projects/{product_id}/prd/approval",
            json={"approved": True, "note": "Reviewed by operator."},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        architecture = self._client.post(
            f"/product/projects/{product_id}/architecture", json={}
        )
        self.assertEqual(architecture.status_code, 200, architecture.text)
        bad_bool = self._client.post(
            f"/product/projects/{product_id}/architecture/approval",
            json={"approved": "yes", "note": "Reviewed."},
        )
        self.assertEqual(bad_bool.status_code, 422)
        bad_approval = self._client.post(
            f"/product/projects/{product_id}/architecture/approval",
            json={"approved": True, "note": "Reviewed."},
        )
        self.assertEqual(bad_approval.status_code, 200, bad_approval.text)
        plan = self._client.post(f"/product/projects/{product_id}/plan", json={})
        self.assertEqual(plan.status_code, 200, plan.text)
        started = self._client.post(
            f"/product/projects/{product_id}/development/start"
        )
        self.assertEqual(started.status_code, 200, started.text)
        cancelled = self._client.post(
            f"/product/projects/{product_id}/cancel", json={"reason": "Stop."}
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.json()["cancelled"])

    def test_conditional_opportunity_requires_approval_payload(self) -> None:
        assert self._nova is not None
        self._nova.product_pipeline._opportunities = lambda opportunity_id: _detail(
            verdict="conditionally_validated",
            stage="conditionally_validated",
            conditions=["One interview remains."],
        )
        response = self._client.post(
            "/product/projects", json={"opportunity_id": "opp-api-1"}
        )
        self.assertEqual(response.status_code, 422)
        response = self._client.post(
            "/product/projects",
            json={
                "opportunity_id": "opp-api-1",
                "approved": True,
                "approval_note": "Proceed while the interview is scheduled.",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["conditions"], ["One interview remains."])

    def test_malformed_payloads_are_rejected(self) -> None:
        response = self._client.post(
            "/product/projects", json={"opportunity_id": "opp-api-1", "approved": "yes"}
        )
        self.assertEqual(response.status_code, 422)
        response = self._client.post(
            "/product/projects",
            json={"opportunity_id": "opp-api-1", "environment": "bad"},
        )
        self.assertEqual(response.status_code, 422)
        created = self._create()
        response = self._client.post(
            f"/product/projects/{created['id']}/workspace/files",
            json={"path": "../escape.py", "content": "x = 1"},
        )
        self.assertEqual(response.status_code, 422)
        response = self._client.post(
            f"/product/projects/{created['id']}/workspace/files",
            json={"path": "src/app.py", "content": "x = 1", "overwrite": "yes"},
        )
        self.assertEqual(response.status_code, 422)
        response = self._client.post(
            f"/product/projects/{created['id']}/build",
            json={
                "command": "python -m compileall src",
                "exit_code": 1,
                "validated": True,
            },
        )
        self.assertEqual(response.status_code, 422)


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class ProductApiAuthTests(unittest.TestCase):
    def test_product_routes_require_the_configured_bearer_token(self) -> None:
        with TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"NOVACONTROL_API_TOKEN": "product-secret"}
        ), mock.patch(
            "novacontrol.api.app.NovaControlApplication",
            side_effect=lambda **kwargs: NovaControlApplication(data_dir=tmp),
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            with TestClient(create_app()) as client:
                unauthorized = client.get("/product/status")
                self.assertEqual(unauthorized.status_code, 401)
                authorized = client.get(
                    "/product/status",
                    headers={"Authorization": "Bearer product-secret"},
                )
                self.assertEqual(authorized.status_code, 200, authorized.text)
