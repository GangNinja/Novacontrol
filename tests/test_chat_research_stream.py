"""Chat research path: research-worthy questions asked in Chat run through the
Explore pipeline, so the UI receives explore.progress events (live stages under
the scan-line) and the answer is a sourced report — not a static model reply.
"""

from __future__ import annotations

import unittest

from novacontrol.application import NovaControlApplication
from novacontrol.brain.brain import looks_like_research_question
from novacontrol.brain.scratch import scratchable_intent


class _CapturingExplore:
    """ExploreService stand-in: records calls, returns a minimal report shape."""

    def __init__(self) -> None:
        self.researched: list[str] = []

    async def research(self, request: object) -> object:
        from novacontrol.explore.models import ExploreReport

        topic = getattr(request, "topic", "?")
        self.researched.append(topic)
        return ExploreReport(
            topic=topic,
            overview=f"Overview of {topic}.",
            key_points=("point one", "point two"),
            detailed_explanation=f"Details about {topic}.",
            sources=(),
            answer=f"A clear answer about {topic}.",
        )


class ChatResearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_detector_is_the_single_canonical_predicate(self) -> None:
        self.assertTrue(looks_like_research_question("what is photosynthesis"))
        self.assertTrue(looks_like_research_question("compare kafka and rabbitmq"))
        self.assertFalse(looks_like_research_question("open chrome"))
        # Scratch's narrow gate still owns canned local answers.
        self.assertIsNone(scratchable_intent("what is the migration of arctic terns"))
        self.assertIsNotNone(scratchable_intent("what is 2+2"))

    async def test_research_question_in_chat_runs_the_explore_pipeline(self) -> None:
        app = NovaControlApplication()
        try:
            fake = _CapturingExplore()
            app.explore = fake  # type: ignore[assignment]
            response = await app.handle_request("what is the migration pattern of arctic terns")
            self.assertEqual(fake.researched, ["what is the migration pattern of arctic terns"])
            self.assertEqual(response.route, "explore")
            self.assertEqual(response.intent, "explore")
            self.assertIn("topic", response.payload)
        finally:
            await app.stop()

    async def test_plain_chat_math_and_local_kb_stay_out_of_research(self) -> None:
        app = NovaControlApplication()
        try:
            fake = _CapturingExplore()
            app.explore = fake  # type: ignore[assignment]
            for text in ("hello there", "what is 2+2", "what is quantum computing"):
                response = await app.handle_request(text)
                self.assertEqual(response.route, "chat", text)
            self.assertEqual(fake.researched, [])
        finally:
            await app.stop()

    async def test_research_in_chat_publishes_progress_events(self) -> None:
        """The live-stage contract: explore.progress on the app-wide bus."""
        app = NovaControlApplication()
        try:
            steps: list[str] = []

            async def sink(event: object) -> None:
                payload = getattr(event, "payload", {})
                if getattr(event, "type", "") == "explore.progress":
                    steps.append(str(payload.get("step")))

            await app.event_bus.subscribe("*", sink)
            app.explore.search_provider = _OfflineProbe()
            response = await app.handle_request("what is the migration pattern of arctic terns")
            self.assertEqual(response.route, "explore")
            self.assertIn("searching", steps)
            self.assertIn("synthesizing", steps)
            self.assertIn("complete", steps)
        finally:
            await app.stop()


class _OfflineProbe:
    """Search provider that fails fast (offline) but is deterministic."""

    name = "offline-probe"

    async def search(self, request: object) -> tuple[object, ...]:
        return ()

    async def videos(self, request: object) -> tuple[object, ...]:
        return ()


if __name__ == "__main__":
    unittest.main()
