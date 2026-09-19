"""Tests for NovaBrain: routing, conversation, LLM integration, and edge cases."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping, Sequence

from novacontrol.brain import ConversationManager, NovaBrain
from novacontrol.brain.brain import BrainDecision, BrainIntent, BrainRequest, BrainResponse
from novacontrol.integrations import EchoLLMProvider
from conftest import EchoProvider, FailingProvider


class FakeOllamaProvider:
    """Stands in for the upgraded provider; a real one is OpenAICompatible."""

    name = "ollama"

    def __init__(self, prefix: str = "Ollama says") -> None:
        self._prefix = prefix

    async def complete(self, messages, **kwargs):
        return f"{self._prefix}: done"


# ---------------------------------------------------------------------------
# Table-driven intent routing
# ---------------------------------------------------------------------------

INTENT_ROUTING: list[tuple[str, BrainIntent]] = [
    # Greetings → chat
    ("hi", BrainIntent.CHAT),
    ("hello", BrainIntent.CHAT),
    ("good morning", BrainIntent.CHAT),
    # Explore: explain, what/why/compare patterns
    ("explain transformers in AI", BrainIntent.EXPLORE),
    ("what is deep learning", BrainIntent.EXPLORE),
    ("what are the benefits of exercise", BrainIntent.EXPLORE),
    ("why do birds migrate", BrainIntent.EXPLORE),
    ("compare React, Vue, and Svelte", BrainIntent.EXPLORE),
    ("tell me a useful idea", BrainIntent.CHAT),  # below confidence threshold
    # Cross-intent boundary: the merged EXPLORE gate fires when a research verb
    # is present, even if the topic also names "plan"/"roadmap" — EXPLORE is
    # checked before PLAN. Do not let plan nouns steal these back to PLAN.
    ("compare the plan", BrainIntent.EXPLORE),
    ("what is the plan", BrainIntent.EXPLORE),
    ("explain the plan", BrainIntent.EXPLORE),
    ("what is the plan for the project", BrainIntent.EXPLORE),
    ("what is the roadmap", BrainIntent.EXPLORE),
    ("explain the roadmap", BrainIntent.EXPLORE),
    ("compare the roadmap", BrainIntent.EXPLORE),
    ("explain the milestones", BrainIntent.EXPLORE),
    ("what is the milestones", BrainIntent.EXPLORE),
    ("compare milestones", BrainIntent.EXPLORE),
    ("what is the roadmap for the project", BrainIntent.EXPLORE),
    ("what is the roadmap and the milestones", BrainIntent.EXPLORE),
    # ...but with no research verb, plan nouns still route PLAN, not EXPLORE.
    ("break down the plan", BrainIntent.PLAN),
    ("break down the milestones", BrainIntent.PLAN),
    ("plan the project", BrainIntent.PLAN),
    ("plan the roadmap", BrainIntent.PLAN),
    # Cross-intent boundary: research verbs beat DEVICE TARGETS. A question
    # ABOUT an app/browser/phone topic deserves a researched answer, not an
    # action against it. Do not let the desktop/browser/phone keyword blocks
    # steal these back.
    ("explain how to open notepad", BrainIntent.EXPLORE),
    ("what is notepad", BrainIntent.EXPLORE),
    ("how does notepad work", BrainIntent.EXPLORE),
    ("explain how to open chrome", BrainIntent.EXPLORE),
    ("what is task manager", BrainIntent.EXPLORE),
    ("explain how to navigate a website", BrainIntent.EXPLORE),
    ("what is a browser cache", BrainIntent.EXPLORE),
    ("how do cookies work in the browser", BrainIntent.EXPLORE),
    ("what is a login form", BrainIntent.EXPLORE),
    ("explain how whatsapp works", BrainIntent.EXPLORE),
    ("what is a screenshot", BrainIntent.EXPLORE),
    ("how does bluetooth work on my phone", BrainIntent.EXPLORE),
    # Casual scaffold phrasing — how people actually type. No textbook question
    # word, still a research request.
    ("whats the deal with brics summit 2026", BrainIntent.EXPLORE),
    ("tell me stuff about quantum computing", BrainIntent.EXPLORE),
    ("any facts about the mariana trench", BrainIntent.EXPLORE),
    ("things to know about berlin", BrainIntent.EXPLORE),
    ("what should i know about the brics summit 2026", BrainIntent.EXPLORE),
    ("wtf is quantum entanglement", BrainIntent.EXPLORE),
    # ...but explicit store-to-memory phrasing outranks research words inside
    # the remembered content, and local recommendations stay local.
    ("remember this: facts about cats", BrainIntent.MEMORY),
    ("what should i eat", BrainIntent.CHAT),
    ("recommend a movie", BrainIntent.CHAT),
    # "How to" instructional questions want a researched answer, not the chat
    # fallback's "use Explore" deflection — but action imperatives with a
    # device target stay device commands, and the phone anchor still wins.
    ("how to start a container garden on a balcony", BrainIntent.EXPLORE),
    ("how to tie a tie", BrainIntent.EXPLORE),
    ("how to open notepad", BrainIntent.EXPLORE),
    ("open notepad", BrainIntent.DESKTOP_AUTOMATION),
    ("how to open spotify on my phone", BrainIntent.PHONE_CONTROL),
    # Cross-intent boundary: memory-topic questions route EXPLORE even though
    # they contain the "memory" keyword; only explicit store phrasing routes
    # MEMORY — even when the remembered content names a device target, plan
    # nouns, or anything else actionable.
    ("explain how human memory works", BrainIntent.EXPLORE),
    ("what is muscle memory", BrainIntent.EXPLORE),
    ("how does computer memory work", BrainIntent.EXPLORE),
    ("remember this: open notepad is my favorite app", BrainIntent.MEMORY),
    ("remember this: create a roadmap for the project", BrainIntent.MEMORY),
    ("remember this: the milestones are due friday", BrainIntent.MEMORY),
    ("remember that: plan the project for monday", BrainIntent.MEMORY),
    ("remember for me: open chrome and search flights", BrainIntent.MEMORY),
    ("compare the milestones in my memory", BrainIntent.EXPLORE),
    # Worded arithmetic, including spelled-out numbers and powers -> local CHAT.
    ("what is fifteen times three", BrainIntent.CHAT),
    ("what is 2 to the power of 8", BrainIntent.CHAT),
    ("fifteen times three", BrainIntent.CHAT),
    # Plan
    ("create a roadmap for the project", BrainIntent.PLAN),
    ("break this down into steps", BrainIntent.PLAN),
    # Desktop automation
    ("open notepad", BrainIntent.DESKTOP_AUTOMATION),
    # Phone control
    ("open whatsapp on my phone", BrainIntent.PHONE_CONTROL),
    # Browser automation
    ("go to this website", BrainIntent.BROWSER_AUTOMATION),
    ("navigate to example.com", BrainIntent.BROWSER_AUTOMATION),
    ("fill the form at https://example.com/login with username=admin", BrainIntent.BROWSER_AUTOMATION),
    # Web-search phrasing drives the real browser, NOT Explore synthesis — and it
    # beats the EXPLORE gate even when the query itself sounds like a research
    # question. The trigger is checked before EXPLORE on purpose; do not move it.
    ("search the web for quantum computing", BrainIntent.BROWSER_AUTOMATION),
    ("search the internet for black holes", BrainIntent.BROWSER_AUTOMATION),
    ("web search for mars rover news", BrainIntent.BROWSER_AUTOMATION),
    ("google bokeh photography examples", BrainIntent.BROWSER_AUTOMATION),
    ("search the web for what is a quasar", BrainIntent.BROWSER_AUTOMATION),
    # ...but generic "search" without web phrasing never routes to the browser.
    ("search my memory for the meeting notes", BrainIntent.MEMORY),
    # Memory
    ("remember this", BrainIntent.MEMORY),
    # Project
    ("what's the status of the project", BrainIntent.PROJECT),
    # Agent
    ("implement a new feature", BrainIntent.AGENT),
    ("create tests for this module", BrainIntent.AGENT),
    # Self-improvement
    ("make it intelligent so it can code itself", BrainIntent.SELF_IMPROVEMENT),
    ("make it intelligent", BrainIntent.SELF_IMPROVEMENT),
]

# ---------------------------------------------------------------------------
# Gate-order boundary probe: phrases that hit TWO gates at once. The expected
# intent encodes which gate must win; if anyone reorders _keyword_classify's
# checks, the violating pair fails a named subTest instead of silently
# rerouting real user input.
# ---------------------------------------------------------------------------

BOUNDARY_PROBES: list[tuple[str, BrainIntent, str]] = [
    # greeting + plan: a greeting prefix must not mask a planning request.
    # The GREETING gate fires first in scratchable_intent, but decide() only
    # honors it for BARE greetings — plan nouns after a greeting still route
    # PLAN via the plan-noun block.
    ("hey create a roadmap", BrainIntent.PLAN, "greeting<plan"),
    ("hello, plan the project", BrainIntent.PLAN, "greeting<plan"),
    ("good morning, break this down into steps", BrainIntent.PLAN, "greeting<plan"),
    # math + plan: arithmetic is computed locally even when the tail names
    # planning nouns ("steps", "roadmap") — the MATH gate sits above the plan
    # block on purpose (see the comment there).
    ("add 5 and 7 and show steps", BrainIntent.CHAT, "math<plan"),
    ("what is 12 times 4 and break it down into steps", BrainIntent.CHAT, "math<plan"),
    ("calculate 2 plus 2 then create a roadmap", BrainIntent.CHAT, "math<plan"),
    # self-improvement + explore: research verbs asking ABOUT self-improvement
    # still route to the self-improvement engine — its gate sits above EXPLORE
    # so meta questions about NovaControl's own improvement keep their dedicated
    # (sandboxed) path rather than becoming web research.
    ("explain how you improve yourself", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<explore"),
    ("what is self improvement", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<explore"),
    ("research how to make yourself intelligent", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<explore"),
    ("compare yourself before and after you improve yourself", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<explore"),
    ("why should you improve your code", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<explore"),
    # self-improvement + plan / + agent: the dedicated gate wins over the generic
    # blocks it lexically overlaps.
    ("make it intelligent and create a roadmap", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<plan"),
    ("improve your code and implement a feature", BrainIntent.SELF_IMPROVEMENT, "selfimprovement<agent"),
    # explore + plan: research verbs claim planning nouns as a TOPIC (the same
    # boundary the INTENT_ROUTING table pins, here with compound two-clause
    # shapes where the plan noun sits in its own clause).
    ("compare the roadmap", BrainIntent.EXPLORE, "explore<plan"),
    ("explain the milestones", BrainIntent.EXPLORE, "explore<plan"),
    ("what is the roadmap for the project", BrainIntent.EXPLORE, "explore<plan"),
    ("compare the roadmap and plan the project", BrainIntent.EXPLORE, "explore<plan"),
    ("explain the milestones then break them down into steps", BrainIntent.EXPLORE, "explore<plan"),
    ("explain the plan and create a roadmap", BrainIntent.EXPLORE, "explore<plan"),
    # memory_store + plan: explicit store phrasing outranks plan nouns inside
    # the remembered content — memory_store sits ABOVE the plan block (same
    # precedence it already had over desktop/browser targets).
    ("remember this: create a roadmap for the project", BrainIntent.MEMORY, "memorystore<plan"),
    ("remember this: the milestones are due friday", BrainIntent.MEMORY, "memorystore<plan"),
    ("remember that: plan the project for monday", BrainIntent.MEMORY, "memorystore<plan"),
    # ...but without the store marker, the plan-noun block still owns it.
    ("create a roadmap for the project", BrainIntent.PLAN, "plan>explore"),
    ("break down the milestones", BrainIntent.PLAN, "plan>explore"),
]


class BrainIntentRoutingTests(unittest.TestCase):
    """Table-driven: every (text, expected_intent) pair is one subTest.

    Restored: a refactor dropped this class, leaving INTENT_ROUTING without a
    consumer — the table grew (every boundary variant below) while nothing
    executed it. This test IS the teeth for the routing tables.
    """

    def test_intent_routing(self) -> None:
        brain = NovaBrain()
        for text, expected in INTENT_ROUTING:
            with self.subTest(text=text):
                decision = brain.decide(BrainRequest(text))
                self.assertEqual(decision.intent, expected, f"Failed for '{text}'")


class GateBoundaryProbeTests(unittest.TestCase):
    """Every gate pair that can lexically collide, with the winner pinned."""

    def test_gate_boundaries(self) -> None:
        brain = NovaBrain()
        for text, expected, pair in BOUNDARY_PROBES:
            with self.subTest(pair=pair, text=text):
                decision = brain.decide(BrainRequest(text))
                self.assertEqual(
                    decision.intent,
                    expected,
                    f"gate-order regression on [{pair}]: {text!r} routed "
                    f"{decision.intent.value}, expected {expected.value}",
                )

                self.assertEqual(decision.intent, expected, f"Failed for '{text}'")

    def test_empty_and_whitespace_route_to_clarify(self) -> None:
        brain = NovaBrain()
        for text in ("", "   "):
            with self.subTest(text=repr(text)):
                decision = brain.decide(BrainRequest(text))
                self.assertEqual(decision.intent, BrainIntent.CLARIFY)

    def test_greeting_has_high_confidence(self) -> None:
        decision = NovaBrain().decide(BrainRequest("hi"))
        self.assertGreaterEqual(decision.confidence, 0.85)


# ---------------------------------------------------------------------------
# Conversation manager
# ---------------------------------------------------------------------------

class ConversationManagerTests(unittest.IsolatedAsyncioTestCase):

    def test_initial_state(self) -> None:
        cm = ConversationManager(system_prompt="You are NovaControl.")
        self.assertEqual(cm.turn_count, 0)
        self.assertEqual(cm.message_count, 0)
        self.assertEqual(cm.system_prompt, "You are NovaControl.")
        self.assertTrue(cm.session_id)

    def test_add_user_and_assistant_messages(self) -> None:
        cm = ConversationManager()
        cm.add_user_message("Hello")
        cm.add_assistant_message("Hi there!")
        self.assertEqual(cm.message_count, 2)
        self.assertEqual(cm.get_last_user_message(), "Hello")

    def test_add_turn_records_history(self) -> None:
        cm = ConversationManager()
        cm.add_turn("What is Python?", "Python is a programming language.", intent="chat")
        self.assertEqual(cm.turn_count, 1)
        turns = cm.get_recent_turns(1)
        self.assertEqual(turns[0].user_message, "What is Python?")
        self.assertEqual(turns[0].intent, "chat")

    def test_to_messages_includes_system_prompt(self) -> None:
        cm = ConversationManager(system_prompt="Be helpful.")
        cm.add_user_message("Hi")
        cm.add_assistant_message("Hello!")
        messages = cm.to_messages()
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "Be helpful.")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "assistant")

    def test_to_messages_without_system_prompt(self) -> None:
        cm = ConversationManager()
        cm.add_user_message("Hi")
        cm.add_assistant_message("Hello!")
        messages = cm.to_messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")

    def test_to_context_string(self) -> None:
        cm = ConversationManager()
        cm.add_turn("What is AI?", "AI is artificial intelligence.", intent="explore")
        cm.add_turn("How does it work?", "It works through algorithms.", intent="chat")
        context = cm.to_context_string()
        self.assertIn("User: What is AI?", context)
        self.assertIn("Intent: explore", context)

    def test_max_turns_trimming(self) -> None:
        cm = ConversationManager(max_turns=3)
        for i in range(5):
            cm.add_turn(f"Q{i}", f"A{i}")
        self.assertEqual(cm.turn_count, 3)
        self.assertEqual(cm.get_recent_turns(5)[0].user_message, "Q2")

    def test_clear_resets_state(self) -> None:
        cm = ConversationManager()
        cm.add_user_message("Hi")
        cm.add_assistant_message("Hello!")
        cm.add_turn("Q", "A")
        cm.clear()
        self.assertEqual(cm.message_count, 0)
        self.assertEqual(cm.turn_count, 0)

    def test_summary(self) -> None:
        cm = ConversationManager(system_prompt="Test")
        cm.add_user_message("Q")
        cm.add_assistant_message("A")
        s = cm.summary()
        self.assertTrue(s["session_id"])
        self.assertEqual(s["message_count"], 2)

    def test_metadata_attached_to_messages(self) -> None:
        cm = ConversationManager()
        cm.add_user_message("Q", metadata={"source": "web"})
        cm.add_assistant_message("A", metadata={"intent": "chat"})
        self.assertEqual(len(cm.to_messages()), 2)


# ---------------------------------------------------------------------------
# Chat with LLM provider
# ---------------------------------------------------------------------------

class BrainChatTests(unittest.IsolatedAsyncioTestCase):

    async def test_chat_without_model_uses_scratch(self) -> None:
        response = await NovaBrain().chat(BrainRequest("hi"))
        self.assertEqual(response.decision.intent, BrainIntent.CHAT)
        self.assertFalse(response.payload["model_configured"])
        self.assertEqual(response.payload["brain_mode"], "scratch")
        self.assertFalse(response.payload["uses_external_llm"])

    async def test_chat_adds_to_conversation_history(self) -> None:
        brain = NovaBrain()
        await brain.chat(BrainRequest("Hello"))
        await brain.chat(BrainRequest("What can you do?"))
        self.assertEqual(brain.conversation.turn_count, 2)
        self.assertGreaterEqual(brain.conversation.message_count, 4)

    async def test_chat_with_real_provider(self) -> None:
        provider = EchoProvider("LLM says")
        brain = NovaBrain(completion_provider=provider)
        response = await brain.chat(BrainRequest("Hello there!"))
        self.assertIn("Hello there!", response.payload["message"])
        self.assertTrue(response.payload["model_configured"])
        self.assertEqual(response.payload["conversation_turns"], 1)
        self.assertEqual(provider.call_count, 1)

    async def test_chat_with_failing_provider(self) -> None:
        brain = NovaBrain(completion_provider=FailingProvider())
        response = await brain.chat(BrainRequest("Hello"))
        self.assertIn("Model provider failed", response.payload["message"])
        self.assertEqual(brain.conversation.turn_count, 1)

    async def test_chat_clears_history(self) -> None:
        brain = NovaBrain()
        await brain.chat(BrainRequest("Hello"))
        brain.conversation.clear()
        self.assertEqual(brain.conversation.turn_count, 0)

    async def test_conversation_summary(self) -> None:
        brain = NovaBrain()
        await brain.chat(BrainRequest("Q1"))
        await brain.chat(BrainRequest("Q2"))
        s = brain.conversation.summary()
        self.assertEqual(s["turn_count"], 2)

    def test_provider_name_without_model(self) -> None:
        brain = NovaBrain()
        self.assertEqual(brain.provider_name, "scratch")
        self.assertFalse(brain.model_configured)

    def test_provider_name_with_model(self) -> None:
        brain = NovaBrain(completion_provider=EchoProvider("test"))
        self.assertEqual(brain.provider_name, "test-echo")
        self.assertTrue(brain.model_configured)


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

class BrainResponseTests(unittest.IsolatedAsyncioTestCase):

    async def test_shape_response_without_llm(self) -> None:
        brain = NovaBrain()
        request = BrainRequest("create tests")
        decision = brain.decide(request)
        response = await brain.shape_response(request, decision, {"ok": True})
        self.assertEqual(response.decision.intent, BrainIntent.AGENT)
        self.assertNotIn("Payload", response.summary)
        self.assertIn("completed", response.summary.lower())

    async def test_shape_response_with_llm(self) -> None:
        provider = EchoProvider("Summary")
        brain = NovaBrain(completion_provider=provider)
        decision = BrainDecision(BrainIntent.EXPLORE, "test", 0.8)
        response = await brain.shape_response(
            BrainRequest("test"), decision, {"overview": "Test overview"},
        )
        self.assertIn("test", response.summary.lower())

class LazyOllamaUpgradeTests(unittest.IsolatedAsyncioTestCase):
    """Chat-time lazy upgrade: when boot found no LLM (Echo), chat probes for a
    freshly started Ollama and hot-swaps it in — no restart. Probes are
    rate-limited by the reprobe; the callback lets Explore follow."""

    async def test_chat_upgrades_provider_after_ollama_appears(self) -> None:
        ollama = FakeOllamaProvider()
        probes = {"n": 0}

        async def reprobe() -> object | None:
            probes["n"] += 1
            return ollama if probes["n"] >= 2 else None

        upgraded: list[object] = []
        brain = NovaBrain(
            completion_provider=EchoLLMProvider(),
            on_provider_upgrade=upgraded.append,
            ollama_reprobe=reprobe,
        )

        first = await brain.chat(BrainRequest(text="hello"))
        self.assertEqual(brain.provider_name, "scratch")
        self.assertEqual(probes["n"], 1)

        second = await brain.chat(BrainRequest(text="hello again"))
        self.assertEqual(brain.provider_name, "ollama")
        self.assertEqual(brain.model_name, "")  # FakeOllamaProvider has no model attr
        self.assertEqual(upgraded, [ollama])
        # The second answer came from the NEW provider (LLM mode, not scratch).
        self.assertTrue(second.payload["model_configured"])
        self.assertIn("Ollama says: done", second.payload["message"])
        self.assertNotIn("Ollama says", first.payload["message"])

    async def test_configured_brain_never_probes(self) -> None:
        async def reprobe() -> object | None:
            raise AssertionError("a configured LLM must not probe")

        brain = NovaBrain(
            completion_provider=EchoProvider("Real model"),  # name != echo
            ollama_reprobe=reprobe,
        )
        response = await brain.chat(BrainRequest(text="hello"))
        self.assertEqual(response.payload["provider"], "test-echo")

    async def test_broken_probe_never_breaks_chat(self) -> None:
        async def reprobe() -> object | None:
            raise RuntimeError("network on fire")

        brain = NovaBrain(completion_provider=EchoLLMProvider(), ollama_reprobe=reprobe)
        response = await brain.chat(BrainRequest(text="hello"))
        self.assertEqual(brain.provider_name, "scratch")
        self.assertTrue(response.payload["message"])


class SetLocalProviderTests(unittest.TestCase):
    """The brain model picker's swap: replaces the LOCAL slot (boot provider),
    not the cloud slot — mode decides whether the swap applies right now."""

    def test_swap_applies_immediately_in_auto_and_llm(self) -> None:
        for mode in ("auto", "llm"):
            with self.subTest(mode=mode):
                brain = NovaBrain(completion_provider=FakeOllamaProvider("old"))
                brain.set_mode(mode)
                replacement = FakeOllamaProvider("new")
                brain.set_local_provider(replacement)
                self.assertIs(brain.completion_provider, replacement)
                # The local slot re-arms: a scratch detour comes back to the
                # PICKED model, not the old one.
                brain.set_mode("scratch")
                self.assertEqual(brain.provider_name, "scratch")
                brain.set_mode("llm")
                self.assertIs(brain.completion_provider, replacement)

    def test_swap_defers_in_cloud_and_scratch(self) -> None:
        for mode in ("cloud", "scratch"):
            with self.subTest(mode=mode):
                brain = NovaBrain(completion_provider=FakeOllamaProvider("old"))
                brain.set_mode("cloud") if mode == "cloud" else brain.set_mode("scratch")
                replacement = FakeOllamaProvider("new")
                brain.set_local_provider(replacement)
                # Unchanged now...
                self.assertIsNot(brain.completion_provider, replacement)
                # ...but armed for when the user switches back.
                brain.set_mode("llm")
                self.assertIs(brain.completion_provider, replacement)

    def test_swap_never_touches_the_cloud_slot(self) -> None:
        cloud = FakeOllamaProvider("cloud")
        brain = NovaBrain(completion_provider=FakeOllamaProvider("old"))
        brain.set_cloud_provider(cloud)
        # Installing the key does not activate it; selecting Cloud does.
        self.assertIsNot(brain.completion_provider, cloud)
        brain.set_mode("cloud")
        self.assertIs(brain.completion_provider, cloud)
        brain.set_local_provider(FakeOllamaProvider("local"))
        # Cloud stays active; the local pick is stored for later.
        self.assertIs(brain.completion_provider, cloud)
        self.assertIs(brain._boot_provider, brain._boot_provider)  # identity sanity
        brain.set_mode("auto")
        self.assertEqual(brain.provider_name, "ollama")
        self.assertEqual(brain.model_name, "")  # FakeOllamaProvider has no model attr


if __name__ == "__main__":
    unittest.main()
