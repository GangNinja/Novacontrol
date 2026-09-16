"""Central AI brain for request routing and response shaping."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from novacontrol.brain.conversation import ConversationManager
from novacontrol.brain.models import BrainDecision, BrainIntent, BrainRequest, BrainResponse
from novacontrol.brain.scratch import ScratchReasoningEngine, classify_broad, scratchable_intent
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
        on_provider_upgrade: Callable[[object], None] | None = None,
        ollama_reprobe: Callable[[], Awaitable[object | None]] | None = None,
    ) -> None:
        self.completion_provider = completion_provider or EchoLLMProvider()
        self._on_provider_upgrade = on_provider_upgrade
        self._ollama_reprobe = ollama_reprobe
        # The boot-time provider the user configured (or auto-detected). Kept so
        # set_mode("llm") can re-arm the external model after a scratch detour
        # without rebuilding the app, and so "auto" can restore it.
        self._boot_provider = self.completion_provider
        # Cloud slot: a user-configured external LLM (ChatGPT/Gemini/Groq/…)
        # installed via set_cloud_provider. Kept separate from the boot/local
        # provider so switching local ↔ cloud never loses either configuration.
        self._cloud_provider: object | None = None
        # User-facing brain mode: "auto" (best local LLM, else scratch), "llm"
        # (force the local/external env model), "scratch" (always local rules),
        # "cloud" (the configured cloud LLM, falling back like "llm").
        self.mode = "auto"
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
    def model_name(self) -> str:
        """The active completion model, or '' when no external model is configured."""
        if not self.model_configured:
            return ""
        return str(getattr(self.completion_provider, "model", "") or "")

    @property
    def model_configured(self) -> bool:
        return str(getattr(self.completion_provider, "name", "unknown")) != "echo"

    @property
    def conversation(self) -> ConversationManager:
        return self._conversation

    @property
    def effective_mode(self) -> str:
        """The brain actually answering right now: "llm" or "scratch".

        Derived from the live provider, not from the user's chosen mode, so
        forced "llm" with no model available still reports scratch honestly.
        """
        return "llm" if self.model_configured else "scratch"

    def set_cloud_provider(self, provider: object | None) -> None:
        """Install (or clear) the cloud LLM and switch onto it when present."""
        self._cloud_provider = provider
        if provider is not None:
            self.mode = "cloud"
            self.completion_provider = provider
        elif self.mode == "cloud":
            self.set_mode("auto")

    def set_local_provider(self, provider: object) -> None:
        """Swap the LOCAL brain onto a new provider (the model picker's swap).

        This replaces the boot/local slot (unlike set_cloud_provider, which
        fills the cloud slot) and re-arms it: a later "llm"/"auto" restores
        THIS provider, and a "scratch" detour remembers it for the way back.
        The previous boot provider is kept on the provider object chain only if
        the caller built it that way — here the picker's provider IS the local
        truth.
        """
        self._boot_provider = provider
        if self.mode in {"llm", "auto"}:
            self.completion_provider = provider
        # "cloud" stays on the cloud provider; "scratch" stays on Echo — both
        # pick the new local provider up when the user switches back.

    @property
    def cloud_provider_name(self) -> str:
        """The installed cloud LLM's name ('cloud:openai'), or '' when none."""
        if self._cloud_provider is None:
            return ""
        return str(getattr(self._cloud_provider, "name", ""))

    def set_mode(self, mode: str) -> None:
        """Switch the chat brain: auto | llm | scratch | cloud (no restart).

        - "llm" forces the local/env model (re-arming the boot provider if a
          previous "scratch" mode swapped it out); when no model exists this is
          a no-op that stays on scratch.
        - "scratch" swaps Echo in, so every chat answer is local; the boot
          provider is remembered for a later "llm"/"auto".
        - "cloud" activates the configured cloud LLM; with none configured it
          behaves exactly like "llm" (best local model, else scratch).
        - "auto" restores the boot provider; the lazy Ollama re-probe resumes
          upgrading it when Ollama appears.
        """
        if mode == "cloud":
            self.completion_provider = (
                self._cloud_provider
                if self._cloud_provider is not None
                else self._boot_provider
            )
        elif mode == "llm":
            if self._boot_provider is not None and str(
                getattr(self._boot_provider, "name", "")
            ) != "echo":
                self.completion_provider = self._boot_provider
            # No real model available: stay on scratch (effective_mode reports it).
        elif mode == "scratch":
            if str(getattr(self.completion_provider, "name", "")) != "echo":
                self.completion_provider = EchoLLMProvider()
        else:  # "auto"
            self.completion_provider = self._boot_provider
        self.mode = mode

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

    async def _maybe_upgrade_provider(self) -> None:
        """Lazily upgrade the Echo fallback to Ollama if it started after boot.

        Runs only while no external model is configured AND the user has not
        forced scratch mode — an auto-detected model must never override the
        switch. The reprobe itself is rate-limited (one 2s probe per minute) and
        cached after a hit. On the first successful upgrade the
        on_provider_upgrade callback fires so other consumers (Explore synthesis)
        swap to the same provider.
        """
        if self._ollama_reprobe is None or self.model_configured or self.mode == "scratch":
            return
        try:
            upgraded = await self._ollama_reprobe()
        except Exception as exc:  # a broken probe must never break chat
            logger.warning("LLM re-probe failed: %s", exc)
            return
        if upgraded is not None and upgraded is not self.completion_provider:
            self.completion_provider = upgraded
            if self._on_provider_upgrade is not None:
                self._on_provider_upgrade(upgraded)

    async def chat(self, request: BrainRequest) -> BrainResponse:
        """Multi-turn chat with conversation history."""
        self._conversation.add_user_message(request.text)

        decision = BrainDecision(BrainIntent.CHAT, "Answered by the configured chat model.", confidence=0.8)

        await self._maybe_upgrade_provider()
        # "scratch" mode answers locally even when a real model is configured:
        # the provider stays swapped to Echo until set_mode moves off scratch.
        if not self.model_configured:
            payload = self.scratch.answer(request.text, request.context)
            payload["provider"] = self.provider_name
            payload["brain_mode"] = self.effective_mode
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
            "brain_mode": self.effective_mode,
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
                payload={**payload, "brain_mode": self.effective_mode},
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
        return BrainResponse(
            decision=decision, payload={**payload, "brain_mode": self.effective_mode}, summary=summary
        )

    # The routing gates, in evaluation order. Each gate is
    # (name, intent, confidence, reason, predicate(lower, scratchable)). The
    # FIRST match wins — the same order decide() has always used. This table is
    # the single source of truth for the routing explorer's live trace.
    _ROUTING_GATES: tuple[tuple[str, BrainIntent, float, str, Callable[[str, str | None], bool]], ...] = (
        (
            "scratch_greeting", BrainIntent.CHAT, 0.9,
            "Request is a simple chat message.",
            lambda lower, scratchable: scratchable == "greeting",
        ),
        (
            "self_improvement", BrainIntent.SELF_IMPROVEMENT, 0.9,
            "Request asks NovaControl to inspect and improve its own code.",
            lambda lower, scratchable: _contains(
                lower,
                "self improve", "self-improve", "improve yourself", "improve itself",
                "code itself", "code yourself", "upgrade yourself", "improve your code",
                "make it intelligent", "make yourself intelligent",
            ),
        ),
        # Arithmetic is always answered locally by the scratch evaluator — never
        # research, and never stolen by later keyword blocks ("steps" -> planning).
        (
            "scratch_math", BrainIntent.CHAT, 0.88,
            "Worded arithmetic is computed locally by the scratch brain.",
            lambda lower, scratchable: scratchable == "math",
        ),
        # Device-anchored ACTION phrasing ("search cats on youtube on my phone",
        # "open spotify on phone") outranks the web-search, research, and plan
        # gates: a device word plus an explicit launch/search verb is a phone
        # command even when the topic sounds researchy ("latest news").
        # Question forms ("how does message delivery work on android") carry
        # no launch/search verb, so they fall through to the research gate —
        # which is why this is NOT simply _looks_like_phone_command() here.
        (
            "phone_anchored_action", BrainIntent.PHONE_CONTROL, 0.84,
            "Request asks for phone control.",
            lambda lower, scratchable: (
                _looks_like_phone_command(lower)
                and _contains(lower, "phone", "android", "mobile")
                and _contains(lower, "open ", "launch ", "search", "find ", "look up", "look for")
            ),
        ),
        # Explicit web-search phrasing drives the REAL browser to a search engine
        # ("google X", "search the web for X") — checked before EXPLORE so a query
        # that itself sounds like a research question ("...what is X") still opens
        # the browser instead of being rerouted to research synthesis.
        (
            "web_search", BrainIntent.BROWSER_AUTOMATION, 0.82,
            "Request asks to search the web in a browser.",
            lambda lower, scratchable: _looks_like_web_search(lower),
        ),
        # Explanation / research requests — but only if the scratch brain has no
        # canned local answer (covers "what is", "what are", and research verbs).
        # NOT when the text is being REMEMBERED: "remember this: facts about
        # cats" stores the fact list, it does not research cats — the explicit
        # store phrasing outranks research words inside the remembered content.
        (
            "research_question", BrainIntent.EXPLORE, 0.86,
            "Request asks for explanation or research.",
            lambda lower, scratchable: (
                looks_like_research_question(lower)
                and scratchable is None
                and not _contains(lower, "remember this", "remember that", "remember for me")
            ),
        ),
        # Explicit store-into-memory phrasing ("remember this: …") must not be
        # stolen by content words named inside the remembered text — apps
        # ("remember this: open notepad is my favorite app"), plan nouns
        # ("remember this: create a roadmap for the project"), or browser
        # targets. It therefore sits ABOVE the plan/phone/desktop/browser
        # blocks. Generic "memory" mentions stay below, so topic questions like
        # "how does computer memory work" still reach research.
        (
            "memory_store", BrainIntent.MEMORY, 0.85,
            "Request asks to store something in memory.",
            lambda lower, scratchable: _contains(lower, "remember this", "remember that", "remember for me"),
        ),
        (
            "plan", BrainIntent.PLAN, 0.84,
            "Request asks for planning.",
            lambda lower, scratchable: _contains(lower, "roadmap", "milestone", "break down", "steps")
            or re.search(r"\bplan\b", lower) is not None,
        ),
        (
            "phone_command", BrainIntent.PHONE_CONTROL, 0.82,
            "Request asks for phone control.",
            lambda lower, scratchable: _looks_like_phone_command(lower),
        ),
        (
            "desktop_command", BrainIntent.DESKTOP_AUTOMATION, 0.8,
            "Request asks for desktop automation.",
            lambda lower, scratchable: _looks_like_desktop_command(lower),
        ),
        (
            "browser", BrainIntent.BROWSER_AUTOMATION, 0.8,
            "Request asks for browser automation.",
            lambda lower, scratchable: _contains(
                lower, "browser", "website", "navigate", "fill form", "fill the form", "download"
            ),
        ),
        (
            "memory", BrainIntent.MEMORY, 0.72,
            "Request refers to memory.",
            lambda lower, scratchable: _contains(lower, "remember", "recall", "memory"),
        ),
        (
            "agent", BrainIntent.AGENT, 0.78,
            "Request should be delegated to a specialized agent.",
            lambda lower, scratchable: _contains(
                lower, "code", "test", "debug", "review", "implement", "fix", "document"
            ),
        ),
        (
            "project", BrainIntent.PROJECT, 0.72,
            "Request refers to project management.",
            lambda lower, scratchable: _contains(lower, "project", "task", "bug", "progress"),
        ),
        (
            "chat_fallback", BrainIntent.CHAT, 0.55,
            "Fallback to model-backed chat.",
            lambda lower, scratchable: True,
        ),
    )

    def _keyword_classify(self, lower: str) -> BrainDecision:
        """Fast keyword-based classification (first matching routing gate)."""
        # One narrow classifier owns all "scratch can answer this locally"
        # detection (greeting / math / time / conversion / knowledge /
        # recommendation). decide() only interprets its returned intent name.
        scratchable = scratchable_intent(lower)
        for _name, intent, confidence, reason, predicate in self._ROUTING_GATES:
            if predicate(lower, scratchable):
                return BrainDecision(intent, reason, confidence=confidence)
        raise AssertionError("chat_fallback gate always matches")

    def route_utterance(self, text: str) -> dict[str, Any]:
        """Trace every routing gate for an utterance, in evaluation order.

        The routing explorer consumes this: each rung reports whether it
        matched and the decision it would produce, so the user sees exactly
        which gate owns their phrasing and why. Mirrors decide() exactly — the
        landing decision IS decide(text).

        Also returns the ``classifiers`` block: the narrow routing gate
        (scratchable_intent — what decide() consults) compared with the broad
        engine's full classification, so the explorer can highlight where the
        two deliberately disagree.
        """
        text = text.strip()
        if not text:
            decision = BrainDecision(BrainIntent.CLARIFY, "Request is empty.", confidence=1.0)
            return {**decision.to_dict(), "trace": [], "classifiers": _classifier_relation(None, "unknown")}
        lower = text.lower()
        scratchable = scratchable_intent(lower)
        trace: list[dict[str, Any]] = []
        for name, intent, confidence, reason, predicate in self._ROUTING_GATES:
            matched = predicate(lower, scratchable)
            trace.append(
                {
                    "gate": name,
                    "matched": matched,
                    "intent": intent.value if matched else None,
                    "confidence": confidence if matched else None,
                    "reason": reason if matched else None,
                }
            )
            if matched:
                break
        decision = self.decide(BrainRequest(text=text))
        classifiers = _classifier_relation(scratchable, classify_broad(lower))
        return {**decision.to_dict(), "trace": trace, "classifiers": classifiers}

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


def _classifier_relation(narrow: str | None, broad: str) -> dict[str, str | None]:
    """Compare the narrow routing gate with the broad engine's view.

    The routing explorer highlights the DELIBERATE disagreements between the
    two classifiers: the narrow router hides engine-only intents (phone /
    desktop / capabilities are routing_safe=False so the request reaches real
    automation), demands exact canned keys before answering locally, and
    promotes math ahead of time/conversion — while the broad engine keeps its
    historical row order and wide detect predicates.
    """
    if narrow is not None and narrow == broad:
        return {"narrow": narrow, "broad": broad, "relation": "match",
                "note": "Both classifiers land on the same intent."}
    if narrow is None and broad in ("phone_control", "desktop_control", "capabilities"):
        return {
            "narrow": narrow, "broad": broad, "relation": "engine_only",
            "note": (
                f"The broad engine sees {broad}, but its row is routing_safe=False: "
                "the narrow router deliberately hides it so the request is dispatched "
                "to real device automation instead of a canned local answer."
            ),
        }
    if narrow is None and broad == "unknown":
        return {
            "narrow": narrow, "broad": broad, "relation": "unknown",
            "note": "Neither classifier has a local answer — the request falls through to research or the configured model.",
        }
    if narrow is None:
        return {
            "narrow": narrow, "broad": broad, "relation": "breadth",
            "note": (
                f"The broad engine matches {broad} with its wide detect, but the narrow router "
                "requires an exact canned key, so routing deliberately sends it onward "
                "(usually Explore) instead of answering locally."
            ),
        }
    return {
        "narrow": narrow, "broad": broad, "relation": "order",
        "note": (
            f"The narrow router promotes {narrow} ahead of time/conversion, while the broad "
            f"engine keeps its historical order and lands on {broad}. Routing intentionally "
            "uses the narrow order."
        ),
    }


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)

def looks_like_research_question(text: str) -> bool:
    """ONE canonical detector for "this deserves a researched answer".

    Covers "what is / what are" questions, the research verbs, and "how
    does/do …" question forms. Scratch's narrow routing gate decides
    separately whether a canned LOCAL answer exists — callers must combine
    both (a hit here + no scratch answer means the question needs real
    research synthesis).

    The "how does/do" forms matter for cross-intent freezing: without them,
    "how do cookies work in the browser" was stolen by the browser keyword
    block and "how does computer memory work" by the memory block — a topic
    noun must not outrank an explicit question. "How to" covers instructional
    requests ("how to start a container garden") that want a researched
    answer, not the chat fallback's "use Explore" deflection.

    The casual-scaffold forms ("things to know about X", "the deal with X",
    "stuff/facts about X", "wtf is X") are how people actually type; they are
    research requests even though no textbook question word appears. The
    explore extractor anchors on the same scaffolding to pull the topic out.
    """
    explore_keywords = [
        "research", "explain", "why", "compare",
        "teach me", "tell me about", "is it true", "true or false",
        "verify", "fact check", "latest", "online",
        "how does", "how do ", "how to",
        # Casual scaffold phrasing -> researched answer.
        "things to know about", "should i know about", "need to know about",
        "the deal with", "stuff about", "facts about", "info on",
        "information about", "basics of", "gist of", "lowdown on", "scoop on",
        "wtf is", "wth is", "what the heck is", "what the hell is",
    ]
    return _contains(text, "what is", "what are", *explore_keywords)


def _looks_like_web_search(text: str) -> bool:
    """True for explicit web-search phrasing that should drive a real browser.

    Deliberately narrower than the EXPLORE research intent ("what is",
    "explain", "research"): only phrasing that names a web search or a search
    engine routes to the browser controller's search-engine page.
    """
    return _contains(
        text,
        "search the web",
        "search the internet",
        "search on the web",
        "search web",
        "web search",
        "internet search",
        "google ",
    )


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
    """True for phrasing that should drive the phone controller.

    Two families:
    - device-anchored: a device word (phone/android/mobile/sms) plus an action
      verb — "text mom on my phone", "open whatsapp on my phone".
    - self-sufficient verbs: "call …", "dial …", "text …", and screenshot
      phrasing name the phone action directly; a bare "call john" is a phone
      command, not desktop automation or chat.
    """
    lower = text.strip().lower()
    if _contains(lower, "phone", "android", "mobile", "sms"):
        return _contains(
            lower, "open ", "launch ", "control", "send", "call ", "text ", "message",
            "screenshot", "dial ", "search", "find ", "look up", "look for",
        )
    if _contains(lower, "whatsapp"):
        return True  # a phone-only app; no device word needed
    # Phone-action verbs that stand alone: text/call/dial/screenshot.
    return (
        lower.startswith(("call ", "dial ", "text ", "sms "))
        or _contains(lower, "send a text", "send text", "take a screenshot", "take screenshot", "screenshot my phone", "screenshot of my phone")
    )


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
