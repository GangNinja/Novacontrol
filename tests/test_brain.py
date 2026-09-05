"""Tests for NovaBrain: routing, conversation, LLM integration, and edge cases."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping, Sequence

from novacontrol.brain import ConversationManager, NovaBrain
from novacontrol.brain.brain import BrainDecision, BrainIntent, BrainRequest, BrainResponse
from conftest import EchoProvider, FailingProvider


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
    # ...but with no research verb, plan nouns still route PLAN, not EXPLORE.
    ("break down the plan", BrainIntent.PLAN),
    ("plan the project", BrainIntent.PLAN),
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


class BrainIntentRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Table-driven: every (text, expected_intent) pair is one subTest."""

    def test_intent_routing(self) -> None:
        brain = NovaBrain()
        for text, expected in INTENT_ROUTING:
            with self.subTest(text=text):
                decision = brain.decide(BrainRequest(text))
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


if __name__ == "__main__":
    unittest.main()
