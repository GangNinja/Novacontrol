"""Regression matrix for the Global Intelligence Layer (GIL).

Acceptance examples 1-10 of the intelligence architecture, driven through the
real application entry point (`handle_request`) — the ONE place every
capability consumes structured intents from.
"""

from __future__ import annotations

import unittest

from novacontrol.application import NovaControlApplication


class GlobalIntelligenceAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """The spec's acceptance examples, through the live /ask pipeline."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def _route(self, text: str) -> str:
        response = await self.app.handle_request(text)
        return response.route

    async def test_examples_1_to_4_punctuation_case_and_politeness(self) -> None:
        for text in (
            "open chrome",
            "open chrome.",
            "Open Chrome!",
            "OPEN   CHROME",
            "please open chrome",
            "launch chrome",
            "start chrome",
            "can you open the chrome browser",
        ):
            with self.subTest(command=text):
                self.assertEqual(await self._route(text), "desktop_automation")

    async def test_example_5_typo_tolerance(self) -> None:
        self.assertEqual(await self._route("opn chrme"), "desktop_automation")
        self.assertEqual(await self._route("take a screenshot"), "desktop_automation")

    async def test_example_6_research_phrasing(self) -> None:
        self.assertEqual(await self._route("research github."), "explore")

    async def test_multi_intent_decomposition(self) -> None:
        response = await self.app.handle_request("open notepad and take a screenshot")
        self.assertEqual(response.route, "desktop_automation")
        followed = response.payload.get("workflow", {}).get("actions", [])
        self.assertGreaterEqual(len(followed), 2)

    async def test_chat_and_question_fallback_still_works(self) -> None:
        self.assertEqual(await self._route("what is 2+2"), "chat")
        self.assertEqual(await self._route("hi"), "chat")

    async def test_phone_phrasing_variants_route_identically(self) -> None:
        for text in ("text mom on my phone saying hi", "text mom saying hi.", "TEXT MOM ON MY PHONE"):
            with self.subTest(command=text):
                self.assertEqual(await self._route(text), "phone_control")

    async def test_planning_phrasing_routes_to_planning(self) -> None:
        self.assertEqual(await self._route("plan my week"), "planning")

    async def test_gil_decision_is_recorded_in_brain_context(self) -> None:
        """The decision must carry the GIL strategy marker for observability."""
        request_context = self.app.status()
        understood = self.app.intelligence.understand("open chrome.")
        self.assertEqual(understood.intent.intent.value, "open_application")
        self.assertEqual(understood.strategy, "fast_path")
        # Context remembers the resolved intent for later references.
        self.assertTrue(self.app.intelligence.context.recent_intents)


class GlobalIntelligenceContextTests(unittest.IsolatedAsyncioTestCase):
    """Context/reference resolution: 'open it' after 'open chrome'."""

    async def test_reference_resolves_to_last_application(self) -> None:
        app = NovaControlApplication()
        try:
            engine = app.intelligence
            first = engine.understand("open chrome")
            engine.context.remember_intent(first.intent.to_dict())
            engine.context.remember_utterance(first.intent.normalized_input)
            second = engine.understand("open it")
            self.assertEqual(second.intent.intent.value, "open_application")
            self.assertEqual(second.intent.entities.get("application"), "chrome")
        finally:
            await app.stop()


if __name__ == "__main__":
    unittest.main()
