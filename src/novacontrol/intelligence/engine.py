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
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from novacontrol.intelligence.capabilities import default_capabilities
from novacontrol.intelligence.complexity import (
    Complexity,
    ComplexityAssessment,
    assess,
    is_continuation,
    signals_for,
)
from novacontrol.intelligence.context import InteractionContext
from novacontrol.intelligence.entities import EntityExtractorRegistry, default_extractors
from novacontrol.intelligence.exemplars import default_exemplars
from novacontrol.intelligence.intent import (
    CapabilityRegistry,
    IntentName,
    IntentRegistry,
    RiskLevel,
    StructuredIntent,
)
from novacontrol.intelligence.lexical import LexicalMatcher
from novacontrol.intelligence.model_manager import ModelManager
from novacontrol.intelligence.normalize import (
    contains_technical_token,
    correct_token,
    fuzzy_token_in,
    normalize,
    normalized_tokens,
)
from novacontrol.intelligence.registry import ExecutionCategory, IntentCatalog, default_catalog
from novacontrol.intelligence.rules import default_rules
from novacontrol.intelligence.scoring import (
    ConfidenceSignals,
    ambiguity_from,
    score_confidence,
)
from novacontrol.intelligence.semantic import EmbeddingIndex
from novacontrol.intelligence.telemetry import InterpretationTelemetry, request_id_for
from novacontrol.intelligence.thresholds import NluThresholds, Route, RoutingDecision
from novacontrol.intelligence.understanding import parse_llm_output

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

# A request that must be SEEN, not merely read: an image reference plus an
# analysis verb. "take a screenshot" is an action and must not match.
_VISION_SUBJECT = re.compile(r"\b(?:screenshot|screen|image|picture|photo|camera)\b", re.IGNORECASE)
_VISION_VERB = re.compile(
    r"\b(?:look at|analyse|analyze|describe|read|explain|interpret|inspect|"
    r"tell me what|what is (?:on|in)|what's (?:on|in)|whats (?:on|in))\b",
    re.IGNORECASE,
)

# Intents answered from live sources rather than from the local model.
_WEB_INTENTS = frozenset(
    {
        IntentName.SEARCH_WEB,
        IntentName.NAVIGATE,
        IntentName.BROWSER_ACTION,
        IntentName.EXTRACT_PAGE,
        IntentName.FILL_FORM,
        IntentName.RESEARCH,
    }
)
_WEB_PHRASES = re.compile(
    r"\b(?:online|on the web|on the internet|latest|current news|right now|live)\b",
    re.IGNORECASE,
)

# Intents whose answer may depend on facts this machine does not hold. Only for
# these does a phrase like "latest" imply "go and look it up".
_KNOWLEDGE_INTENTS = frozenset(
    {
        IntentName.ANSWER_QUESTION,
        IntentName.GENERAL_QUESTION,
        IntentName.RESEARCH,
        IntentName.SUMMARIZE,
        IntentName.COMPARE,
        IntentName.GENERATE_REPORT,
        IntentName.CHAT,
        IntentName.CONVERSATION,
    }
)

# Executors that answer without touching the machine, so a request routed to
# them needs no tool at all.
_CONVERSATIONAL_EXECUTORS = frozenset({"chat_brain", "scratch_brain"})

# "open chrome and search youtube for X" is ONE browser task, not two clauses:
# split apart it becomes an app launch plus a stray search. Detected before the
# multi-step split so the composite survives.
_COMPOSITE_BROWSER = re.compile(
    r"^(?:open|launch|start)\s+(?P<app>[\w.]+)\s+(?:and|then)\s+"
    r"(?P<verb>search|google|look up|find|go to|navigate to)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
_COMPOSITE_SITES = "|".join(
    ("youtube", "google", "github", "gmail", "amazon", "reddit", "wikipedia", "netflix", "linkedin")
)
# "... search youtube for X" names the site right after the verb.
_COMPOSITE_LEAD_SITE = re.compile(
    r"^(?:for\s+)?(?:on\s+|in\s+)?(?P<site>" + _COMPOSITE_SITES + r")\b\s*",
    re.IGNORECASE,
)
# "... search for X on youtube" names it afterwards.
_COMPOSITE_TRAILING_SITE = re.compile(
    r"\b(?:on|in)\s+(?P<site>" + _COMPOSITE_SITES + r")\b",
    re.IGNORECASE,
)

# Prefixes to strip from the phrase before reading the entity.
_ENTITY_PREFIX = {
    IntentName.OPEN_APPLICATION: re.compile(r"^(?:open|launch|start|run|fire up|bring up)\s+(?:the\s+|up\s+)?"),
    IntentName.CLOSE_APPLICATION: re.compile(r"^(?:close|quit|exit|kill)\s+(?:the\s+)?"),
    IntentName.RESEARCH: re.compile(r"^(?:research|look into|dig into|find out about)\s+(?:on\s+|about\s+|the\s+)?"),
    IntentName.ANSWER_QUESTION: re.compile(r"^(?:what\s+is|what\s+are|who\s+is|who\s+was|why\s+is|why\s+are|how\s+does|how\s+do|explain|tell me about)\s+(?:the\s+)?"),
    IntentName.SEARCH_WEB: re.compile(r"^(?:google|search|look up|search for|look up for)\s+(?:the\s+|for\s+)?"),
    IntentName.NAVIGATE: re.compile(r"^(?:navigate to|go to|visit|browse to|take me to|open website|open the website|open site|open the site)\s+"),
    IntentName.PHONE_CALL: re.compile(r"^(?:call|dial|phone)\s+"),
    IntentName.OPEN_FOLDER: re.compile(r"^(?:open|show|go to)\s+(?:the\s+)?(?:folder|directory)\s+"),
    IntentName.PHONE_SEND_TEXT: re.compile(r"^(?:text|sms|send a text to|send text to|message)\s+"),
    IntentName.RUN_COMMAND: re.compile(r"^(?:run|execute)\s+(?:the\s+)?(?:script|command|following)\s*"),
    IntentName.FIND_FILE: re.compile(r"^(?:find|locate|search for|where\s+is|where's|show me|show)\s+(?:the\s+|my\s+|our\s+|a\s+)?"),
    IntentName.READ_FILE: re.compile(r"^(?:read|show|display|view|cat|print|open|launch|start)\s+(?:me\s+)?(?:the\s+|my\s+|our\s+|contents\s+of\s+)?"),
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

# Words that name no target of their own. A trailing kind word preceded ONLY
# by these is a bare reference ("open that file" -> the remembered file);
# preceded by anything else, the user NAMED something ("where is my NovaControl
# folder") and the deterministic rule table must be allowed to read it.
_KIND_NEUTRAL = frozenset(_VERB_VOCABULARY) | frozenset({
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "one",
    "my", "our", "your", "his", "her", "their", "me", "us", "them",
    "some", "any", "please", "now", "again", "is", "are", "was", "were",
    "where", "at", "of", "for", "to", "on", "in", "up", "down", "into", "back",
})

# References that name a KIND of thing ("that file") instead of the last task.
_REFERENCE_KINDS = (
    ("file", "file", IntentName.READ_FILE),
    ("app", "application", IntentName.OPEN_APPLICATION),
    ("application", "application", IntentName.OPEN_APPLICATION),
    ("folder", "folder", IntentName.OPEN_FOLDER),
    ("directory", "folder", IntentName.OPEN_FOLDER),
    # A project is a folder under another name, so "show me that project" is a
    # reference to the remembered one — not a project named "show me that".
    ("project", "project", IntentName.OPEN_FOLDER),
    ("website", "url", IntentName.NAVIGATE),
    ("site", "url", IntentName.NAVIGATE),
)

# Entity values that are placeholders and must be resolved from context.
_PLACEHOLDER_VALUES = frozenset({
    "it", "that", "this", "the same", "that one", "the thing", "same thing",
    "same", "requested app", "unknown",
})

# Multi-intent splitting: separators that join TASKS. A comma counts (that is
# how people write "open VS Code, run the tests"), and the caller's rule that
# at least two clauses must start with a verb is what keeps lists ("cats, dogs")
# from being mistaken for a sequence of tasks.
_SPLIT_RE = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band also\b|\band\b|\bafter that\b)\s*")

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
        thresholds: NluThresholds | None = None,
        entity_extractors: EntityExtractorRegistry | None = None,
        model_manager: ModelManager | None = None,
    ) -> None:
        self.registry = registry or IntentRegistry()
        for rule in default_rules():
            self.registry.register(rule)
        self.context = context or InteractionContext()
        self._provider = completion_provider
        # How confident the system must be, and what it does otherwise. Every
        # band is configurable so it can be tuned after benchmarking rather
        # than being frozen at the values that happened to be developed on.
        self.thresholds = thresholds or NluThresholds.from_environment()
        # Modular entity extraction — add an entity type, not an engine branch.
        self.entity_extractors = entity_extractors or default_extractors()
        # Model lifecycle. Held here so understanding can ask "is a model even
        # resident?" without reaching into the integration layer itself.
        self.model_manager = model_manager or ModelManager()
        # The lexical layer: TF-IDF over exemplar phrasings, the cheap middle
        # ground between exact rules and a language-model call. Building the
        # index is lazy so a process that never needs it never pays for it.
        self._lexical: LexicalMatcher | None = None
        # The semantic layer: an embedding index over the same exemplars, used
        # when the words differ. Optional by construction — with no embedder
        # supplied it is a deterministic local vector space, and the layer
        # declines entirely when semantic matching is switched off.
        self._semantic: EmbeddingIndex | None = None
        # What each layer cost on the request being handled, so "understanding
        # was slow" can be attributed to a layer instead of the whole pipeline.
        self._layer_costs: dict[str, float] = {}
        # Self-improvement feed: rolling record of how input is interpreted.
        self.telemetry = InterpretationTelemetry()
        # What NovaControl can do, by intent — the orchestrator's planning table.
        self.capabilities = CapabilityRegistry()
        for capability in default_capabilities():
            self.capabilities.register(capability)
        # The intent catalog: one declarative definition per intent (examples,
        # required entities, tool, flags, confidence floor, execution category).
        # It answers the questions the pipeline asks about an intent, so a new
        # intent is one table entry rather than a branch in three modules.
        self.catalog: IntentCatalog = default_catalog(
            capabilities=default_capabilities(), exemplars=default_exemplars()
        )

    @property
    def lexical(self) -> LexicalMatcher:
        """The exemplar index, built on first use."""
        if self._lexical is None:
            self._lexical = LexicalMatcher(default_exemplars())
        return self._lexical

    @property
    def semantic(self) -> EmbeddingIndex:
        """The embedding index, built on first use.

        Replacing the embedder is a constructor argument rather than a fork of
        this class: ``EmbeddingIndex(exemplars, embedder=SomeModelBackend())``
        is the whole change for a deployment that has embeddings available.
        """
        if self._semantic is None:
            self._semantic = EmbeddingIndex(default_exemplars(), catalog=self.catalog)
        return self._semantic

    @property
    def llm_available(self) -> bool:
        """Whether understanding may escalate to a language model at all.

        Two independent conditions: a provider must be wired AND the policy must
        permit escalation. ``NOVACONTROL_NLU_ALLOW_LLM=false`` therefore turns
        the fallback off completely — the deterministic layers keep working and
        low-confidence input asks a question instead of calling a model.
        """
        return self._provider is not None and self.thresholds.allow_llm

    # -- public API -----------------------------------------------------------

    def understand(self, raw: str) -> UnderstandResult:
        started = time.perf_counter()
        self._reset_layer_costs()
        raw_text = str(raw or "").strip()
        normalized = normalize(raw_text)
        if not normalized:
            self.telemetry.record_clarification(question="The request is empty.", normalized="")
            return UnderstandResult(self._clarify(raw_text, "The request is empty."), "clarification")

        # COMPOSITE browser work ("open chrome and search youtube for X") is ONE
        # task, and detecting it before the clause split is what keeps it from
        # degrading into an app launch plus a stray search.
        composite = self._composite_browser_action(raw_text, normalized)
        if composite is not None:
            # Post-process like every other reading. A hand-built intent must not
            # skip the step that derives its requirement flags from the catalog:
            # without it, "open chrome and search youtube for X" reported
            # ``requires_web=False`` for a web task, and the planner and the
            # approval layer would have judged it by a different description of
            # the same work than the catalog gives.
            # Its own actions are the steps: without them a two-part browser task
            # was assessed as ONE action and classified SIMPLE, so the planner it
            # needs was never named. The spec's own MODERATE example is exactly
            # this sentence.
            return self._finalize(
                self._post_process(composite),
                "composite",
                started,
                steps=max(1, len(composite.actions)),
            )

        # MULTI-INTENT: several imperative clauses -> decompose for the planner.
        steps = _split_steps(normalized)
        if len(steps) > 1 and sum(1 for step in steps if _starts_with_verb(step)) >= 2:
            multi = self._understand_multi(raw_text, normalized, steps, started)
            if multi is not None:
                return multi
            # Nothing resolved -> fall through to whole-input handling.

        return self._understand_single(raw_text, normalized, started)

    def _understand_single(
        self, raw_text: str, normalized: str, started: float | None = None
    ) -> UnderstandResult:
        """The layered pipeline for one clause: cheapest evidence first."""
        started = time.perf_counter() if started is None else started

        # REFERENCE KIND: "open that file" names a KIND, not a target. Answering
        # it from the phrase invents an application literally called "that
        # files", so the context layer resolves it (or asks which one) first.
        reference = self._reference_kind(normalized)
        if reference is not None:
            intent_name, kind = reference
            known = self.context.last_entity(kind) or self.context.environment.get(kind)
            if known:
                resolved = StructuredIntent(
                    raw_input=raw_text,
                    normalized_input=normalized,
                    intent=intent_name,
                    action=intent_name.value.split("_", 1)[0],
                    target_kind=kind,
                    entities={kind: str(known)},
                    confidence=self.thresholds.reference_confidence,
                    references=(normalized,),
                    source="contextual",
                    context=dict(self.context.environment),
                )
                return self._finalize(self._post_process(resolved), "contextual", started, learned_phrase=normalized)
            return self._clarify_result(
                raw_text,
                normalized,
                started,
                question=self._missing_entity_question(kind, self.context.candidates_for(kind)),
            )

        # FAST PATH: deterministic registry match on normalized text.
        rules_started = time.perf_counter()
        rule = self.registry.match(normalized)
        if rule is not None:
            intent = self._build_intent(raw_text, normalized, rule.intent, rule.entity, rule.confidence, "fast_path")
            intent = self._post_process(intent)
            if not intent.needs_clarification:
                self._charge("rules", rules_started)
                return self._finalize(intent, "fast_path", started)
        self._charge("rules", rules_started)

        # FUZZY: token-level typo correction, then re-match.
        fuzzy = self._fuzzy_understand(raw_text, normalized, started)
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
            return self._finalize(intent, "learned_variant", started)

        # CONTEXTUAL: bare entity / reference / continuation input.
        contextual = self._understand_contextual(raw_text, normalized)
        if contextual is not None:
            resolved = self._post_process(contextual.intent)
            return self._finalize(resolved, contextual.strategy, started, learned_phrase=normalized)

        # LEXICAL: similarity over exemplar phrasings. The cheap middle ground
        # between an exact rule and a language-model call — this is what makes
        # "which programs are eating my memory" reach the right capability.
        lexical_started = time.perf_counter()
        lexical = self._lexical_understand(raw_text, normalized, started)
        self._charge("lexical", lexical_started)
        # A lexical reading that does not trust ITSELF must not end the ladder:
        # term matching can rank an exemplar highly while the arithmetic says
        # the reading is a coin flip, and the embedding layer — which reads the
        # paraphrases term matching cannot — deserves the look. Whichever layer
        # is more confident wins, so a strong lexical match is never overruled.
        if lexical is None or lexical.intent.confidence < self.thresholds.verify_confidence:
            semantic_local = self._semantic_understand(raw_text, normalized, started)
            if semantic_local is not None and (
                lexical is None or semantic_local.intent.confidence > lexical.intent.confidence
            ):
                return semantic_local
        if lexical is not None:
            return lexical

        # MODEL: only now consider a language model.
        if self.llm_available and not _looks_technical(normalized):
            model_started = time.perf_counter()
            semantic = _run_semantic(self._provider, raw_text, normalized)
            self._charge("model", model_started)
            if semantic is not None:
                self._record(semantic, "semantic")
                return self._finalize(semantic, "semantic", started, escalated=True)

        # No confident understanding: ask ONE precise question.
        return self._clarify_result(raw_text, normalized, started)

    # -- composition, routing, and bookkeeping ---------------------------------

    def _understand_multi(
        self, raw_text: str, normalized: str, steps: list[str], started: float
    ) -> UnderstandResult | None:
        """Decompose a multi-clause request, keeping what WAS understood.

        Previously a single unparseable clause discarded the whole reading and
        collapsed the request into one misparsed intent (the entity extractor
        swallowed the rest of the sentence). Now the resolvable clauses are
        planned and the remainder is reported as unresolved, which is what
        routes the request onward instead of executing a confident mistake.
        """
        results = [self._understand_single(step, step, started) for step in steps]
        resolved = [result.intent for result in results if result.intent.intent is not IntentName.CLARIFY]
        unresolved = tuple(
            step
            for step, result in zip(steps, results, strict=False)
            if result.intent.intent is IntentName.CLARIFY
        )
        if not resolved:
            return None
        primary = resolved[0].with_(
            parameters={**resolved[0].parameters, "followed_by": [item.to_dict() for item in resolved[1:]]},
            unresolved_steps=unresolved,
            confidence=min(item.confidence for item in resolved),
            goal=normalized,
            actions=tuple(item.intent.value for item in resolved),
        )
        self.context.remember_intent(primary.to_dict())
        self.context.remember_utterance(primary.normalized_input)
        result = self._finalize(
            primary,
            "multi_intent",
            started,
            steps=len(steps),
            unresolved=len(unresolved),
        )
        return UnderstandResult(result.intent, "multi_intent", intents=tuple(resolved))

    def _composite_browser_action(self, raw_text: str, normalized: str) -> StructuredIntent | None:
        """"open chrome and search youtube for X" is ONE browser task."""
        match = _COMPOSITE_BROWSER.match(normalized)
        if match is None:
            return None
        rest = match.group("rest").strip()
        site_name = ""
        leading = _COMPOSITE_LEAD_SITE.match(rest)
        if leading is not None:
            site_name = leading.group("site").lower()
            rest = rest[leading.end():]
        trailing = _COMPOSITE_TRAILING_SITE.search(rest)
        if trailing is not None:
            site_name = site_name or trailing.group("site").lower()
            rest = _COMPOSITE_TRAILING_SITE.sub("", rest)
        query = rest.strip(" .,;:")
        for verb in ("for ", "about "):
            if query.lower().startswith(verb):
                query = query[len(verb):].strip()
        entities: dict[str, Any] = {"application": match.group("app").strip()}
        if site_name:
            entities["website"] = site_name
        if query:
            entities["query"] = query
        # The goal is the TASK, not the fragment a greedy split left behind:
        # "open chrome and search youtube for the latest ai news" used to report
        # its goal as the dangling "for the latest ai news", which reads as a
        # prepositional phrase rather than something anyone asked for.
        task = _composite_goal(match.group("verb"), site_name, query)
        return StructuredIntent(
            raw_input=raw_text,
            normalized_input=normalized,
            intent=IntentName.BROWSER_ACTION,
            action="browser",
            target_kind="query",
            entities=entities,
            goal=task,
            actions=("open_application", "navigate", "search"),
            confidence=0.9,
            source="composite",
            context=dict(self.context.environment),
        )

    def _lexical_understand(
        self, raw_text: str, normalized: str, started: float
    ) -> UnderstandResult | None:
        """Similarity match against the exemplar corpus, above a threshold."""
        if not self.thresholds.lexical_matching or contains_technical_token(normalized):
            return None
        # A short reference ("run that", "show me the thing") must be resolved
        # from context, never guessed at by similarity.
        if _is_bare_reference(normalized):
            return None
        # Ranked, not just best: the runner-up's distance is evidence about
        # whether this reading is a match or a coin flip. "close chrome" and
        # "close the chrome tab" can score almost identically against different
        # intents, and a single score cannot express that doubt.
        ranked = self.lexical.rank(normalized, limit=2)
        if not ranked:
            return None
        match = ranked[0]
        if match.score < self.thresholds.lexical_confidence:
            return None
        ambiguity, margin = ambiguity_from(tuple(item.score for item in ranked))
        score = score_confidence(
            ConfidenceSignals(
                lexical_score=match.score,
                ambiguity=ambiguity,
                candidate_margin=margin,
                context_available=bool(
                    self.context.last_intent() or self.context.active_application()
                ),
                strategy="lexical",
            )
        )
        entity_kind = _DEFAULT_ENTITY_KIND.get(match.intent, "")
        intent = self._build_intent(
            raw_text,
            normalized,
            match.intent,
            entity_kind,
            min(self.thresholds.fast_confidence, round(score.confidence, 3)),
            "lexical",
        )
        # Keep the arithmetic: why this confidence, not just what it is.
        original = intent.parameters
        intent = intent.with_(
            parameters={**original, "confidence_parts": score.to_dict()},
        )
        intent = intent.with_(parameters={**intent.parameters, "matched_exemplar": match.phrase})
        intent = self._post_process(intent)
        if intent.needs_clarification:
            return None
        return self._finalize(intent, "lexical", started, learned_phrase=normalized)

    def _semantic_understand(
        self, raw_text: str, normalized: str, started: float
    ) -> UnderstandResult | None:
        """Embedding similarity — the last cheap look before a language model.

        Lexical matching needs shared words; an embedding does not, which is how
        *"what is chewing up my ram"* reaches ``memory_status`` when no single
        term overlaps its exemplar. The score is evidence rather than proof, so
        a similarity below the configured floor is not acted on at all —
        otherwise an unrelated request would be answered by whichever exemplar
        happened to be nearest.
        """
        if not self.thresholds.semantic_matching or contains_technical_token(normalized):
            return None
        # A short reference names no target, so similarity would be a guess at
        # the KIND rather than a reading of it.
        if _is_bare_reference(normalized):
            return None
        # A continuation names no target either, and the guess here is not even
        # about a kind: similarity between "continue from where I stopped" and a
        # status exemplar is coincidence, not evidence. Measured, the nearest
        # exemplar scored 0.57 raw — above the calibrated floor — so a request
        # to resume work was answered as GPU telemetry. Only memory can say what
        # to continue, so this layer declines and lets the context resolver and,
        # if it cannot resolve it, the model do their jobs.
        if is_continuation(normalized):
            return None
        layer_started = time.perf_counter()
        ranked = self.semantic.rank(normalized, limit=2)
        self._charge("semantic", layer_started)
        if not ranked:
            return None
        match = ranked[0]
        ambiguity, margin = ambiguity_from(tuple(item.score for item in ranked))
        score = score_confidence(
            ConfidenceSignals(
                semantic_score=match.score,
                ambiguity=ambiguity,
                candidate_margin=margin,
                context_available=bool(
                    self.context.last_intent() or self.context.active_application()
                ),
                strategy="embedding",
            )
        )
        # The gate is on the CALIBRATED reading, not the raw similarity: a
        # nearest exemplar that is only nearest because the input is nonsense
        # ("show me the florb") scores well raw and calibrates to nothing, and
        # acting on that would invent an intent and swallow the "not understood"
        # signal a multi-clause request depends on.
        if score.confidence < self.thresholds.semantic_confidence:
            return None
        entity_kind = _DEFAULT_ENTITY_KIND.get(match.intent, "")
        # Source "embedding", never "semantic": the latter names the language
        # model in this system, and reporting a local vector match as a model
        # call would misdescribe what happened to anyone reading the status.
        intent = self._build_intent(
            raw_text,
            normalized,
            match.intent,
            entity_kind,
            min(self.thresholds.fast_confidence, round(score.confidence, 3)),
            "embedding",
        )
        # Keep the evidence, not just the conclusion: which candidate won, by
        # how much, and which phrasing carried it — all checkable by a human.
        intent = intent.with_(
            parameters={
                **intent.parameters,
                "confidence_parts": score.to_dict(),
                "semantic_match": match.to_dict(),
            },
        )
        intent = self._post_process(intent)
        if intent.needs_clarification:
            # A similarity is not evidence enough to ASK with: the intent it
            # guessed may not be the one the user meant, so a question built on
            # it would ask about the wrong target. Let the model, or the generic
            # clarification, do that instead.
            return None
        return self._finalize(intent, "embedding", started)

    def _finalize(
        self,
        intent: StructuredIntent,
        strategy: str,
        started: float,
        *,
        steps: int = 1,
        unresolved: int = 0,
        escalated: bool = False,
        learned_phrase: str = "",
    ) -> UnderstandResult:
        """Apply the routing policy, measure the cost, and record the outcome."""
        # Per-layer attribution travels with the reading, so a latency report
        # can say WHICH step was slow instead of only how slow the request was.
        if self._layer_costs:
            intent = intent.with_(
                parameters={**intent.parameters, "layer_ms": dict(self._layer_costs)}
            )
        # COMPLEXITY first: it is the honest answer to "how much machinery does
        # this deserve", and it is what lets a complex request reach a model
        # instead of dying in clarification.
        complexity = self._assess_complexity(intent, steps=steps, unresolved=unresolved)
        intent = self._apply_route(
            intent,
            steps=steps,
            unresolved_steps=unresolved,
            escalated=escalated,
            complexity=complexity,
        )
        intent = intent.with_(
            latency_ms=(time.perf_counter() - started) * 1000.0,
            parameters={**intent.parameters, "complexity": complexity.to_dict()},
        )
        if escalated:
            # The provider's own view of the call (load, first token, decode
            # rate) is what makes the fallback path tunable; it is absent for a
            # cloud provider, which is why it is read defensively rather than
            # assumed.
            measured = getattr(self._provider, "last_timings", None)
            self.telemetry.record_escalation(
                reason=str(intent.decision.get("reason", "")),
                normalized=intent.normalized_input,
                latency_ms=intent.latency_ms,
                timings=measured if isinstance(measured, Mapping) else None,
            )
        self._record(intent, strategy, learned_phrase=learned_phrase)
        return UnderstandResult(intent, strategy)

    def _assess_complexity(
        self, intent: StructuredIntent, *, steps: int, unresolved: int
    ) -> ComplexityAssessment:
        """Score how much machinery this request needs (see complexity.py)."""
        definition = self.catalog.get(intent.intent)
        categories: tuple[ExecutionCategory, ...] = (
            (definition.category,) if definition is not None else ()
        )
        ambiguity = float(intent.parameters.get("ambiguity", 0.0) or 0.0)
        return assess(
            signals_for(
                intent.normalized_input,
                actions=max(1, steps),
                unresolved=unresolved,
                ambiguity=ambiguity,
                categories=categories,
            )
        )

    def _apply_confidence_floor(self, intent: StructuredIntent) -> StructuredIntent:
        """Demote a shaky reading of an intent that declares a higher bar.

        The catalog's ``confidence_floor`` is per intent: a deterministic match
        clears its own rule's confidence by construction, while an action that
        closes, types or spends needs a stronger reading before it acts without
        a second look. This never blocks anything — it moves the route from
        ``fast`` to ``verify``, which is flagged, not refused.
        """
        if intent.decision.get("route") != Route.FAST.value:
            return intent
        floor = self.catalog.confidence_floor(intent.intent)
        if intent.confidence >= floor:
            return intent
        decision = RoutingDecision(
            route=Route.VERIFY,
            reason=(
                f"Understood, but below the confidence {intent.intent.value} requires "
                f"before acting without a second look ({intent.confidence:.2f} < {floor:.2f})."
            ),
            confidence=intent.confidence,
        )
        return intent.with_(decision=decision.to_dict(), requires_llm=decision.requires_llm)

    def _apply_route(
        self,
        intent: StructuredIntent,
        *,
        steps: int = 1,
        unresolved_steps: int = 0,
        escalated: bool = False,
        complexity: ComplexityAssessment | None = None,
    ) -> StructuredIntent:
        """Attach the routing decision — the auditable "why this route".

        Complexity gets the last word on one case only: a COMPLEX request that
        the confidence bands would otherwise send to a clarifying question is
        worth a model call, because clarification is a dead end while the model
        can still understand it. With no model configured the request keeps the
        question, so nothing about a model-less install changes.
        """
        if escalated:
            decision = RoutingDecision(
                route=Route.LLM,
                reason="Understood by the language model after the lightweight layers could not.",
                confidence=intent.confidence,
                requires_llm=True,
                reasoning_level="high",
            )
        else:
            decision = self.thresholds.decide(
                confidence=intent.confidence,
                requires_vision=intent.requires_vision,
                steps=steps,
                unresolved_steps=unresolved_steps,
                llm_available=self.llm_available,
                reference_resolved=bool(intent.references),
            )
            if (
                decision.route is Route.CLARIFY
                and complexity is not None
                and complexity.level is Complexity.COMPLEX
                and self.llm_available
            ):
                decision = RoutingDecision(
                    route=Route.LLM,
                    reason=(
                        "Complex request the lightweight layers could not resolve "
                        f"({complexity.reasons[0]})."
                    ),
                    confidence=intent.confidence,
                    requires_llm=True,
                    reasoning_level="high",
                )
        routed = intent.with_(
            decision=decision.to_dict(),
            requires_llm=decision.requires_llm,
            reasoning_level=decision.reasoning_level,
        )
        return self._apply_confidence_floor(routed)

    def _clarify_result(
        self,
        raw_text: str,
        normalized: str,
        started: float,
        *,
        question: str = "",
    ) -> UnderstandResult:
        """Ask ONE precise question, without a model and without guessing."""
        resolved_question = question or self._clarification_question(normalized)
        self.telemetry.record_unknown(normalized=normalized)
        intent = self._clarify(raw_text, resolved_question)
        # Derive what the request NEEDS before routing it. A request that asks
        # about an image says nothing else this layer can use — but the vision
        # route beats every other one, and a dead-end "what should I do?" would
        # hide the fact that the request is answerable by LOOKING at it. The
        # question is still the fallback; the flag is what lets a VLM take over.
        intent = self._apply_requirements(intent)
        # A clarification is an outcome too: record WHY, so the surface that
        # shows "Understanding / Reason" does not have to guess.
        intent = self._apply_route(intent.with_(confidence=0.0), steps=1)
        # Recorded WITH its route: most clarifications are route ``clarify``, but
        # one that needs an image is routed to vision, and the aggregate should
        # say where the request actually went.
        self.telemetry.record_clarification(
            question=resolved_question,
            normalized=normalized,
            route=str(intent.decision.get("route", "")),
        )
        intent = intent.with_(latency_ms=(time.perf_counter() - started) * 1000.0)
        return UnderstandResult(intent, "clarification")

    def _reference_kind(self, normalized: str) -> tuple[IntentName, str] | None:
        """Resolve "open that file" to a KIND (file/folder/site) when trailing.

        Application references are deliberately excluded: "open that app"
        already resolves through the placeholder path to the remembered
        application, and must keep doing so.
        """
        tokens = normalized.split()
        if len(tokens) > 5:
            return None
        for word, kind, intent in _REFERENCE_KINDS:
            if word in ("app", "application"):
                continue
            if not re.search(rf"\b{word}\b\s*$", normalized):
                continue
            # A named target before the kind word means this is not a bare
            # reference: "where is my NovaControl folder" has an answer.
            if any(token not in _KIND_NEUTRAL for token in tokens[:-1]):
                continue
            return intent, kind
        return None

    def _reset_layer_costs(self) -> None:
        """Start a fresh per-layer breakdown for the request being handled."""
        self._layer_costs = {}

    def _record(self, intent: StructuredIntent, strategy: str, *, learned_phrase: str = "") -> None:
        """Feed the self-improvement telemetry (and teach the variation).

        One record per request answers the operational questions without
        logging what the user said: which intent, how confident, how it was
        understood, what would be run, and what it cost.
        """
        taught = False
        # Learning loop: a fuzzy/contextual/lexical resolution proves the
        # phrasing maps to this intent — teach it so the next identical phrase
        # takes the fast path. Confidence must be solid before committing the
        # lesson, otherwise a mistake would be taught back as a rule.
        # An embedding match is deliberately NOT taught: turning a similarity
        # into a deterministic rule is a stronger claim than the evidence
        # supports, and the next identical request will match it again anyway.
        if (
            strategy in ("fuzzy", "contextual", "lexical")
            and intent.confidence >= 0.7
            # A phrase whose target came from MEMORY, or whose meaning IS "the
            # last thing", must never become a rule: the learned layer runs
            # BEFORE the context resolver, so teaching it freezes what "that" or
            # "the same thing" meant the first time it was heard. Measured —
            # "do the same thing" was taught against a file read and then
            # replayed that read after an unrelated browser task.
            and not intent.references
            and not _is_bare_reference(intent.normalized_input)
            and not is_continuation(intent.normalized_input)
        ):
            self.registry.register_variation(learned_phrase or intent.normalized_input, intent.intent)
            taught = True
        self.telemetry.record_resolution(
            intent=intent.intent.value,
            strategy=strategy,
            confidence=intent.confidence,
            normalized=intent.normalized_input,
            taught=taught,
            route=str(intent.decision.get("route", "")),
            latency_ms=intent.latency_ms,
            tool=self._selected_tool(intent),
            complexity=str(intent.parameters.get("complexity", {}).get("level", "")),
            request_id=request_id_for(intent.id),
            used_model=strategy == "semantic",
        )

    def _charge(self, layer: str, started: float) -> None:
        """Add a layer's cost to this request's breakdown.

        Attribution, not totals: the interesting question about a slow request
        is WHICH layer was slow, and a single end-to-end number cannot answer
        it. Only layers that actually ran appear, so a missing entry means "did
        not run" rather than "measured zero".
        """
        cost_ms = (time.perf_counter() - started) * 1000.0
        self._layer_costs[layer] = round(self._layer_costs.get(layer, 0.0) + cost_ms, 3)

    def _selected_tool(self, intent: StructuredIntent) -> str:
        """The executor this intent would reach, for the telemetry record."""
        definition = self.catalog.get(intent.intent)
        if definition is None or not definition.tools:
            return ""
        return definition.tools[0]

    async def understand_async(self, raw: str) -> UnderstandResult:
        """Async understand for event-loop contexts.

        Same layered flow as :meth:`understand`, but the semantic fallback can
        await a real (async) provider — which is the only place a language model
        is consulted at all, and only after every cheaper layer declined.
        """
        started = time.perf_counter()
        self._reset_layer_costs()
        raw_text = str(raw or "").strip()
        normalized = normalize(raw_text)
        if not normalized:
            return UnderstandResult(self._clarify(raw_text, "The request is empty."), "clarification")

        composite = self._composite_browser_action(raw_text, normalized)
        if composite is not None:
            # Same reason as the sync path: the catalog decides what this needs.
            # Its own actions are the steps: without them a two-part browser task
            # was assessed as ONE action and classified SIMPLE, so the planner it
            # needs was never named. The spec's own MODERATE example is exactly
            # this sentence.
            return self._finalize(
                self._post_process(composite),
                "composite",
                started,
                steps=max(1, len(composite.actions)),
            )

        steps = _split_steps(normalized)
        if len(steps) > 1 and sum(1 for step in steps if _starts_with_verb(step)) >= 2:
            multi = self._understand_multi(raw_text, normalized, steps, started)
            if multi is not None:
                return multi

        result = self._understand_single(raw_text, normalized, started)
        if result.strategy != "clarification" or not self.llm_available or _looks_technical(normalized):
            return result
        # Last resort before clarifying: ask the model (async-capable).
        semantic = await _run_semantic_async(self._provider, raw_text, normalized)
        if semantic is not None:
            semantic = self._post_process(semantic)
            if not semantic.needs_clarification:
                self.context.remember_intent(semantic.to_dict())
                self.context.remember_utterance(semantic.normalized_input)
                return self._finalize(semantic, "semantic", started, escalated=True)
        return result

    def teach_variation(self, phrase: str, intent: IntentName) -> None:
        """Record that a phrasing maps to an intent (learning loop hook)."""
        self.registry.register_variation(phrase, intent)

    # -- fuzzy path --------------------------------------------------------------

    def _fuzzy_understand(
        self, raw: str, normalized: str, started: float | None = None
    ) -> UnderstandResult | None:
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
                    return self._finalize(
                        intent,
                        "fuzzy",
                        time.perf_counter() if started is None else started,
                    )
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
        # A dedicated extractor's evidence beats the generic "rest of the
        # phrase" reading: the level extractor knows "40%" is the value and
        # "set volume to" is not, and the generic reader cannot.
        for kind, value in self.entity_extractors.extract(normalized, intent).items():
            entities[kind] = value
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
            goal=_goal_for(normalized, entities),
            actions=(intent.value,),
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
        # "open that file" must not invent an application called "that files":
        # a value that STARTS with a demonstrative names a kind, not a target.
        # Exact placeholder values ("that", "it") are kept — they are resolved
        # from context downstream, which is the behaviour to preserve.
        if value and value not in _PLACEHOLDER_VALUES and value.split(" ", 1)[0] in _REFERENCE_FIRST_WORDS:
            value = ""
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
        if normalized in _REFERENCE_PATTERNS or normalized in (
            "do the same thing",
            "same as before",
            "again",
            "that one",
        ):
            replayed = self._resolve_reference(normalized)
            if replayed is not None:
                return UnderstandResult(replayed, "contextual")

        # Bare continuation ("continue from where I stopped", "resume"). One
        # reading is available and it is the remembered one: re-run what was
        # last understood, in this conversation's context. When memory holds
        # nothing, returning None is the honest answer — the ladder then reaches
        # the model, and only then the question.
        if is_continuation(normalized):
            resumed = self._resolve_reference(normalized)
            if resumed is not None:
                return UnderstandResult(resumed, "contextual")
            return None

        # Pending clarification answer: "earthdial" after "which repository?"
        # The question's own utterance is not an answer to it: without this
        # guard, "open it" asked "Which application?" and then read itself back
        # as the answer, producing an application literally called "open it".
        pending = self.context.pending_intent
        if pending and pending.get("for") == normalized:
            pending = None
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
        intent = self._apply_requirements(intent)
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
            self.context.pending_intent = {
                "intent": intent.intent.value,
                "entity_kind": required,
                "for": intent.normalized_input,
            }
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
        # A project IS a folder, under the word the user used. The catalog asks
        # open_folder for a "folder"; mirroring the prose name keeps the entity
        # the user actually named AND satisfies the requirement, so "open my
        # NovaControl project" opens it instead of being asked for a folder.
        if intent.intent is IntentName.OPEN_FOLDER and "project" in entities and not entities.get("folder"):
            entities["folder"] = entities["project"]
        for kind, value in list(entities.items()):
            lowered = str(value).strip().lower() if isinstance(value, str) else ""
            if lowered in _PLACEHOLDER_VALUES:
                resolved = self.context.last_entity(kind)
                if not resolved:
                    resolved = self.context.environment.get(kind)  # environment-aware fallback
                if resolved:
                    entities[kind] = resolved
                    # Reading "it" from memory is evidence, not a transcription:
                    # a reference-resolved target must never look as certain as
                    # a name the user actually said.
                    intent = intent.with_(
                        references=(*intent.references, str(value)),
                        confidence=min(intent.confidence, self.thresholds.reference_confidence),
                    )
                else:
                    # Nothing to resolve it to, so the user named no target. A
                    # pronoun is not a name: keeping "it" as the application
                    # would have the planner try to launch a program called
                    # "it". Dropping it lets the required-entity check ask the
                    # precise question instead.
                    del entities[kind]
        entities = {k: v for k, v in entities.items() if v not in (None, "")}
        return intent.with_(entities=entities)

    def _apply_requirements(self, intent: StructuredIntent) -> StructuredIntent:
        """Derive what the request NEEDS — from the capability registry.

        The registry is the single source of truth for what a capability
        requires and how risky it is, so these flags cannot drift away from
        what the planner and the approval layer believe.
        """
        capability = self.capabilities.best(intent.intent)
        requires_vision = intent.intent is IntentName.SCREENSHOT_ANALYSIS or (
            bool(_VISION_SUBJECT.search(intent.normalized_input))
            and bool(_VISION_VERB.search(intent.normalized_input))
        )
        # Phrase hints ("latest", "online") only mean "consult the web" for a
        # KNOWLEDGE intent. "am I online" is a network reading, not a search.
        # The catalog also DECLARES what an intent needs; where it does, its
        # statement is added to the derived one. These flags only ever widen
        # ("needs the web", "needs a second look"), so a declaration cannot
        # strip a requirement the capability layer already established.
        definition = self.catalog.get(intent.intent)
        requires_web = intent.intent in _WEB_INTENTS or (
            intent.intent in _KNOWLEDGE_INTENTS
            and bool(_WEB_PHRASES.search(intent.normalized_input))
        ) or bool(definition is not None and definition.requires_web)
        requires_vision = requires_vision or bool(
            definition is not None and definition.requires_vision
        )
        requires_tools = bool(
            capability is not None and capability.executor not in _CONVERSATIONAL_EXECUTORS
        )
        requires_confirmation = bool(
            (capability is not None and capability.risk is not RiskLevel.LOW)
            or self._risk_for(intent.intent) in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            or (definition is not None and definition.requires_confirmation)
        )
        return intent.with_(
            requires_vision=requires_vision,
            requires_web=requires_web,
            requires_tools=requires_tools,
            requires_confirmation=requires_confirmation,
        )

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

    def _required_entity(self, intent: IntentName) -> str:
        """The entity to ask for when this intent arrives without a target.

        Two sources, in this order:

        * the positional map — the intents that are *only* meaningful with a
          target ("open ___", "run ___"), where the question is part of the
          intent's contract;
        * the catalog — every other intent, so a newly registered intent
          declares what it needs in its own table entry instead of being
          special-cased here.

        Catalog ``required_entities`` are a *dispatch* requirement, not a
        clarification trigger: ``find_file`` declares ``file`` yet is perfectly
        answerable as "find my NovaControl project". An intent opts in to being
        asked with ``clarify_when_missing``, so a new intent can require a
        target in its own table entry rather than being special-cased here.
        """
        positional = _REQUIRED_ENTITY_FALLBACK.get(intent, "")
        if positional:
            return positional
        definition = self.catalog.get(intent)
        if definition is None or not definition.clarify_when_missing:
            return ""
        return definition.required_entities[0] if definition.required_entities else ""

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


# Demonstratives that name a KIND of thing rather than a target. A value that
# STARTS with one of these is not a name the user uttered.
_REFERENCE_FIRST_WORDS = frozenset({"that", "this", "those", "these"})

# Positional entity requirements: intents whose declaration is not in the
# catalog. Never the primary source — the catalog is consulted first.
_REQUIRED_ENTITY_FALLBACK: dict[IntentName, str] = {
    IntentName.PHONE_SEND_TEXT: "message",
    IntentName.RUN_COMMAND: "command",
    IntentName.WRITE_FILE: "file",
    IntentName.PHONE_CALL: "contact",
    IntentName.OPEN_APPLICATION: "application",
    IntentName.CLOSE_APPLICATION: "application",
    IntentName.OPEN_FOLDER: "folder",
}

# An input that ENDS with a reference word is asking about something already
# known — "run that", "show me the thing", "open that file".
_REFERENCE_TAIL = re.compile(r"\b(?:it|that|this|those|these|them|thing|one)\b\s*$")
_REFERENCE_MAX_WORDS = 5


def _is_bare_reference(normalized: str) -> bool:
    """True for short inputs that point at something instead of naming it."""
    if not normalized or len(normalized.split()) > _REFERENCE_MAX_WORDS:
        return False
    return bool(_REFERENCE_TAIL.search(normalized))


def _composite_goal(verb: str, site: str, query: str) -> str:
    """The goal of a composite browser task, rebuilt from what was read out.

    The clause that follows the verb is not the goal on its own: for *"open
    chrome and search youtube for the latest ai news"* it is the dangling
    *"for the latest ai news"*. Recomposing from the recognised verb, site and
    query gives the task a reader can act on instead of a fragment.
    """
    parts = [verb.strip()]
    if site:
        parts.append(site)
    if query:
        parts.append(f"for {query}")
    return " ".join(part for part in parts if part).strip()


def _goal_for(normalized: str, entities: dict[str, Any]) -> str:
    """The distilled goal: the target when there is one, else the request.

    "search youtube for python tutorials" should report its goal as the query,
    not repeat the sentence — the goal is what the planner reasons about.
    """
    for kind in ("query", "topic", "goal", "expression", "text", "url", "file", "application"):
        value = entities.get(kind)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return normalized


def _looks_technical(normalized: str) -> bool:
    return bool(re.search(r"https?://|www\.|\w+\.\w{2,}|[A-Za-z]:\\", normalized))


def _extract_constraints(normalized: str) -> list[str]:
    constraints: list[str] = []
    for match in re.finditer(r"\b(without|don't|do not|never|only|must|exactly|no more than)\b([^.,;]*)", normalized, re.IGNORECASE):
        fragment = f"{match.group(1)} {match.group(2)}".strip()
        if fragment:
            constraints.append(fragment)
    return constraints


def _semantic_messages(raw: str, *, strict: bool = False) -> list[dict[str, str]]:
    """Ask for the SAME structured shape the deterministic layers produce.

    The model is a fallback understanding component, not an executor: it is
    asked for an intent, a goal, entities, and the requirement flags — never for
    a command, a path, or a tool name. Everything it returns is validated
    before use (see parse_llm_output).

    ``strict`` is the second attempt: same question, less room to answer it
    badly. A local model that returned prose, a code fence, or a truncated
    object is usually one instruction away from a clean answer, and one retry
    is far cheaper than giving up on the request.
    """
    from novacontrol.intelligence.intent import IntentName as IN

    intents = ", ".join(item.value for item in IN)
    preamble = (
        "Reply with ONE single line of JSON and nothing else — no prose, no markdown, "
        "no code fence, no explanation, no trailing commentary.\n\n"
        if strict
        else ""
    )
    return [
        {"role": "user", "content": (
            preamble +
            "Convert this user request for a computer-control assistant into ONE JSON object. "
            "Reply with JSON only — no prose, no markdown, no code fence — in exactly this shape:\n"
            '{"intent": "<one of: ' + intents + '>", '
            '"goal": "<what the user wants, in their words>", '
            '"entities": {"<kind>": "<value>"}, '
            '"actions": ["<step>"], '
            '"confidence": <number 0..1>, '
            '"requires_vision": <true|false>, '
            '"requires_web": <true|false>, '
            '"requires_confirmation": <true|false>}\n'
            'Use "clarify" when the request cannot be understood. Never invent a shell command, '
            "a file path, or a tool name — describe the goal and let the system choose the tool.\n\n"
            f"Request: {raw}"
        )},
    ]


def _parse_semantic(answer: str, raw: str, normalized: str) -> StructuredIntent | None:
    """Validate a model's reply into a StructuredIntent, or reject it.

    Anything that is not a valid intent object — prose, an invented intent, a
    truncated body — yields None, which sends the request to a clarifying
    question instead of a guess.
    """
    parsed = parse_llm_output(answer)
    if parsed is None:
        return None
    if parsed.confidence < 0.5:
        return None
    intent = parsed.to_structured(
        raw_input=raw,
        normalized_input=normalized,
        source="semantic",
        # A model's own confidence must never outrank a deterministic rule's.
        confidence=min(0.9, parsed.confidence),
        target_kind=parsed.target or next(iter(parsed.entities), ""),
        goal=parsed.goal or normalized,
    )
    return intent


def _run_semantic(provider: object, raw: str, normalized: str) -> StructuredIntent | None:
    """Sync LLM fallback for phrasing the deterministic layers cannot parse.

    When the provider is async (all real NovaControl providers are), this
    returns None -- the caller should use `understand_async` instead.
    """
    complete = getattr(provider, "complete", None)
    if complete is None:
        return None
    try:
        # ``json_mode`` asks a local provider for its structured path (Ollama:
        # a thinking-free endpoint), where a reasoning model cannot spend the
        # whole budget thinking and answer nothing.
        answer = complete(_semantic_messages(raw), json_mode=True)
        if inspect.isawaitable(answer):
            # Async provider: only understand_async can await it. Close the
            # coroutine so the event loop never warns about an orphan.
            close = getattr(answer, "close", None)
            if close is not None:
                close()
            return None
        parsed = _parse_semantic(str(answer), raw, normalized)
        if parsed is not None:
            return parsed
        # One stricter attempt: an unusable reply is usually a formatting
        # problem, and giving up on the request costs more than one retry.
        retry = complete(_semantic_messages(raw, strict=True), json_mode=True)
        if inspect.isawaitable(retry):
            close = getattr(retry, "close", None)
            if close is not None:
                close()
            return None
        return _parse_semantic(str(retry), raw, normalized)
    except Exception:
        return None


async def _run_semantic_async(provider: object, raw: str, normalized: str) -> StructuredIntent | None:
    """Async twin of `_run_semantic`; awaits async providers (sync also OK).

    Two attempts, the second stricter. Anything still unusable yields None,
    which the caller turns into ONE precise question — never a guessed intent.
    """
    complete = getattr(provider, "complete", None)
    if complete is None:
        return None
    for strict in (False, True):
        try:
            answer = complete(_semantic_messages(raw, strict=strict), json_mode=True)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception:
            # A failing transport on the first try is worth one more attempt;
            # a failing transport twice means the provider is down.
            if strict:
                return None
            continue
        parsed = _parse_semantic(str(answer), raw, normalized)
        if parsed is not None:
            return parsed
    return None
