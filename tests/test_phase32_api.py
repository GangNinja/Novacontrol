"""Phase 32 HTTP surface: acquisition, retrieval, inspection, correction, export, deletion."""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx is optional in some installations
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class PersonalKnowledgeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
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

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: Any) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    def test_full_user_control_workflow(self) -> None:
        status = self._client.get("/memory/personal/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["records"], 0)

        created = self._client.post(
            "/memory/personal",
            json={
                "content": "I prefer concise weekly project updates",
                "category": "preference",
                "kind": "preference",
                "predicate": "communication_style",
                "source": "test:api",
                "project": "atlas",
                "entities": ["NovaControl"],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertTrue(body["ok"])
        record_id = body["record"]["id"]
        self.assertEqual(body["record"]["source"], "test:api")

        search = self._client.get(
            "/memory/personal",
            params={"query": "weekly project updates", "project": "atlas"},
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual(search.json()["count"], 1)
        self.assertEqual(search.json()["hits"][0]["record"]["id"], record_id)

        inspected = self._client.get(f"/memory/personal/{record_id}")
        self.assertEqual(inspected.status_code, 200, inspected.text)
        self.assertTrue(inspected.json()["found"])
        self.assertEqual(inspected.json()["record"]["version"], 1)

        corrected = self._client.post(
            f"/memory/personal/{record_id}/correct",
            json={
                "content": "I prefer concise monthly project updates",
                "reason": "the user changed the cadence",
            },
        )
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["record"]["version"], 2)
        self.assertEqual(
            corrected.json()["record"]["content"],
            "I prefer concise monthly project updates",
        )
        self.assertEqual(len(corrected.json()["record"]["history"]), 1)

        exported = self._client.get("/memory/personal/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertIn(record_id, {row["id"] for row in exported.json()["records"]})

        deleted = self._client.post(f"/memory/personal/{record_id}/delete", json={})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["deleted"])
        missing = self._client.get(f"/memory/personal/{record_id}")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_invalid_acquisition_and_unknown_record_are_explicit(self) -> None:
        blank = self._client.post("/memory/personal", json={"content": "   "})
        self.assertEqual(blank.status_code, 422, blank.text)
        missing = self._client.get("/memory/personal/not-a-record")
        self.assertEqual(missing.status_code, 404, missing.text)
        bad_delete = self._client.post("/memory/personal/not-a-record/delete", json={})
        self.assertEqual(bad_delete.status_code, 404, bad_delete.text)

    def test_sensitive_records_are_not_returned_by_default(self) -> None:
        created = self._client.post(
            "/memory/personal",
            json={
                "content": "A private medical note",
                "category": "fact",
                "privacy": "sensitive",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        record_id = created.json()["record"]["id"]
        default = self._client.get(
            "/memory/personal", params={"query": "medical note"}
        )
        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual(default.json()["count"], 0)
        allowed = self._client.get(
            "/memory/personal",
            params={"query": "medical note", "include_sensitive": True},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["count"], 1)
        self.assertEqual(allowed.json()["hits"][0]["record"]["id"], record_id)

        inspected = self._client.get(f"/memory/personal/{record_id}")
        self.assertEqual(inspected.status_code, 200, inspected.text)
        corrected = self._client.post(
            f"/memory/personal/{record_id}/correct",
            json={"content": "A corrected private medical note", "reason": "controlled"},
        )
        self.assertEqual(corrected.status_code, 200, corrected.text)
        deleted = self._client.post(
            f"/memory/personal/{record_id}/delete", json={}
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_conflict_is_409_and_withdrawing_one_side_restores_the_other(self) -> None:
        first = self._client.post(
            "/memory/personal",
            json={
                "content": "The deployment window is Tuesday",
                "subject": "deployment",
                "predicate": "window",
                "category": "decision",
            },
        )
        self.assertEqual(first.status_code, 200, first.text)
        existing_id = first.json()["record"]["id"]
        second = self._client.post(
            "/memory/personal",
            json={
                "content": "The deployment window is Thursday",
                "subject": "deployment",
                "predicate": "window",
                "category": "decision",
            },
        )
        self.assertEqual(second.status_code, 409, second.text)
        detail = second.json()["detail"]
        incoming_id = detail["result"]["record"]["id"]
        self.assertEqual(detail["result"]["action"], "conflict")
        withdrawn = self._client.post(
            f"/memory/personal/{incoming_id}/resolve",
            json={"keep": False, "reason": "Thursday was a mistake"},
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        inspected = self._client.get(f"/memory/personal/{existing_id}")
        self.assertEqual(inspected.status_code, 200, inspected.text)
        self.assertEqual(inspected.json()["record"]["status"], "current")

    def test_search_filters_and_invalid_fields_are_explicit(self) -> None:
        preference = self._client.post(
            "/memory/personal",
            json={
                "content": "I prefer concise updates for NovaControl",
                "category": "preference",
                "kind": "preference",
                "subject": "user",
                "predicate": "communication_style",
                "entities": ["NovaControl"],
            },
        )
        self.assertEqual(preference.status_code, 200, preference.text)
        decision = self._client.post(
            "/memory/personal",
            json={
                "content": "Project Atlas uses SQLite",
                "category": "decision",
                "kind": "decision",
                "subject": "Project Atlas",
                "predicate": "database",
                "entities": ["Project Atlas"],
            },
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        filtered = self._client.get(
            "/memory/personal",
            params={
                "query": "concise",
                "entity": "NovaControl",
                "category": "preference",
                "kind": "preference",
            },
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["count"], 1)
        self.assertEqual(
            filtered.json()["hits"][0]["record"]["id"],
            preference.json()["record"]["id"],
        )

        for bad in (
            {"content": "bad confidence", "confidence": "not-a-number"},
            {"content": "bad metadata", "metadata": ["not", "a", "map"]},
            {"content": "bad ttl", "ttl_seconds": "not-a-number"},
            {"content": "bad date", "valid_until": "not-a-date"},
        ):
            response = self._client.post("/memory/personal", json=bad)
            self.assertEqual(response.status_code, 422, (bad, response.text))

        naive = self._client.post(
            "/memory/personal",
            json={"content": "A future valid fact", "valid_until": "2100-01-01"},
        )
        self.assertEqual(naive.status_code, 200, naive.text)
        self.assertEqual(naive.json()["record"]["status"], "current")


if __name__ == "__main__":
    unittest.main()
