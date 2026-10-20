"""HTTP surface for NovaForge NF-01, driven through the real create_app().

The routes are exercised against the REAL session manager, with only its
desktop seams replaced (a fake window + a capture that writes a real PNG), so
what is tested is the route -> manager -> state-machine path the UI uses. The
``TestClient`` fixture is the same one the rest of the web API suite uses.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from typing import Any

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase
from test_novaforge import FakeDesktop, _mission

from novacontrol.novaforge import NovaForgeManager


class _ForgeApiFixture(_IsolatedApiTestCase):
    """The real application with only NovaForge's desktop seams replaced."""

    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        self.desktop = FakeDesktop()
        self.manager = NovaForgeManager(
            data_dir=Path(self._tmp.name),
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            permissions=app.risk,
            publish=app._novaforge_publish,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=2.0,
            stop_timeout=4.0,
        )
        app.novaforge = self.manager
        return app

    # -- helpers -----------------------------------------------------------

    def _start(self, **overrides: Any) -> dict[str, Any]:
        response = self._client.post("/novaforge/sessions", json=_mission(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait(self, predicate, *, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition was not reached in time")


class NovaForgeApiTests(_ForgeApiFixture):
    """status/windows/mission/validate/sessions/frames over HTTP."""

    def test_status_reports_readiness_and_no_session(self) -> None:
        body = self._client.get("/novaforge/status").json()
        self.assertTrue(body["status"]["capture_supported"])
        self.assertIsNone(body["status"]["active_session"])
        self.assertIsNone(body["active"])
        self.assertTrue(body["status"]["limitations"])

    def test_status_states_what_the_action_path_cannot_do(self) -> None:
        """The honest limitations are on the wire, not only in the docs.

        They describe the build that SHIPS: no observe-only claim (input exists
        now) and no claim that a budget is unenforceable (it is enforced at
        execution), while the real bounds — the key policy, the launch
        prohibition and the one checkable objective kind — stay stated.
        """
        body = self._client.get("/novaforge/status").json()
        joined = " ".join(body["status"]["limitations"]).lower()
        self.assertIn("declared gameplay keys", joined)
        self.assertIn("never launches", joined)
        self.assertIn("checkable objective", joined)
        self.assertNotIn("observes only", joined)
        self.assertNotIn("cannot be enforced", joined)

    def test_windows_lists_the_open_targets_once(self) -> None:
        body = self._client.get("/novaforge/windows").json()
        self.assertTrue(body["available"])
        self.assertEqual([row["title"] for row in body["windows"]], ["NC FORGE TEST - disposable"])

    def test_validate_rejects_an_unknown_target(self) -> None:
        response = self._client.post(
            "/novaforge/validate", json=_mission(target_title="No Such Window")
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["valid"])
        self.assertIn("No open window matches", body["errors"][0])
        self.assertIsNone(body["target"])

    def test_validate_reports_the_measured_window(self) -> None:
        body = self._client.post("/novaforge/validate", json=_mission()).json()
        self.assertTrue(body["valid"], body["errors"])
        self.assertEqual(body["target"]["width"], 1000)
        self.assertEqual(body["target"]["handle"], 0x1234)

    def test_mission_draft_round_trips(self) -> None:
        saved = self._client.post("/novaforge/mission", json=_mission(name="Draft One")).json()
        self.assertTrue(saved["valid"])
        read = self._client.get("/novaforge/mission").json()
        self.assertEqual(read["name"], "Draft One")

    def test_invalid_mission_is_refused_with_422_and_creates_nothing(self) -> None:
        response = self._client.post("/novaforge/sessions", json=_mission(name="", target_title=""))
        self.assertEqual(response.status_code, 422)
        self.assertIn("Mission name is required", response.json()["detail"])
        self.assertEqual(self._client.get("/novaforge/sessions").json()["count"], 0)

    def test_start_refuses_a_replaced_window_when_the_pin_does_not_match(self) -> None:
        validation = self._client.post("/novaforge/validate", json=_mission()).json()
        self.assertTrue(validation["valid"])
        response = self._client.post(
            "/novaforge/sessions",
            json={**_mission(), "expected_handle": validation["target"]["handle"] + 1},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("changed since it was validated", response.json()["detail"])
        self.assertEqual(self._client.get("/novaforge/sessions").json()["count"], 0)

    def test_start_conflict_is_409(self) -> None:
        session = self._start()
        response = self._client.post("/novaforge/sessions", json=_mission(name="Second"))
        self.assertEqual(response.status_code, 409)
        self.assertIn(session["session_id"], response.json()["detail"])
        self._client.post(f"/novaforge/sessions/{session['session_id']}/stop")

    def test_lifecycle_pause_resume_stop_through_the_api(self) -> None:
        session = self._start()
        session_id = session["session_id"]
        self.assertEqual(session["state"], "RUNNING")
        self.assertTrue(session["can"]["pause"])
        self._wait(lambda: self.manager.get(session_id).frames)

        paused = self._client.post(f"/novaforge/sessions/{session_id}/pause")
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["state"], "PAUSED")
        self.assertTrue(paused.json()["can"]["resume"])

        resumed = self._client.post(f"/novaforge/sessions/{session_id}/resume")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["state"], "RUNNING")

        stopped = self._client.post(f"/novaforge/sessions/{session_id}/stop")
        self.assertEqual(stopped.status_code, 200)
        body = stopped.json()
        self.assertEqual(body["state"], "STOPPED")
        self.assertTrue(body["input_cleanup"]["ok"])
        self.assertEqual(body["verification"]["status"], "unverified")
        self.assertFalse(body["verification"]["supported"])
        self.assertEqual(body["stages"][0]["stage"], "observed")
        self.assertFalse(body["stages"][1]["available"])
        self.assertFalse(body["stages"][3]["available"])
        self.assertIn("telemetry", body)
        self.assertIn("guardian", body)
        self.assertFalse(body["guardian"]["limits"]["action_budget"]["enforced"])
        self.assertEqual(body["guardian"]["cleanup"]["state"], "ok")

    def test_pause_after_stop_is_409(self) -> None:
        session = self._start()
        self._client.post(f"/novaforge/sessions/{session['session_id']}/stop")
        response = self._client.post(f"/novaforge/sessions/{session['session_id']}/pause")
        self.assertEqual(response.status_code, 409)
        self.assertIn("STOPPED", response.json()["detail"])

    def test_unknown_session_is_404_everywhere(self) -> None:
        self.assertEqual(self._client.get("/novaforge/sessions/nope").status_code, 404)
        for action in ("pause", "resume", "stop", "cancel", "emergency-stop"):
            response = self._client.post(f"/novaforge/sessions/nope/{action}")
            self.assertEqual(response.status_code, 404, action)
        self.assertEqual(self._client.get("/novaforge/frames/nope").status_code, 404)

    def test_frame_route_serves_png_with_no_cache_headers(self) -> None:
        session = self._start()
        session_id = session["session_id"]
        self._wait(lambda: self.manager.get(session_id).frames)
        frame = self.manager.get(session_id).frames[-1]

        response = self._client.get(f"/novaforge/frames/{session_id}?seq={frame.seq}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertTrue(response.content.startswith(b"\x89PNG"))

        newest = self._client.get(f"/novaforge/frames/{session_id}")
        self.assertEqual(newest.status_code, 200)
        self._wait(lambda: self.manager.get(session_id).frames[-1].seq > frame.seq)
        self.assertEqual(
            self._client.get(f"/novaforge/frames/{session_id}?seq=9999").status_code, 404
        )
        self._client.post(f"/novaforge/sessions/{session_id}/stop")

    def test_history_survives_stop_and_is_bounded(self) -> None:
        first = self._start()
        self._client.post(f"/novaforge/sessions/{first['session_id']}/stop")
        response = self._client.get("/novaforge/sessions?limit=1")
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["sessions"][0]["session_id"], first["session_id"])
        self.assertEqual(body["sessions"][0]["state"], "STOPPED")
        # A bounded row keeps enough to inspect without shipping every frame.
        self.assertLessEqual(len(body["sessions"][0]["frames"]), 1)
        self.assertLessEqual(len(body["sessions"][0]["history"]), 6)
        # ...and without shipping the raw persisted snapshot (latency window).
        self.assertNotIn("telemetry", body["sessions"][0])
        self.assertNotIn("latency_window_ms", response.text)

    def test_telemetry_route_reports_measured_capture_and_guardian_state(self) -> None:
        session = self._start()
        session_id = session["session_id"]
        self._wait(lambda: len(self.manager.get(session_id).frames) >= 2)
        body = self._client.get(f"/novaforge/sessions/{session_id}/telemetry").json()
        self.assertEqual(body["session_id"], session_id)
        telemetry = body["telemetry"]
        self.assertGreaterEqual(telemetry["capture"]["frames"], 2)
        self.assertGreaterEqual(telemetry["capture"]["latency_ms"]["samples"], 2)
        self.assertIsNotNone(telemetry["capture"]["latency_ms"]["median_ms"])
        self.assertFalse(telemetry["actions"]["available"])
        self.assertIsNone(telemetry["actions"]["executed"])
        self.assertFalse(telemetry["resources"]["gpu"]["available"])
        self.assertTrue(telemetry["resources"]["gpu"]["reason"])
        self.assertGreaterEqual(telemetry["target"]["checks"], 1)
        self.assertTrue(telemetry["measurement"]["notes"])
        self.assertIsInstance(telemetry["session"]["errors"], int)
        self.assertFalse(telemetry["recovery"]["available"])
        self.assertIn("No retry or recovery path", telemetry["recovery"]["reason"])

        guardian = body["guardian"]
        self.assertEqual(guardian["safety_state"], "observing")
        self.assertTrue(guardian["target"]["valid"])
        self.assertTrue(guardian["target"]["validated"], "a tick confirmed this run's target")
        self.assertTrue(guardian["controls"]["emergency_stop"]["available"])
        self.assertFalse(guardian["limits"]["action_budget"]["enforced"])
        self.assertIn("input_path_available", guardian)
        self._client.post(f"/novaforge/sessions/{session_id}/stop")

    def test_telemetry_route_for_an_unknown_session_is_404(self) -> None:
        response = self._client.get("/novaforge/sessions/nope/telemetry")
        self.assertEqual(response.status_code, 404)

    def test_emergency_stop_is_confirmed_by_the_backend_and_repeatable(self) -> None:
        session = self._start()
        session_id = session["session_id"]
        self._wait(lambda: self.manager.get(session_id).frames)

        response = self._client.post(f"/novaforge/sessions/{session_id}/emergency-stop")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["state"], "STOPPED")
        self.assertEqual(body["end_reason"], "emergency_stop")
        self.assertFalse(body["already_stopped"])
        self.assertIsNotNone(body["stop_latency_ms"])
        self.assertTrue(body["input_cleanup"]["ok"])
        self.assertEqual(body["guardian"]["safety_state"], "closed")
        self.assertFalse(body["guardian"]["controls"]["emergency_stop"]["available"])
        self.assertTrue(
            any(entry["kind"] == "emergency_stop" for entry in body["guardian"]["interventions"])
        )

        again = self._client.post(f"/novaforge/sessions/{session_id}/emergency-stop")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["already_stopped"])
        self.assertEqual(self.desktop.release_calls, 1)

    def test_cancel_endpoint_is_idempotent_and_releases_once(self) -> None:
        session = self._start()
        session_id = session["session_id"]
        self._wait(lambda: self.manager.get(session_id).frames)
        first = self._client.post(f"/novaforge/sessions/{session_id}/cancel")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["state"], "STOPPED")
        second = self._client.post(f"/novaforge/sessions/{session_id}/cancel")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["already_stopped"])
        self.assertEqual(self.desktop.release_calls, 1)

    def test_new_routes_are_registered_as_authenticated(self) -> None:
        from novacontrol.api.models import ApiSurface

        routes = {(route.method, route.path): route for route in ApiSurface.default().routes}
        for method, path in (
            ("POST", "/novaforge/sessions/{session_id}/cancel"),
            ("POST", "/novaforge/sessions/{session_id}/emergency-stop"),
            ("GET", "/novaforge/sessions/{session_id}/telemetry"),
        ):
            self.assertIn((method, path), routes, f"unregistered route {method} {path}")
            self.assertTrue(routes[(method, path)].authenticated, f"unauthenticated {path}")

    def test_capture_failure_is_visible_and_not_faked(self) -> None:
        session = self._start()
        session_id = session["session_id"]
        self._wait(lambda: self.manager.get(session_id).frames)
        self.desktop.fail = True
        self._wait(lambda: any("Capture failed" in e["message"] for e in self.manager.get(session_id).errors))
        body = self._client.get(f"/novaforge/sessions/{session_id}").json()
        self.assertFalse(body["observations"][-1]["evidence"])
        self.assertTrue(body["errors"])
        self._client.post(f"/novaforge/sessions/{session_id}/stop")


class NovaForgeTimelineApiTests(_ForgeApiFixture):
    """NF-03 over HTTP: paginated read-only evidence and a two-session compare."""

    def _stopped(self, *, name: str = "Timeline Probe", frames: int = 2) -> str:
        session = self._start(name=name)
        session_id = session["session_id"]
        self._wait(lambda: len(self.manager.get(session_id).frames) >= frames)
        stopped = self._client.post(f"/novaforge/sessions/{session_id}/stop")
        self.assertEqual(stopped.status_code, 200, stopped.text)
        return session_id

    def test_timeline_pages_are_stable_and_marked_read_only(self) -> None:
        session_id = self._stopped()
        first = self._client.get(f"/novaforge/sessions/{session_id}/timeline?limit=2")
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(body["source"], "recorded")
        self.assertTrue(body["read_only"])
        for flag in ("can_start", "can_resume", "can_stop", "can_execute_action", "can_send_input"):
            self.assertFalse(body["replay"][flag], flag)
        sequences = [event["seq"] for event in body["events"]]
        self.assertEqual(sequences, sorted(sequences))
        self.assertTrue(body["page"]["has_more"])

        second = self._client.get(
            f"/novaforge/sessions/{session_id}/timeline?limit=2"
            f"&after_seq={body['page']['next_after_seq']}"
        ).json()
        second_sequences = [event["seq"] for event in second["events"]]
        self.assertFalse(set(sequences) & set(second_sequences), "no event may repeat across pages")
        self.assertEqual(second_sequences, sorted(second_sequences))
        self.assertTrue(all(seq > sequences[-1] for seq in second_sequences))
        # Classification is computed over the whole timeline, not the page.
        full = self._client.get(f"/novaforge/sessions/{session_id}/timeline?limit=500").json()
        self.assertEqual(body["classification"]["outcome"], full["classification"]["outcome"])
        self.assertEqual(full["page"]["count"], full["page"]["total"])

    def test_filters_keep_event_identity_and_bad_values_are_422(self) -> None:
        session_id = self._stopped()
        full = self._client.get(f"/novaforge/sessions/{session_id}/timeline?limit=500").json()
        filtered = self._client.get(
            f"/novaforge/sessions/{session_id}/timeline?categories=capture"
        ).json()
        self.assertTrue(filtered["events"])
        self.assertTrue(all(event["category"] == "capture" for event in filtered["events"]))
        identities = {event["seq"]: event["event_id"] for event in full["events"]}
        for event in filtered["events"]:
            self.assertEqual(identities[event["seq"]], event["event_id"])

        for query in ("categories=nonsense", "sources=nonsense", "severities=nonsense", "limit=0"):
            response = self._client.get(f"/novaforge/sessions/{session_id}/timeline?{query}")
            self.assertEqual(response.status_code, 422, query)
            self.assertIn("nonsense" if "nonsense" in query else "limit", response.json()["detail"])

    def test_a_frame_event_resolves_its_evidence_through_the_secured_frame_route(self) -> None:
        session_id = self._stopped()
        timeline = self._client.get(f"/novaforge/sessions/{session_id}/timeline?limit=500").json()
        frame_event = next(event for event in timeline["events"] if event["type"] == "frame.captured")
        detail = self._client.get(
            f"/novaforge/sessions/{session_id}/timeline/{frame_event['seq']}"
        ).json()["event"]
        self.assertEqual(detail["event_id"], frame_event["event_id"])
        reference = next(ref for ref in detail["evidence"] if ref["kind"] == "frame")
        self.assertTrue(reference["available"])
        self.assertIn(f"/novaforge/frames/{session_id}?seq={reference['seq']}", reference["url"])
        image = self._client.get(reference["url"])
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        # Bounded references, never a filesystem path from the record.
        self.assertNotIn(str(self.manager.frames_root), json.dumps(detail))
        self.assertNotIn("path", reference)

    def test_expired_frame_evidence_is_explained_and_the_event_stays_readable(self) -> None:
        self.manager.max_frames_on_disk = 1
        session_id = self._stopped(frames=4)
        timeline = self._client.get(f"/novaforge/sessions/{session_id}/timeline?limit=500").json()
        frame_events = [event for event in timeline["events"] if event["type"] == "frame.captured"]
        self.assertGreater(len(frame_events), 1)
        oldest = frame_events[0]
        expired = next(ref for ref in oldest["evidence"] if ref["kind"] == "frame")
        self.assertFalse(expired["available"])
        self.assertIn("no longer retained", expired["reason"])
        self.assertEqual(
            self._client.get(f"/novaforge/sessions/{session_id}/timeline/{oldest['seq']}").status_code,
            200,
        )

    def test_compare_reports_measured_figures_and_unmeasured_rows(self) -> None:
        left = self._stopped(name="Left run", frames=2)
        right = self._stopped(name="Right run", frames=4)
        response = self._client.get(f"/novaforge/timeline/compare?left={left}&right={right}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["read_only"])
        rows = {row["key"]: row for row in body["rows"]}
        self.assertEqual(rows["frames"]["left"], len(self.manager.get(left).frames))
        self.assertEqual(rows["frames"]["right"], len(self.manager.get(right).frames))
        self.assertIsNotNone(rows["frames"]["delta"])
        self.assertFalse(rows["actions_executed"]["available"])
        self.assertIsNone(rows["actions_executed"]["left"])
        self.assertTrue(rows["actions_executed"]["reason"])
        self.assertFalse(rows["verified"]["available"])
        # Every session here records fewer than the 20 p95 samples.
        self.assertIsNone(rows["latency_p95_ms"]["left"])
        self.assertTrue(rows["latency_p95_ms"]["reason"])
        self.assertTrue(body["missing"]["note"])
        unknown = self._client.get(f"/novaforge/timeline/compare?left={left}&right=nope")
        self.assertEqual(unknown.status_code, 404)

    def test_replay_routes_are_get_only_and_cannot_start_a_session(self) -> None:
        session_id = self._stopped()
        before = self.manager.get(session_id).state
        for path in (
            f"/novaforge/sessions/{session_id}/timeline",
            f"/novaforge/sessions/{session_id}/timeline/1",
            f"/novaforge/timeline/compare?left={session_id}&right={session_id}",
        ):
            response = self._client.post(path)
            self.assertEqual(response.status_code, 405, path)
        self.assertIs(self.manager.get(session_id).state, before)
        self.assertIsNone(self.manager.active())

    def test_unknown_and_traversal_session_ids_are_404(self) -> None:
        self.assertEqual(self._client.get("/novaforge/sessions/nope/timeline").status_code, 404)
        self.assertEqual(
            self._client.get("/novaforge/sessions/..%2F..%2Fetc/timeline").status_code, 404
        )
        session_id = self._stopped()
        self.assertEqual(
            self._client.get(f"/novaforge/sessions/{session_id}/timeline/9999").status_code, 404
        )
        self.assertEqual(
            self._client.get(f"/novaforge/sessions/{session_id}/timeline?limit=501").status_code, 422
        )

    def test_timeline_routes_are_registered_as_authenticated(self) -> None:
        from novacontrol.api.models import ApiSurface

        routes = {(route.method, route.path): route for route in ApiSurface.default().routes}
        for method, path in (
            ("GET", "/novaforge/sessions/{session_id}/timeline"),
            ("GET", "/novaforge/sessions/{session_id}/timeline/{seq}"),
            ("GET", "/novaforge/timeline/compare"),
        ):
            self.assertIn((method, path), routes, f"unregistered route {method} {path}")
            self.assertTrue(routes[(method, path)].authenticated, f"unauthenticated {path}")


class NovaForgeUnavailableApiTests(_IsolatedApiTestCase):
    """A runner with no capture seams must say so, not offer a broken Start."""

    def test_status_reports_capture_unavailable(self) -> None:
        body = self._client.get("/novaforge/status").json()
        self.assertFalse(body["status"]["capture_supported"])
        self.assertIsNone(body["active"])

    def test_windows_report_unavailable_with_a_reason(self) -> None:
        body = self._client.get("/novaforge/windows").json()
        self.assertFalse(body["available"])
        self.assertTrue(body["reason"])

    def test_start_is_refused_with_a_reason(self) -> None:
        response = self._client.post("/novaforge/sessions", json=_mission())
        self.assertEqual(response.status_code, 422)
        self.assertIn("capture", response.json()["detail"].lower())
        self.assertEqual(self._client.get("/novaforge/sessions").json()["count"], 0)


if __name__ == "__main__":
    unittest.main()
