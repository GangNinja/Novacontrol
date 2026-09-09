"""Recent-activity journal + completion events feeding the web timeline.

The Recent Activity feed used to be per-tab localStorage; it is now server-fed:
completed actions are recorded in a bounded in-process journal AND announced on
the application EventBus as ``<family>.completed`` so every open UI tab appends
them over the shared /events/stream channel. These tests pin the recording
points (command execution, learning cycles, research completion), the /activity
seed endpoint, and the journal's bounded contract.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.core.activity import RecentActivityLog
from novacontrol.core.events import Event, EventBus
from novacontrol.explore import ExploreRequest, ExploreService

from conftest import FakeSearchProvider, FakeVideoProvider
from test_explore import SynthesizingLLMProvider
from test_web_api import _IsolatedApiTestCase


class RecentActivityLogTests(unittest.TestCase):
    def test_newest_first_and_bounded(self) -> None:
        log = RecentActivityLog(limit=3)
        for index in range(5):
            log.record("command", f"Command {index}")
        entries = log.recent()
        self.assertEqual(len(entries), 3)
        self.assertEqual([e["title"] for e in entries], ["Command 4", "Command 3", "Command 2"])

    def test_entry_shape_matches_sse_payload(self) -> None:
        log = RecentActivityLog()
        log.record("research", "Research complete", "black holes")
        entry = log.recent(limit=1)[0]
        self.assertEqual(set(entry), {"type", "title", "detail", "at"})
        self.assertEqual(entry["type"], "research")
        self.assertEqual(entry["title"], "Research complete")
        self.assertEqual(entry["detail"], "black holes")
        self.assertIsInstance(entry["at"], int)

    def test_clear_and_len(self) -> None:
        log = RecentActivityLog()
        log.record("learn", "Learning cycle", "goal")
        self.assertEqual(len(log), 1)
        log.clear()
        self.assertEqual(len(log), 0)
        self.assertEqual(log.recent(), [])


class ApplicationCompletionRecordingTests(unittest.TestCase):
    """Completed actions land in the journal AND on the bus as *.completed."""

    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = NovaControlApplication(data_dir=tmp.name)

    def test_executed_command_is_recorded_and_announced(self) -> None:
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.app.event_bus.subscribe("command.completed", capture))
            plan = self.app.plan_command("open notepad")
            token = plan["approval"]["token"]
            loop.run_until_complete(self.app.execute_command("open notepad", approval_token=token))
            loop.run_until_complete(asyncio.sleep(0))  # let the bus task run
        finally:
            loop.close()
        entries = self.app.activity.recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "command")
        self.assertEqual(entries[0]["title"], "Command executed")
        self.assertEqual(entries[0]["detail"], "open notepad")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["detail"], "open notepad")
        self.assertEqual(seen[0].payload["type"], "command")

    def test_plan_only_records_nothing(self) -> None:
        # Planning alone (no token consumed, nothing executed) must not claim
        # a completion — the timeline records what RAN, not what was previewed.
        self.app.plan_command("open notepad")
        self.assertEqual(len(self.app.activity), 0)

    def test_learning_cycle_is_recorded_and_announced(self) -> None:
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.app.event_bus.subscribe("learn.completed", capture))
            loop.run_until_complete(self.app.learning_cycle("improve memory"))
            loop.run_until_complete(asyncio.sleep(0))
        finally:
            loop.close()
        entries = self.app.activity.recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "learn")
        self.assertEqual(entries[0]["title"], "Learning cycle")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["title"], "Learning cycle")

    def test_record_activity_without_running_loop_is_safe(self) -> None:
        # Called from a sync context (no event loop): the journal record must
        # still happen; only the bus announce is skipped.
        self.app._record_activity("command", "Command executed", "open calc")
        self.assertEqual(len(self.app.activity), 1)


class ExploreCompletionTests(unittest.TestCase):
    """explore.completed fires on the fresh AND cached research paths."""

    def _service(self, bus: EventBus) -> ExploreService:
        return ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            completion_provider=SynthesizingLLMProvider(),
            event_bus=bus,
        )

    def test_fresh_research_announces_completion(self) -> None:
        bus = EventBus()
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(bus.subscribe("explore.completed", capture))
            loop.run_until_complete(self._service(bus).research(ExploreRequest(topic="black holes")))
        finally:
            loop.close()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["detail"], "black holes")
        self.assertEqual(seen[0].payload["type"], "research")

    def test_cached_research_also_announces(self) -> None:
        bus = EventBus()
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(bus.subscribe("explore.completed", capture))
            service = self._service(bus)
            request = ExploreRequest(topic="black holes")
            loop.run_until_complete(service.research(request))
            loop.run_until_complete(service.research(request))  # cache hit
        finally:
            loop.close()
        self.assertEqual(len(seen), 2)


class ActivityEndpointTests(_IsolatedApiTestCase):
    """/activity seeds the timeline from the isolated app's journal."""

    def test_activity_returns_journal_entries(self) -> None:
        assert self._nova is not None
        self._nova.activity.record("command", "Command executed", "open notepad")
        response = self._client.get("/activity")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["activity"]), 1)
        self.assertEqual(body["activity"][0]["type"], "command")
        self.assertEqual(body["activity"][0]["detail"], "open notepad")

    def test_activity_starts_empty(self) -> None:
        response = self._client.get("/activity")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"activity": []})

    def test_api_surface_declares_activity_route(self) -> None:
        from novacontrol.api.models import ApiSurface

        paths = {(r.method, r.path) for r in ApiSurface.default().routes}
        self.assertIn(("GET", "/activity"), paths)


if __name__ == "__main__":
    unittest.main()
