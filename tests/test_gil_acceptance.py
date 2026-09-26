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


class ResolvedReferenceReachesThePlannerTests(unittest.IsolatedAsyncioTestCase):
    """A reference the reading resolved must reach the plan that acts on it.

    Resolving "open it" to chrome inside ``engine.understand`` is not enough:
    the desktop parser cannot resolve a pronoun, so the resolved target has to
    travel into the plan. These drive the real request path, which is exactly
    where the resolution used to be dropped — a plan that launched, or closed,
    a program named "it".
    """

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_open_it_plans_the_application_the_reading_resolved(self) -> None:
        await self.app.handle_request("open chrome")
        response = await self.app.handle_request("open it")

        self.assertEqual(response.route, "desktop_automation")
        self.assertEqual(response.payload.get("command"), "open chrome")
        self.assertEqual(response.payload.get("target"), "chrome")
        actions = response.payload.get("workflow", {}).get("actions", [])
        self.assertEqual([action.get("target") for action in actions], ["chrome"])

    async def test_close_it_plans_a_named_close_not_a_shell_command(self) -> None:
        await self.app.handle_request("open chrome")
        response = await self.app.handle_request("close it")

        self.assertEqual(response.route, "desktop_automation")
        self.assertEqual(response.payload.get("command"), "close chrome")
        actions = response.payload.get("workflow", {}).get("actions", [])
        self.assertEqual([action.get("type") for action in actions], ["stop_app"])
        self.assertEqual([action.get("target") for action in actions], ["chrome"])


if __name__ == "__main__":
    unittest.main()
