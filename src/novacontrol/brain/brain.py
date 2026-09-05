"""Central AI brain for request routing and response shaping."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from novacontrol.brain.conversation import ConversationManager
from novacontrol.brain.models import BrainDecision, BrainIntent, BrainRequest, BrainResponse
from novacontrol.brain.scratch import ScratchReasoningEngine, scratchable_intent
from novacontrol.integrations import EchoLLMProvider

logger = logging.getLogger(__name__)

CompletionProvider = Callable[[list[Mapping[str, str]]], Awaitable[str]]


class NovaBrain:
    """Classifies user requests and optionally uses an LLM provider for response shaping.

    Features:
    - Keyword-based intent classification (always available)
    - LLM-assisted intent classification (when LLM is configured)
    - Conversation history management
    - Multi-turn context
    - Streaming response support
    """

    def __init__(
        self,
        *,
        completion_provider: object | None = None,
        conversation: ConversationManager | None = None,
    ) -> None:
        self.completion_provider = completion_provider or EchoLLMProvider()
        self.scratch = ScratchReasoningEngine()
        self._conversation = conversation or ConversationManager(
            system_prompt=(
                "You are NovaControl, a practical AI assistant inside a local control app. "
                "Answer naturally and directly. If the user asks for live/current facts, "
                "tell them to use Explore. If the user asks to control desktop, browser, "
                "files, or phone, explain that execution requires explicit approval."
            )
        )

    @property
    def provider_name(self) -> str:
        if not self.model_configured:
            return "scratch"
        return str(getattr(self.completion_provider, "name", "unknown"))

    @property
    def model_configured(self) -> bool:
        return str(getattr(self.completion_provider, "name", "unknown")) != "echo"

    @property
    def conversation(self) -> ConversationManager:
        return self._conversation

    def decide(self, request: BrainRequest) -> BrainDecision:
        """Classify user intent using keyword rules, optionally augmented by LLM."""
        text = request.text.strip()
        lower = text.lower()
        if not text:
            return BrainDecision(BrainIntent.CLARIFY, "Request is empty.", confidence=1.0)

        # Fast keyword classification
        decision = self._keyword_classify(lower)
        logger.debug("decide %r -> %s (confidence=%.2f)", text, decision.intent.value, decision.confidence)
        return decision

    async def chat(self, request: BrainRequest) -> BrainResponse:
        """Multi-turn chat with conversation history."""
        self._conversation.add_user_message(request.text)

        decision = BrainDecision(BrainIntent.CHAT, "Answered by the configured chat model.", confidence=0.8)

        if not self.model_configured:
            payload = self.scratch.answer(request.text, request.context)
            payload["provider"] = self.provider_name
            response_text = payload["message"]
            self._conversation.add_assistant_message(response_text, metadata={"intent": "chat", "mode": "scratch"})
            self._conversation.add_turn(request.text, response_text, intent="chat")
            return BrainResponse(decision=decision, payload=payload, summary=response_text)

        try:
            # Build messages with conversation history
            messages = self._build_chat_messages(request)
            answer = await self._complete(messages)
        except Exception as exc:
            answer = f"Model provider failed: {type(exc).__name__}: {exc}"
            logger.error("LLM chat failed: %s", exc)

        self._conversation.add_assistant_message(answer, metadata={"intent": "chat", "mode": "llm"})
        self._conversation.add_turn(request.text, answer, intent="chat")
        payload = {
            "message": answer.strip() or "The model returned an empty answer.",
            "model_configured": True,
            "provider": self.provider_name,
            "conversation_turns": self._conversation.turn_count,
        }
        return BrainResponse(decision=decision, payload=payload, summary=answer)

    async def shape_response(
        self,
        request: BrainRequest,
        decision: BrainDecision,
        payload: Mapping[str, Any],
    ) -> BrainResponse:
        if getattr(self.completion_provider, "name", "") == "echo":
            response_text = _friendly_summary(request, decision, payload)
            return BrainResponse(
                decision=decision,
                payload=dict(payload),
                summary=response_text,
            )
        try:
            summary = await self._complete(
                [
                    {
                        "role": "system",
                        "content": "Summarize the NovaControl result in one clear sentence.",
                    },
                    {
                        "role": "user",
                        "content": f"Request: {request.text}\nIntent: {decision.intent.value}\nPayload: {payload}",
                    },
                ]
            )
        except Exception as exc:
            summary = _friendly_summary(request, decision, payload)
            logger.warning("Response shaping failed, using fallback: %s", exc)
        return BrainResponse(decision=decision, payload=dict(payload), summary=summary)

    def _keyword_classify(self, lower: str) -> BrainDecision:
        """Fast keyword-based classification."""
        # One narrow classifier owns all "scratch can answer this locally"
        # detection (greeting / math / time / conversion / knowledge /
        # recommendation). decide() only interprets its returned intent name.
        scratchable = scratchable_intent(lower)
        if scratchable == "greeting":
            return BrainDecision(BrainIntent.CHAT, "Request is a simple chat message.", confidence=0.9)
        if _contains(
            lower,
            "self improve",
            "self-improve",
            "improve yourself",
            "improve itself",
            "code itself",
            "code yourself",
            "upgrade yourself",
            "improve your code",
            "make it intelligent",
            "make yourself intelligent",
        ):
            return BrainDecision(
                BrainIntent.SELF_IMPROVEMENT,
                "Request asks NovaControl to inspect and improve its own code.",
                confidence=0.9,
            )
        # Arithmetic is always answered locally by the scratch evaluator — never
        # research, and never stolen by later keyword blocks ("steps" -> planning).
        if scratchable == "math":
            return BrainDecision(
                BrainIntent.CHAT, "Worded arithmetic is computed locally by the scratch brain.", confidence=0.88
            )
        # Explanation / research requests — but only if the scratch brain has no
        # canned local answer (covers "what is", "what are", and research verbs).
        explore_keywords = [
            "research", "explain", "why", "compare",
            "teach me", "is it true", "true or false",
            "verify", "fact check", "latest", "online",
        ]
        if _contains(lower, "what is", "what are", *explore_keywords) and scratchable is None:
            return BrainDecision(BrainIntent.EXPLORE, "Request asks for explanation or research.", confidence=0.86)
        import re as _re
        if _contains(lower, "roadmap", "milestone", "break down", "steps") or _re.search(r'\bplan\b', lower):
            return BrainDecision(BrainIntent.PLAN, "Request asks for planning.", confidence=0.84)
        if _looks_like_phone_command(lower):
            return BrainDecision(BrainIntent.PHONE_CONTROL, "Request asks for phone control.", confidence=0.82)
        if _looks_like_desktop_command(lower):
            return BrainDecision(BrainIntent.DESKTOP_AUTOMATION, "Request asks for desktop automation.", confidence=0.8)
        if _contains(lower, "browser", "website", "navigate", "fill form", "fill the form", "download"):
            return BrainDecision(BrainIntent.BROWSER_AUTOMATION, "Request asks for browser automation.", confidence=0.8)
        if _contains(lower, "remember", "recall", "memory"):
            return BrainDecision(BrainIntent.MEMORY, "Request refers to memory.", confidence=0.72)
        if _contains(lower, "code", "test", "debug", "review", "implement", "fix", "document"):
            return BrainDecision(BrainIntent.AGENT, "Request should be delegated to a specialized agent.", confidence=0.78)
        if _contains(lower, "project", "task", "bug", "progress"):
            return BrainDecision(BrainIntent.PROJECT, "Request refers to project management.", confidence=0.72)
        return BrainDecision(BrainIntent.CHAT, "Fallback to model-backed chat.", confidence=0.55)

    def _build_chat_messages(self, request: BrainRequest) -> list[dict[str, str]]:
        """Build LLM message list with conversation history."""
        messages = self._conversation.to_messages()
        # Ensure system prompt is present
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {
                "role": "system",
                "content": self._conversation.system_prompt,
            })
        # Add context if available
        if request.context:
            context_block = self._conversation.to_context_string()
            if context_block:
                messages.append({
                    "role": "system",
                    "content": f"Recent conversation context:\n{context_block}",
                })
        messages.append({"role": "user", "content": request.text})
        return messages

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        complete = getattr(self.completion_provider, "complete")
        return str(await complete(messages))


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)

def _looks_like_desktop_command(text: str) -> bool:
    browser_targets = (" website", " web page", " url", " http://", " https://")
    if any(target in text for target in browser_targets):
        return False
    known_targets = (
        "open app",
        "desktop",
        "organize files",
        "run script",
        "launch app",
        "open notepad",
        "open chrome",
        "open code",
        "open vscode",
        "open vs code",
        "open calculator",
        "open calc",
    )
    if _contains(text, *known_targets):
        return True
    return text.startswith(("open ", "launch ", "run app "))


def _looks_like_phone_command(text: str) -> bool:
    if not _contains(text, "phone", "android", "mobile", "whatsapp", "sms", "call "):
        return False
    return _contains(text, "open ", "launch ", "control", "send", "call ", "text ", "message")


def _friendly_summary(
    request: BrainRequest,
    decision: BrainDecision,
    payload: Mapping[str, Any],
) -> str:
    if "message" in payload:
        return str(payload["message"])
    if "overview" in payload:
        return str(payload["overview"])
    if "summary" in payload:
        return str(payload["summary"])
    if "plan" in payload:
        steps = payload.get("plan", {}).get("steps", []) if isinstance(payload.get("plan"), Mapping) else []
        return f"I created a plan with {len(steps)} step(s) for: {request.text}."
    if "actions" in payload:
        actions = payload.get("actions", [])
        action_count = len(actions) if hasattr(actions, "__len__") else 0
        return f"I inspected NovaControl and found {action_count} improvement action(s)."
    if "content" in payload:
        return str(payload["content"])
    if decision.intent is BrainIntent.CLARIFY:
        return "Please give me a little more detail so I can help properly."
    return "I completed the request and prepared the result."
