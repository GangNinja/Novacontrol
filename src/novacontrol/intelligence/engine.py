"""Global Input Intelligence engine.

Layered understanding with performance discipline:

  FAST PATH      deterministic normalize + registry match (no model, ~µs)
  LEARNED        previously taught linguistic variations
  CONTEXTUAL     incomplete/anaphoric input resolved from InteractionContext
  SEMANTIC       optional LLM fallback for truly novel phrasing
  CLARIFY        multiple materially different interpretations -> one question

Output is always a StructuredIntent (or a clarification request shaped as one).
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from novacontrol.intelligence.capabilities import default_capabilities
from novacontrol.intelligence.context import InteractionContext
from novacontrol.intelligence.intent import CapabilityRegistry, IntentName, IntentRegistry, RiskLevel, StructuredIntent
from novacontrol.intelligence.telemetry import InterpretationTelemetry
from novacontrol.intelligence.normalize import (
    contains_technical_token,
    correct_token,
    fuzzy_token_in,
    normalize,
    normalized_tokens,
)
from novacontrol.intelligence.rules import default_rules

# Politeness glue words removed when extracting an entity, but never removed
# from technical tokens.
_ENTITY_NOISE = re.compile(
    r"\b(?:the|a|an|my|our|please|now|app|application|program|browser|website|site|on|phone)\b",
    re.IGNORECASE,
)

# Applications with canonical spellings — the typo-tolerance vocabulary.
_APPLICATION_VOCABULARY = (
    "chrome", "edge", "firefox", "brave", "notepad", "calculator", "calc",
    "paint", "explorer", "settings", "steam", "spotify", "outlook", "word",
    "excel", "powerpoint", "code", "vscode", "terminal", "whatsapp", "youtube",
    "gmail", "github", "files",
)

# Entity kinds by intent — which entity the remainder of the phrase names.
_DEFAULT_ENTITY_KIND = {
    IntentName.OPEN_APPLICATION: "application",
    IntentName.CLOSE_APPLICATION: "application",
    IntentName.PHONE_OPEN_APP: "application",
    IntentName.RESEARCH: "topic",
    IntentName.ANSWER_QUESTION: "topic",
    IntentName.SEARCH_WEB: "query",
    IntentName.NAVIGATE: "url",
    IntentName.PHONE_CALL: "contact",
    IntentName.PHONE_SEND_TEXT: "message",
    IntentName.OPEN_FOLDER: "folder",
    IntentName.READ_FILE: "file",
    IntentName.WRITE_FILE: "file",
    IntentName.TYPE_TEXT: "text",
    IntentName.PRESS_KEY: "key",
    IntentName.RUN_COMMAND: "command",
}

# Prefixes to strip from the phrase before reading the entity.
_ENTITY_PREFIX = {
    IntentName.OPEN_APPLICATION: re.compile(r"^(?:open|launch|start|run|fire up)\s+(?:the\s+|up\s+)?"),
    IntentName.CLOSE_APPLICATION: re.compile(r"^(?:close|quit|exit|kill)\s+(?:the\s+)?"),
    IntentName.RESEARCH: re.compile(r"^(?:research|look into|dig into|find out about)\s+(?:on\s+|about\s+|the\s+)?"),
    IntentName.ANSWER_QUESTION: re.compile(r"^(?:what\s+is|what\s+are|who\s+is|who\s+was|why\s+is|why\s+are|how\s+does|how\s+do|explain|tell me about)\s+(?:the\s+)?"),
    IntentName.SEARCH_WEB: re.compile(r"^(?:google|search|look up|search for|look up for)\s+(?:the\s+|for\s+)?"),
    IntentName.NAVIGATE: re.compile(r"^(?:navigate to|go to|visit|browse to|take me to|open website|open the website|open site|open the site)\s+"),
    IntentName.PHONE_CALL: re.compile(r"^(?:call|dial|phone)\s+"),
    IntentName.OPEN_FOLDER: re.compile(r"^(?:open|show|go to)\s+(?:the\s+)?(?:folder|directory)\s+"),
    IntentName.PHONE_SEND_TEXT: re.compile(r"^(?:text|sms|send a text to|send text to|message)\s+"),
    IntentName.RUN_COMMAND: re.compile(r"^(?:run|execute)\s+(?:the\s+)?(?:script|command|following)\s*"),
}

# Reference phrases that need context to resolve.
_REFERENCE_PATTERNS = (
    "it", "that", "this", "the same", "same as before", "do the same thing",
    "that one", "that file", "the thing", "the thing we were using", "again",
)

_SINGLE_WORD_KINDS = (
    ("chrome", "application", IntentName.OPEN_APPLICATION),
    ("edge", "application", IntentName.OPEN_APPLICATION),
    ("firefox", "application", IntentName.OPEN_APPLICATION),
    ("notepad", "application", IntentName.OPEN_APPLICATION),
    ("calculator", "application", IntentName.OPEN_APPLICATION),
    ("whatsapp", "application", IntentName.PHONE_OPEN_APP),
    ("youtube", "application", IntentName.PHONE_OPEN_APP),
    ("gmail", "application", IntentName.PHONE_OPEN_APP),
    ("screenshot", "action", IntentName.TAKE_SCREENSHOT),
)

# Leading verbs the fuzzy path may correct ("opn chrme" -> "open chrome").
_VERB_VOCABULARY = (
    "open", "launch", "start", "run", "close", "quit", "exit", "check", "save",
    "write", "type", "press", "search", "research", "find", "tell", "show",
    "list", "read", "create", "make", "take", "capture", "call", "text",
    "dial", "send", "summarize", "summarise", "compare", "go", "navigate",
    "extract", "download", "upload", "remember", "recall", "plan", "schedule",
    "organize", "organise", "connect", "pair", "kill", "sort", "tidy",
)
_KNOWN_VERBS = frozenset(_VERB_VOCABULARY)

# References that name a KIND of thing ("that file") instead of the last task.
_REFERENCE_KINDS = (
    ("file", "file", IntentName.READ_FILE),
    ("app", "application", IntentName.OPEN_APPLICATION),
    ("application", "application", IntentName.OPEN_APPLICATION),
    ("folder", "folder", IntentName.OPEN_FOLDER),
    ("directory", "folder", IntentName.OPEN_FOLDER),
    ("website", "url", IntentName.NAVIGATE),
    ("site", "url", IntentName.NAVIGATE),
)

# Entity values that are placeholders and must be resolved from context.
_PLACEHOLDER_VALUES = frozenset({
    "it", "that", "this", "the same", "that one", "the thing", "same thing",
    "same", "requested app", "unknown",
})

# Multi-intent splitting: conjunctions that separate TASKS (not list items).
_SPLIT_RE = re.compile(r"\s+(?:and then|then|and also|and|after that)\s+")

_MESSAGE_SAY_RE = re.compile(r"^(?:to\s+)?(.+?)\s+(?:saying|that|about)\s+(.+)$")
_MESSAGE_COLON_RE = re.compile(r"^(.+?)\s*:\s*(.+)$")


def _split_steps(normalized: str) -> list[str]:
    parts = [part.strip(" .") for part in _SPLIT_RE.split(normalized)]
    return [part for part in parts if part]


def _starts_with_verb(part: str) -> bool:
    return bool(part) and part.split(" ", 1)[0] in _KNOWN_VERBS


def _split_message(value: str) -> dict[str, Any]:
    """Split "mom saying running late" into recipient + message."""
    match = _MESSAGE_SAY_RE.match(value)
    if match:
        return {"recipient": match.group(1).strip(), "message": match.group(2).strip()}
    match = _MESSAGE_COLON_RE.match(value)
    if match:
        return {"recipient": match.group(1).strip(), "message": match.group(2).strip()}
    return {"message": value}


class CompletionProvider(Protocol):
    async def complete(self, messages: Any, **kwargs: Any) -> str:  # pragma: no cover
        ...


@dataclass(slots=True)
class UnderstandResult:
    """The intent plus metadata about how it was understood."""

    intent: StructuredIntent
    strategy: str  # fast_path | fuzzy | learned_variant | contextual | multi_intent | semantic | clarification
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    # Non-empty when the input decomposed into several intents (multi-intent).
    intents: tuple[StructuredIntent, ...] = ()


class GlobalInputIntelligence:
    """The ONE language layer every NovaControl capability consumes."""

    def __init__(
        self,
        *,
        registry: IntentRegistry | None = None,
        completion_provider: object | None = None,
        context: InteractionContext | None = None,
    ) -> None:
        self.registry = registry or IntentRegistry()
        for rule in default_rules():
            self.registry.register(rule)
        self.context = context or InteractionContext()
        self._provider = completion_provider
        # Self-improvement feed: rolling record of how input is interpreted.
        self.telemetry = InterpretationTelemetry()
        # What NovaControl can do, by intent — the orchestrator's planning table.
        self.capabilities = CapabilityRegistry()
        for capability in default_capabilities():
            self.capabilities.register(capability)

    # -- public API -----------------------------------------------------------

    def understand(self, raw: str) -> UnderstandResult:
        raw_text = str(raw or "").strip()
        normalized = normalize(raw_text)
        if not normalized:
            self.telemetry.record_clarification(question="The request is empty.", normalized="")
            return UnderstandResult(self._clarify(raw_text, "The request is empty."), "clarification")

        # MULTI-INTENT: several imperative clauses -> decompose for the planner.
        steps = _split_steps(normalized)
        if len(steps) > 1 and sum(1 for step in steps if _starts_with_verb(step)) >= 2:
            intents = [self._understand_single(step, step).intent for step in steps]
            if all(intent.intent is not IntentName.CLARIFY for intent in intents):
                primary = intents[0].with_(
                    parameters={**intents[0].parameters, "followed_by": [i.to_dict() for i in intents[1:]]},
                )
                self.context.remember_intent(primary.to_dict())
                self.context.remember_utterance(primary.normalized_input)
                return UnderstandResult(primary, "multi_intent", intents=tuple(intents))
            # Any unresolvable clause -> fall through to whole-input handling.

        return self._understand_single(raw_text, normalized)

    def _understand_single(self, raw_text: str, normalized: str) -> UnderstandResult:
        # FAST PATH: deterministic registry match on normalized text.
        rule = self.registry.match(normalized)
        if rule is not None:
            intent = self._build_intent(raw_text, normalized, rule.intent, rule.entity, rule.confidence, "fast_path")
            intent = self._post_process(intent)
            if not intent.needs_clarification:
                self._record(intent, "fast_path")
                return UnderstandResult(intent, "fast_path")

        # FUZZY: token-level typo correction, then re-match.
        fuzzy = self._fuzzy_understand(raw_text, normalized)
        if fuzzy is not None:
            # Teach the ORIGINAL (typo) phrase — the correction is what the
            # learned variation must reproduce next time.
            self._record(fuzzy.intent, fuzzy.strategy, learned_phrase=normalized)
            return fuzzy

        # LEARNED: previously taught linguistic variations.
        variant = self.registry.variant_intent(normalized)
        if variant is not None:
            intent = self._build_intent(raw_text, normalized, variant, _DEFAULT_ENTITY_KIND.get(variant, ""), 0.85, "learned_variant")
            intent = self._post_process(intent)
            self._record(intent, "learned_variant")
            return UnderstandResult(intent, "learned_variant")

        # CONTEXTUAL: bare entity / reference / continuation input.
        contextual = self._understand_contextual(raw_text, normalized)
        if contextual is not None:
            resolved = self._post_process(contextual.intent)
            self._record(resolved, contextual.strategy)
            return UnderstandResult(resolved, contextual.strategy)

        # SEMANTIC fallback: only now consider a model.
        if self._provider is not None and not _looks_technical(normalized):
            semantic = _run_semantic(self._provider, raw_text, normalized)
            if semantic is not None:
                self._record(semantic, "semantic")
                return UnderstandResult(semantic, "semantic")

        # No confident understanding: ask ONE precise question.
        question = self._clarification_question(normalized)
        self.telemetry.record_unknown(normalized=normalized)
        self.telemetry.record_clarification(question=question, normalized=normalized)
        return UnderstandResult(self._clarify(raw_text, question), "clarification")

    def _record(self, intent: StructuredIntent, strategy: str, *, learned_phrase: str = "") -> None:
        """Feed the self-improvement telemetry (and teach the variation)."""
        taught = False
        # Learning loop: a fuzzy/contextual resolution proves the phrasing maps
        # to this intent — teach it so the next identical phrase takes the
        # fast path. Confidence must be solid before committing the lesson.
        if strategy in ("fuzzy", "contextual") and intent.confidence >= 0.7:
            self.registry.register_variation(learned_phrase or intent.normalized_input, intent.intent)
            taught = True
        self.telemetry.record_resolution(
            intent=intent.intent.value,
            strategy=strategy,
            confidence=intent.confidence,
            normalized=intent.normalized_input,
            taught=taught,
        )

    async def understand_async(self, raw: str) -> UnderstandResult:
        """Async understand for event-loop contexts: same layered flow, but the semantic fallback can await real (async) LLM providers."""
        raw_text = str(raw or "").strip()
        normalized = normalize(raw_text)
        if not normalized:
            return UnderstandResult(self._clarify(raw_text, "The request is empty."), "clarification")

        steps = _split_steps(normalized)
        if len(steps) > 1 and sum(1 for step in steps if _starts_with_verb(step)) >= 2:
            intents = [self._understand_single(step, step).intent for step in steps]
            if all(intent.intent is not IntentName.CLARIFY for intent in intents):
                primary = intents[0].with_(
                    parameters={**intents[0].parameters, "followed_by": [i.to_dict() for i in intents[1:]]},
                )
                self.context.remember_intent(primary.to_dict())
                self.context.remember_utterance(primary.normalized_input)
                return UnderstandResult(primary, "multi_intent", intents=tuple(intents))

        result = self._understand_single(raw_text, normalized)
        if result.strategy != "clarification" or self._provider is None or _looks_technical(normalized):
            return result
        # Last resort before clarifying: ask the model (async-capable).
        semantic = await _run_semantic_async(self._provider, raw_text, normalized)
        if semantic is not None:
            semantic = self._post_process(semantic)
            if not semantic.needs_clarification:
                self._record(semantic, "semantic")
                self.context.remember_intent(semantic.to_dict())
                self.context.remember_utterance(semantic.normalized_input)
                return UnderstandResult(semantic, "semantic")
        return result

    def teach_variation(self, phrase: str, intent: IntentName) -> None:
        """Record that a phrasing maps to an intent (learning loop hook)."""
        self.registry.register_variation(phrase, intent)

    # -- fuzzy path --------------------------------------------------------------

    def _fuzzy_understand(self, raw: str, normalized: str) -> UnderstandResult | None:
        """Typo-tolerant fast path: correct obvious typos and re-match.

        Skipped for technical input (URLs, paths, commands) where a "typo" may
        be meaningful.
        """
        if contains_technical_token(normalized):
            return None
        tokens = normalized_tokens(normalized)
        corrected = [tok if tok in _KNOWN_VERBS else correct_token(tok, _KNOWN_VERBS, threshold=0.8) for tok in tokens]
        if not corrected or corrected[0] not in _KNOWN_VERBS:
            return None
        candidates: list[str] = []
        joined = " ".join(corrected)
        if joined != normalized:
            candidates.append(joined)
        if corrected[0] != tokens[0]:
            verb_fixed = " ".join([corrected[0], *tokens[1:]])
            if verb_fixed not in candidates:
                candidates.append(verb_fixed)
        for phrase in candidates:
            rule = self.registry.match(phrase)
            if rule is not None:
                intent = self._build_intent(raw, phrase, rule.intent, rule.entity, min(rule.confidence, 0.72), "fuzzy")
                intent = self._post_process(intent)
                if not intent.needs_clarification:
                    return UnderstandResult(intent, "fuzzy")
        return None

    # -- fast path -------------------------------------------------------------

    def _build_intent(
        self,
        raw: str,
        normalized: str,
        intent: IntentName,
        entity_kind: str,
        confidence: float,
        source: str,
    ) -> StructuredIntent:
        entities = self._extract_entities(normalized, intent, entity_kind)
        constraints = tuple(_extract_constraints(normalized))
        return StructuredIntent(
            raw_input=raw,
            normalized_input=normalized,
            intent=intent,
            action=intent.value.split("_", 1)[0],
            target_kind=entity_kind,
            entities=entities,
            constraints=constraints,
            confidence=confidence,
            source=source,
            context=dict(self.context.environment),
        )

    def _extract_entities(self, normalized: str, intent: IntentName, entity_kind: str) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        if not entity_kind:
            return entities
        phrase = normalized
        prefix = _ENTITY_PREFIX.get(intent)
        if prefix:
            phrase = prefix.sub("", phrase, count=1).strip()
        # Drop leading verb remnants for intents with prefix-less rules.
        for verb in ("open ", "launch ", "start ", "search for ", "search "):
            if phrase.startswith(verb) and intent in (
                IntentName.OPEN_APPLICATION, IntentName.PHONE_OPEN_APP, IntentName.SEARCH_WEB,
            ):
                phrase = phrase[len(verb):]
                break
        value = _ENTITY_NOISE.sub(" ", phrase).strip()
        value = re.sub(r"\s+", " ", value).strip()
        # Typo tolerance for application names.
        if entity_kind == "application" and value:
            corrected = " ".join(correct_token(tok, _APPLICATION_VOCABULARY) for tok in value.split())
            value = corrected
        if value and value not in ("requested app",):
            entities[entity_kind] = value
        return entities

    # -- contextual / incomplete input ------------------------------------------

    def _understand_contextual(self, raw: str, normalized: str) -> UnderstandResult | None:
        tokens = normalized_tokens(normalized)

        # Kind-specific references ("that file", "that app") resolve the KIND
        # from context rather than replaying the whole last task.
        for word, kind, intent in _REFERENCE_KINDS:
            if normalized in (f"that {word}", f"this {word}", f"the {word}", f"open {word}"):
                value = self.context.last_entity(kind)
                if value:
                    resolved = StructuredIntent(
                        raw_input=raw,
                        normalized_input=normalized,
                        intent=intent,
                        action=intent.value.split("_", 1)[0],
                        target_kind=kind,
                        entities={kind: value},
                        confidence=0.78,
                        references=(normalized,),
                        source="contextual",
                        context=dict(self.context.environment),
                    )
                    return UnderstandResult(resolved, "contextual")
                return None  # nothing to resolve -> fall through to a precise question

        # Bare reference phrases: resolve via context.
        if normalized in _REFERENCE_PATTERNS or normalized in ("do the same thing", "same as before", "again", "that one"):
            replayed = self._resolve_reference(normalized)
            if replayed is not None:
                return UnderstandResult(replayed, "contextual")

        # Pending clarification answer: "earthdial" after "which repository?"
        pending = self.context.pending_intent
        if pending:
            value = " ".join(tokens)
            if value and len(tokens) <= 6:
                completed = StructuredIntent(
                    raw_input=raw,
                    normalized_input=normalized,
                    intent=IntentName(str(pending["intent"])),
                    action="provide",
                    target_kind=str(pending.get("entity_kind", "")),
                    entities={pending["entity_kind"]: value},
                    confidence=0.8,
                    source="contextual",
                    context=dict(self.context.environment),
                )
                self.context.pending_intent = None
                return UnderstandResult(completed, "contextual")

        # Bare known word: "chrome", "screenshot", "earthdial".
        if len(tokens) == 1:
            token = tokens[0]
            corrected = correct_token(token, _APPLICATION_VOCABULARY, threshold=0.75)
            if corrected != token or token in _APPLICATION_VOCABULARY:
                kind, intent = (
                    ("application", IntentName.PHONE_OPEN_APP)
                    if corrected in ("whatsapp", "youtube", "gmail")
                    else ("application", IntentName.OPEN_APPLICATION)
                )
                intent_obj = StructuredIntent(
                    raw_input=raw,
                    normalized_input=normalized,
                    intent=intent,
                    action="open",
                    target_kind=kind,
                    entities={kind: corrected},
                    confidence=0.75,
                    source="contextual",
                    context=dict(self.context.environment),
                )
                # Multiple candidate entities with this name? Ask which one.
                candidates = self.context.candidates_for(kind)
                matching = [c for c in candidates if corrected.lower() in c.lower()]
                if len(matching) > 1:
                    intent_obj = intent_obj.with_(
                        needs_clarification=True,
                        clarification_question=f"Which {kind} — {', '.join(matching[:3])}?",
                        ambiguity=tuple(matching[:3]),
                    )
                return UnderstandResult(intent_obj, "contextual")
            for word, kind, intent in _SINGLE_WORD_KINDS:
                if fuzzy_token_in(normalized, word):
                    intent_obj = StructuredIntent(
                        raw_input=raw,
                        normalized_input=normalized,
                        intent=intent,
                        action=intent.value.split("_", 1)[0],
                        target_kind=kind,
                        entities={kind: word},
                        confidence=0.7,
                        source="contextual",
                        context=dict(self.context.environment),
                    )
                    return UnderstandResult(intent_obj, "contextual")
            # Bare unknown-but-remembered entity: "earthdial" after working on it.
            for kind, values in self.context.entities.items():
                if any(v.lower() == normalized for v in values):
                    last = self.context.last_intent() or {}
                    if str(last.get("intent", "")) in _DEFAULT_ENTITY_KIND or last:
                        try:
                            intent = IntentName(str(last["intent"]))
                        except ValueError:
                            continue
                        resolved = StructuredIntent(
                            raw_input=raw,
                            normalized_input=normalized,
                            intent=intent,
                            action="continue",
                            entities=dict(last.get("entities", {})),
                            confidence=0.72,
                            references=(normalized,),
                            source="contextual",
                            context=dict(self.context.environment),
                        )
                        return UnderstandResult(resolved, "contextual")
        return None

    def _resolve_reference(self, phrase: str) -> StructuredIntent | None:
        last = self.context.last_intent()
        if last is None and not self.context.last_workflow:
            return None
        if self.context.last_workflow and phrase in ("do the same thing", "same as before", "again"):
            try:
                intent = IntentName(str(last["intent"])) if last else IntentName.AGENTIC_TASK
            except ValueError:
                intent = IntentName.AGENTIC_TASK
            return StructuredIntent(
                raw_input=phrase,
                normalized_input=phrase,
                intent=intent,
                action="repeat",
                entities=dict(last.get("entities", {})) if last else {},
                parameters={"workflow": list(self.context.last_workflow)},
                confidence=0.75,
                source="contextual",
            )
        if last:
            try:
                intent = IntentName(str(last["intent"]))
            except ValueError:
                return None
            return StructuredIntent(
                raw_input=phrase,
                normalized_input=phrase,
                intent=intent,
                action="repeat",
                entities=dict(last.get("entities", {})),
                confidence=0.7,
                source="contextual",
            )
        return None

    # -- post-processing: environment, ambiguity, risk ---------------------------

    def _post_process(self, intent: StructuredIntent) -> StructuredIntent:
        """Apply resolution, risk policy, entity requirements, and bookkeeping."""
        intent = self._resolve_entities(intent)
        # Missing required entity -> one precise clarification question.
        required = self._required_entity(intent.intent)
        if required and required not in intent.entities:
            options = self.context.candidates_for(required)
            question = self._missing_entity_question(required, options)
            intent = intent.with_(
                needs_clarification=True,
                clarification_question=question,
                ambiguity=tuple(options[:3]),
            )
            self.context.pending_intent = {"intent": intent.intent.value, "entity_kind": required}
            self.telemetry.record_failed_entity(
                intent=intent.intent.value, entity_kind=required, normalized=intent.normalized_input,
            )
            return intent
        self.context.remember_intent(intent.to_dict())
        self.context.remember_utterance(intent.normalized_input)
        return intent

    def _resolve_entities(self, intent: StructuredIntent) -> StructuredIntent:
        """Split structured payloads and resolve placeholder entities from context."""
        entities = dict(intent.entities)
        if intent.intent is IntentName.PHONE_SEND_TEXT and "message" in entities:
            entities.update(_split_message(str(entities["message"])))
        for kind, value in list(entities.items()):
            lowered = str(value).strip().lower() if isinstance(value, str) else ""
            if lowered in _PLACEHOLDER_VALUES:
                resolved = self.context.last_entity(kind)
                if not resolved:
                    resolved = self.context.environment.get(kind)  # environment-aware fallback
                if resolved:
                    entities[kind] = resolved
                    intent = intent.with_(references=(*intent.references, str(value)))
        entities = {k: v for k, v in entities.items() if v not in (None, "")}
        return intent.with_(entities=entities)

    def _missing_entity_question(self, kind: str, options: list[str]) -> str:
        label = kind.replace("_", " ")
        if options:
            return f"Which {label} — {', '.join(options[:3])}?"
        return f"What {label}?"

    @staticmethod
    def _risk_for(intent: IntentName) -> RiskLevel:
        high_risk = {
            IntentName.RUN_COMMAND, IntentName.WRITE_FILE, IntentName.PHONE_SEND_TEXT,
            IntentName.PHONE_CALL, IntentName.CLOSE_APPLICATION, IntentName.ORGANIZE_FILES,
            IntentName.FILL_FORM, IntentName.IMPROVE_SELF,
        }
        return RiskLevel.HIGH if intent in high_risk else RiskLevel.LOW

    @staticmethod
    def _required_entity(intent: IntentName) -> str:
        mapping = {
            IntentName.PHONE_SEND_TEXT: "message",
            IntentName.RUN_COMMAND: "command",
            IntentName.WRITE_FILE: "file",
            IntentName.PHONE_CALL: "contact",
            IntentName.OPEN_APPLICATION: "application",
            IntentName.CLOSE_APPLICATION: "application",
            IntentName.OPEN_FOLDER: "folder",
        }
        return mapping.get(intent, "")

    # -- clarification ------------------------------------------------------------

    def _clarification_question(self, normalized: str) -> str:
        candidates = self.context.candidates_for("application")
        if normalized in ("open it", "open the thing", "open the app", "launch it"):
            return f"Which application — {', '.join(candidates[:3])}?" if candidates else "Which application?"
        if normalized in _REFERENCE_PATTERNS or normalized in ("that file", "this file"):
            if candidates := self.context.candidates_for("file"):
                return f"Which file — {', '.join(candidates[:3])}?"
        return "What would you like me to do?"

    def _clarify(self, raw: str, question: str) -> StructuredIntent:
        return StructuredIntent(
            raw_input=raw,
            normalized_input=normalize(raw),
            intent=IntentName.CLARIFY,
            action="clarify",
            confidence=1.0,
            needs_clarification=True,
            clarification_question=question,
            source="clarification",
        )


def _looks_technical(normalized: str) -> bool:
    return bool(re.search(r"https?://|www\.|\w+\.\w{2,}|[A-Za-z]:\\", normalized))


def _extract_constraints(normalized: str) -> list[str]:
    constraints: list[str] = []
    for match in re.finditer(r"\b(without|don't|do not|never|only|must|exactly|no more than)\b([^.,;]*)", normalized, re.IGNORECASE):
        fragment = f"{match.group(1)} {match.group(2)}".strip()
        if fragment:
            constraints.append(fragment)
    return constraints


def _semantic_messages(raw: str) -> list[dict[str, str]]:
    from novacontrol.intelligence.intent import IntentName as IN

    return [
        {"role": "user", "content": (
            "Classify this user request for a computer-control assistant. Reply with ONE JSON object "
            '{"intent": "<one of: ' + ", ".join(item.value for item in IN) + '>", '
            '"entity": "<the main target or empty>", "confidence": <0..1>} and nothing else.\n\n'
            f"Request: {raw}"
        )},
    ]


def _parse_semantic(answer: str, raw: str, normalized: str) -> StructuredIntent | None:
    import json

    from novacontrol.intelligence.intent import IntentName as IN

    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if not match:
        return None
    payload = json.loads(match.group(0))
    intent = IN(str(payload.get("intent", "")))
    confidence = float(payload.get("confidence", 0.5))
    if confidence < 0.5:
        return None
    entity = str(payload.get("entity", "")).strip()
    entities = {"application": entity} if entity and intent in (
        IN.OPEN_APPLICATION, IN.CLOSE_APPLICATION, IN.PHONE_OPEN_APP,
    ) else ({payload.get("entity", "target") or "target": entity} if entity else {})
    return StructuredIntent(
        raw_input=raw,
        normalized_input=normalized,
        intent=intent,
        action=intent.value.split("_", 1)[0],
        target_kind=next(iter(entities), ""),
        entities=entities,
        confidence=min(0.9, confidence),
        source="semantic",
    )


def _run_semantic(provider: object, raw: str, normalized: str) -> StructuredIntent | None:
    """Sync LLM fallback for phrasing the deterministic layers cannot parse.

    When the provider is async (all real NovaControl providers are), this
    returns None -- the caller should use `understand_async` instead.
    """
    complete = getattr(provider, "complete", None)
    if complete is None:
        return None
    try:
        answer = complete(_semantic_messages(raw))
        if inspect.isawaitable(answer):
            # Async provider: only understand_async can await it. Close the
            # coroutine so the event loop never warns about an orphan.
            close = getattr(answer, "close", None)
            if close is not None:
                close()
            return None
        return _parse_semantic(str(answer), raw, normalized)
    except Exception:
        return None


async def _run_semantic_async(provider: object, raw: str, normalized: str) -> StructuredIntent | None:
    """Async twin of `_run_semantic`; awaits async providers (sync also OK)."""
    complete = getattr(provider, "complete", None)
    if complete is None:
        return None
    try:
        answer = complete(_semantic_messages(raw))
        if inspect.isawaitable(answer):
            answer = await answer
        return _parse_semantic(str(answer), raw, normalized)
    except Exception:
        return None
