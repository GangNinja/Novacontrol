"""The hybrid NLU layer: fast understanding, measured confidence, escalation.

Covers the request-understanding pipeline end to end:

  * paraphrase and typo coverage that the deterministic rules cannot hold
    (the lexical/TF-IDF layer over the exemplar corpus),
  * the confidence policy — which band a reading lands in, and what it does
    about it (nothing / verify / escalate), with every threshold configurable,
  * ambiguity and references, which must stay LOW confidence and be resolved
    from context or escalated rather than guessed,
  * multi-step requests, including the clause that cannot be understood,
  * requirement flags (vision / web / tools / confirmation) derived from the
    capability registry, so a text-only model is never handed an image,
  * the language-model fallback returning the SAME structured schema, parsed
    strictly, and never executing anything,
  * deterministic system status answered from measured telemetry with no model,
  * model lifecycle behind the replaceable backend abstraction.

Every assertion here is about observable behaviour, not internals: an intent, a
confidence band, a flag, a route, or a measured string.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.brain import NovaBrain
from novacontrol.brain.models import BrainDecision, BrainIntent, BrainRequest
from novacontrol.intelligence import (
    GlobalInputIntelligence,
    ModelManager,
    NluThresholds,
    Route,
    StructuredIntent,
    UserIntent,
    parse_llm_output,
)
from novacontrol.intelligence.entities import default_extractors
from novacontrol.intelligence.exemplars import default_exemplars, exemplar_count
from novacontrol.intelligence.rules import default_rules
from novacontrol.intelligence.intent import IntentName, RiskLevel
from novacontrol.intelligence.lexical import LexicalMatcher
from novacontrol.intelligence.thresholds import RoutingDecision
from novacontrol.intelligence.understanding import extract_json_object
from pydantic import ValidationError


def _engine(**kwargs: Any) -> GlobalInputIntelligence:
    """A deterministic-only engine (no provider) unless a test passes one."""
    return GlobalInputIntelligence(**kwargs)


class IntentVariationTests(unittest.TestCase):
    """Different ways of saying the same thing must land on the same intent."""

    def setUp(self) -> None:
        self.gil = _engine()

    def test_open_application_variations(self) -> None:
        for phrase in (
            "open chrome",
            "open chrome.",
            "Open Chrome!",
            "OPEN   CHROME",
            "launch chrome",
            "start chrome",
            "can you open chrome?",
            "could you please open chrome",
            "please bring up Chrome",
            "fire up chrome",
            "opn chrme",
        ):
            with self.subTest(phrase=phrase):
                understood = self.gil.understand(phrase)
                self.assertEqual(understood.intent.intent, IntentName.OPEN_APPLICATION)
                self.assertEqual(understood.intent.entities.get("application"), "chrome")

    def test_memory_status_variations(self) -> None:
        """"show my RAM" and "how much memory am I using" are one request."""
        for phrase in (
            "show my ram",
            "show my RAM usage",
            "how much memory am I using?",
            "how much RAM do I have?",
            "check memory usage",
            "check my memory",
            "what is consuming my RAM?",
            "what's using my ram",
            "memory usage",
            "free memory",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.gil.understand(phrase).intent.intent, IntentName.MEMORY_STATUS)

    def test_paraphrase_reaches_the_right_capability_by_similarity(self) -> None:
        """"which programs are eating my memory" shares no prefix with any rule."""
        understood = self.gil.understand("Which programs are eating my memory?")
        self.assertEqual(understood.intent.intent, IntentName.MEMORY_STATUS)
        self.assertIn(
            understood.intent.decision.get("route"),
            {Route.FAST.value, Route.VERIFY.value},
        )

    def test_status_family_is_answered_locally(self) -> None:
        cases = {
            "what's my cpu usage": IntentName.CPU_STATUS,
            "how busy is my processor": IntentName.CPU_STATUS,
            "is my battery full": IntentName.BATTERY_STATUS,
            "how much battery do i have left": IntentName.BATTERY_STATUS,
            "am I online": IntentName.NETWORK_STATUS,
            "any network issues": IntentName.NETWORK_STATUS,
            "what's my gpu doing": IntentName.GPU_STATUS,
            "how is my system doing": IntentName.SYSTEM_STATUS,
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(self.gil.understand(phrase).intent.intent, expected)

    def test_memory_question_never_routes_into_research(self) -> None:
        """\"check memory usage\" used to match the research prefix \"check \"."""
        understood = self.gil.understand("check memory usage")
        self.assertEqual(understood.intent.intent, IntentName.MEMORY_STATUS)
        self.assertNotEqual(understood.intent.intent, IntentName.RESEARCH)


class AmbiguityAndReferenceTests(unittest.TestCase):
    """Ambiguous input must be recognised as ambiguous, never guessed."""

    def setUp(self) -> None:
        self.gil = _engine()

    def test_bare_reference_without_context_asks_a_question(self) -> None:
        for phrase in ("run that", "show me the thing", "open that file"):
            with self.subTest(phrase=phrase):
                understood = self.gil.understand(phrase)
                self.assertEqual(understood.intent.intent, IntentName.CLARIFY)
                self.assertTrue(understood.intent.needs_clarification)
                self.assertTrue(understood.intent.clarification_question)

    def test_reference_resolves_from_context_at_lower_confidence(self) -> None:
        direct = self.gil.understand("open chrome")
        self.gil.context.remember_intent(direct.intent.to_dict())
        resolved = self.gil.understand("open it")
        self.assertEqual(resolved.intent.intent, IntentName.OPEN_APPLICATION)
        self.assertEqual(resolved.intent.entities.get("application"), "chrome")
        # A remembered target is evidence, not a transcription: it must never be
        # as certain as a name the user actually said.
        self.assertLess(resolved.intent.confidence, direct.intent.confidence)
        self.assertEqual(resolved.intent.decision.get("route"), Route.VERIFY.value)

    def test_reference_kind_routes_to_the_right_intent(self) -> None:
        """"open that file" must not invent an application called "that file"."""
        self.gil.context.remember_intent(
            StructuredIntent(
                raw_input="find report.pdf",
                normalized_input="find report.pdf",
                intent=IntentName.FIND_FILE,
                entities={"file": "report.pdf"},
            ).to_dict()
        )
        understood = self.gil.understand("open that file")
        self.assertEqual(understood.intent.intent, IntentName.READ_FILE)
        self.assertEqual(understood.intent.entities.get("file"), "report.pdf")

    def test_placeholder_never_becomes_an_entity_name(self) -> None:
        understood = self.gil.understand("open that file")
        self.assertNotIn(understood.intent.entities.get("application"), ("that file", "that files"))


class NamedTargetTests(unittest.TestCase):
    """A phrase that NAMES a target is not a bare reference.

    A trailing kind word ("file", "folder") used to be read as a reference to
    something remembered even when the user had just said what they meant, so
    "where is my NovaControl folder" asked a question instead of finding it, and
    "open notes.txt" was claimed as an application literally called
    "notes.txt".
    """

    def setUp(self) -> None:
        self.gil = _engine()

    def test_a_named_folder_is_found_not_referenced(self) -> None:
        understood = self.gil.understand("where is my NovaControl folder")
        self.assertEqual(understood.intent.intent, IntentName.FIND_FILE)
        self.assertEqual(understood.intent.entities.get("folder"), "novacontrol")
        self.assertFalse(understood.intent.needs_clarification)

    def test_a_named_folder_request_stays_a_folder_request(self) -> None:
        understood = self.gil.understand("open my report folder")
        self.assertEqual(understood.intent.intent, IntentName.OPEN_FOLDER)
        self.assertEqual(understood.intent.entities.get("folder"), "report")
        self.assertNotIn("application", understood.intent.entities)

    def test_content_files_are_read_rather_than_launched(self) -> None:
        for phrase in ("read notes.txt", "read my notes.txt", "open notes.txt", "view report.md"):
            with self.subTest(phrase=phrase):
                intent = self.gil.understand(phrase).intent
                self.assertEqual(intent.intent, IntentName.READ_FILE)
                self.assertTrue(intent.entities.get("file", "").endswith((".txt", ".md")))
                self.assertNotIn("application", intent.entities)

    def test_a_named_project_is_extracted_from_prose(self) -> None:
        understood = self.gil.understand("find my NovaControl project")
        self.assertEqual(understood.intent.intent, IntentName.FIND_FILE)
        self.assertEqual(understood.intent.entities.get("project"), "novacontrol")

    def test_a_bare_kind_reference_still_uses_context(self) -> None:
        """The named-target fix must not disable genuine references."""
        self.gil.context.remember_intent(
            StructuredIntent(
                raw_input="find notes.txt",
                normalized_input="find notes.txt",
                intent=IntentName.FIND_FILE,
                entities={"file": "notes.txt"},
            ).to_dict()
        )
        understood = self.gil.understand("download that file")
        self.assertEqual(understood.intent.intent, IntentName.READ_FILE)
        self.assertEqual(understood.intent.entities.get("file"), "notes.txt")


class MultiStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gil = _engine()

    def test_two_step_request_keeps_both_steps(self) -> None:
        understood = self.gil.understand("open notepad and take a screenshot")
        self.assertEqual(understood.strategy, "multi_intent")
        self.assertEqual(len(understood.intents), 2)
        self.assertEqual(
            [item.intent for item in understood.intents],
            [IntentName.OPEN_APPLICATION, IntentName.TAKE_SCREENSHOT],
        )
        self.assertEqual(understood.intent.unresolved_steps, ())

    def test_comma_separated_steps_are_decomposed(self) -> None:
        understood = self.gil.understand("open chrome, open notepad")
        self.assertEqual(understood.strategy, "multi_intent")
        self.assertEqual(len(understood.intents), 2)

    def test_three_clause_request_is_understood_without_a_model(self) -> None:
        """The spec's example must decompose, not escalate."""
        understood = self.gil.understand("Open VS Code, find my NovaControl project and run the tests.")
        self.assertEqual(understood.strategy, "multi_intent")
        self.assertEqual(
            [item.intent for item in understood.intents],
            [IntentName.OPEN_APPLICATION, IntentName.FIND_FILE, IntentName.RUN_COMMAND],
        )
        self.assertEqual(understood.intents[1].entities.get("project"), "novacontrol")
        # Every clause resolved, so no clause needs the model.
        self.assertEqual(understood.intent.unresolved_steps, ())
        self.assertFalse(understood.intent.requires_llm)
        self.assertNotEqual(understood.intent.decision.get("route"), Route.LLM.value)

    def test_unresolved_clause_escalates_instead_of_collapsing(self) -> None:
        """One unparseable clause must not swallow the whole request."""
        understood = self.gil.understand("open notepad and show me the florb")
        self.assertEqual(understood.strategy, "multi_intent")
        # What WAS understood is still planned...
        self.assertEqual(understood.intents[0].intent, IntentName.OPEN_APPLICATION)
        # ...and the remainder is reported rather than folded into the entity.
        self.assertEqual(len(understood.intent.unresolved_steps), 1)
        self.assertNotIn("florb", str(understood.intent.entities.get("application", "")))
        # Without a model available the policy asks a question, never guesses.
        self.assertEqual(understood.intent.decision.get("route"), Route.CLARIFY.value)

    def test_composite_browser_action_is_one_task(self) -> None:
        understood = self.gil.understand("open chrome and search YouTube for the latest AI news")
        intent = understood.intent
        self.assertEqual(intent.intent, IntentName.BROWSER_ACTION)
        self.assertEqual(intent.entities.get("application"), "chrome")
        self.assertEqual(intent.entities.get("website"), "youtube")
        self.assertEqual(intent.entities.get("query"), "the latest ai news")
        self.assertEqual(intent.actions, ("open_application", "navigate", "search"))

    def test_composite_detects_a_trailing_site(self) -> None:
        understood = self.gil.understand("open chrome and look up flutter tutorials on youtube")
        self.assertEqual(understood.intent.intent, IntentName.BROWSER_ACTION)
        self.assertEqual(understood.intent.entities.get("website"), "youtube")
        self.assertEqual(understood.intent.entities.get("query"), "flutter tutorials")


class EntityExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gil = _engine()

    def test_application_entity(self) -> None:
        entities = self.gil.understand("open Chrome").intent.entities
        self.assertEqual(entities.get("application"), "chrome")

    def test_file_entity(self) -> None:
        entities = self.gil.understand("find report.pdf").intent.entities
        self.assertEqual(entities.get("file"), "report.pdf")

    def test_query_and_website_entities(self) -> None:
        entities = self.gil.understand("search YouTube for Python tutorials").intent.entities
        self.assertEqual(entities.get("website"), "youtube")
        self.assertEqual(entities.get("query"), "python tutorials")

    def test_percentage_level_entity(self) -> None:
        entities = self.gil.understand("set volume to 40%").intent.entities
        self.assertEqual(entities.get("level"), 40)

    def test_percentage_is_clamped_to_a_valid_range(self) -> None:
        entities = self.gil.understand("set volume to 250%").intent.entities
        self.assertEqual(entities.get("level"), 100)

    def test_url_entity_is_kept_whole(self) -> None:
        entities = self.gil.understand("go to https://example.com/docs").intent.entities
        self.assertEqual(entities.get("url"), "https://example.com/docs")

    def test_extractors_are_modular_and_independently_registered(self) -> None:
        registry = default_extractors()
        self.assertIn("application", registry.kinds())
        self.assertIn("file", registry.kinds())
        self.assertIn("level", registry.kinds())
        # An extractor that raises must not break understanding.
        broken = registry.register(_ExplodingExtractor())
        self.assertEqual(broken.extract("open chrome", IntentName.OPEN_APPLICATION).get("application"), "chrome")


class _ExplodingExtractor:
    name = "exploding"
    kinds = ("boom",)

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        raise RuntimeError("extractor failure")


class LexicalLayerTests(unittest.TestCase):
    def test_corpus_indexes_every_exemplar(self) -> None:
        self.assertEqual(exemplar_count(), sum(len(phrases) for _intent, phrases in default_exemplars()))
        self.assertGreater(exemplar_count(), 100)

    def test_matcher_ranks_paraphrase_above_unrelated_text(self) -> None:
        matcher = LexicalMatcher(default_exemplars())
        best = matcher.best("which programs are eating my memory")
        assert best is not None
        self.assertEqual(best.intent, IntentName.MEMORY_STATUS)
        unrelated = matcher.best("write a python function to sort a list")
        assert unrelated is not None
        self.assertNotEqual(unrelated.intent, IntentName.MEMORY_STATUS)

    def test_matcher_cache_is_stable(self) -> None:
        matcher = LexicalMatcher(default_exemplars())
        first = matcher.best("show my ram")
        second = matcher.best("show my ram")
        self.assertEqual(first, second)

    def test_lexical_matching_can_be_disabled(self) -> None:
        """A phrasing only the exemplar corpus knows depends on the switches.

        TWO switches, because two layers read that corpus: TF-IDF (lexical) and
        the embedding index (semantic). Switching off one leaves the other —
        which is the point of separate switches — so the corpus-only phrasing is
        only unreadable with both off.
        """
        with_layer = _engine().understand("thanks that helped").intent.intent
        without_lexical = _engine(thresholds=NluThresholds(lexical_matching=False)).understand(
            "thanks that helped"
        ).intent.intent
        without_any = _engine(
            thresholds=NluThresholds(lexical_matching=False, semantic_matching=False)
        ).understand("thanks that helped").intent.intent
        self.assertEqual(with_layer, IntentName.CONVERSATION)
        # The embedding index still reads it: a near-exact exemplar is exactly
        # the case the semantic layer is allowed to act on.
        self.assertEqual(without_lexical, IntentName.CONVERSATION)
        self.assertEqual(without_any, IntentName.CLARIFY)


class ThresholdPolicyTests(unittest.TestCase):
    def _decide(self, thresholds: NluThresholds, **kwargs: Any) -> RoutingDecision:
        return thresholds.decide(**kwargs)

    def test_high_confidence_stays_local(self) -> None:
        decision = self._decide(NluThresholds(), confidence=0.95)
        self.assertEqual(decision.route, Route.FAST)
        self.assertFalse(decision.requires_llm)

    def test_mid_confidence_is_verified_not_escalated(self) -> None:
        decision = self._decide(NluThresholds(), confidence=0.8)
        self.assertEqual(decision.route, Route.VERIFY)
        self.assertFalse(decision.requires_llm)

    def test_low_confidence_escalates_when_a_model_exists(self) -> None:
        decision = self._decide(NluThresholds(), confidence=0.4, llm_available=True)
        self.assertEqual(decision.route, Route.LLM)
        self.assertTrue(decision.requires_llm)

    def test_low_confidence_asks_a_question_without_a_model(self) -> None:
        decision = self._decide(NluThresholds(), confidence=0.4, llm_available=False)
        self.assertEqual(decision.route, Route.CLARIFY)
        self.assertFalse(decision.requires_llm)

    def test_escalation_can_be_switched_off_entirely(self) -> None:
        decision = self._decide(NluThresholds(allow_llm=False), confidence=0.1, llm_available=True)
        self.assertEqual(decision.route, Route.CLARIFY)

    def test_vision_outranks_everything_else(self) -> None:
        decision = self._decide(NluThresholds(), confidence=0.99, requires_vision=True)
        self.assertEqual(decision.route, Route.VISION)
        self.assertFalse(decision.requires_llm)

    def test_unresolved_clauses_escalate(self) -> None:
        decision = self._decide(NluThresholds(), confidence=0.95, steps=2, unresolved_steps=1)
        self.assertEqual(decision.route, Route.LLM)

    def test_thresholds_are_configurable_from_the_environment(self) -> None:
        thresholds = NluThresholds.from_environment(
            {
                "NOVACONTROL_NLU_FAST_CONFIDENCE": "0.75",
                "NOVACONTROL_NLU_VERIFY_CONFIDENCE": "0.5",
                "NOVACONTROL_NLU_ALLOW_LLM": "false",
            }
        )
        self.assertAlmostEqual(thresholds.fast_confidence, 0.75)
        self.assertAlmostEqual(thresholds.verify_confidence, 0.5)
        self.assertFalse(thresholds.allow_llm)

    def test_bad_environment_values_fall_back_instead_of_crashing(self) -> None:
        thresholds = NluThresholds.from_environment(
            {"NOVACONTROL_NLU_FAST_CONFIDENCE": "not-a-number", "NOVACONTROL_NLU_LEXICAL_CONFIDENCE": "42"}
        )
        self.assertEqual(thresholds.fast_confidence, NluThresholds().fast_confidence)
        self.assertEqual(thresholds.lexical_confidence, NluThresholds().lexical_confidence)

    def test_inverted_bands_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            NluThresholds(fast_confidence=0.5, verify_confidence=0.9)

    def test_configurable_thresholds_change_routing(self) -> None:
        gil = _engine(thresholds=NluThresholds(fast_confidence=0.6, verify_confidence=0.4))
        # A lexical-level match would be VERIFY under defaults; with a lower fast
        # band the same reading is fully trusted.
        understood = gil.understand("Which programs are eating my memory?")
        self.assertEqual(understood.intent.decision.get("route"), Route.FAST.value)


class RequirementFlagTests(unittest.TestCase):
    """requires_* describes what the request NEEDS; it grants nothing."""

    def setUp(self) -> None:
        self.gil = _engine()

    def test_vision_analysis_is_flagged(self) -> None:
        for phrase in (
            "look at this screenshot and tell me what's wrong",
            "what is on my screen right now",
            "read the error in this screenshot",
            "describe what you see in this image",
        ):
            with self.subTest(phrase=phrase):
                intent = self.gil.understand(phrase).intent
                self.assertTrue(intent.requires_vision)
                self.assertEqual(intent.decision.get("route"), Route.VISION.value)

    def test_taking_a_screenshot_is_an_action_not_vision_analysis(self) -> None:
        intent = self.gil.understand("take a screenshot").intent
        self.assertEqual(intent.intent, IntentName.TAKE_SCREENSHOT)
        self.assertFalse(intent.requires_vision)

    def test_web_requests_are_flagged(self) -> None:
        for phrase in ("search the web for rust tutorials", "go to youtube", "research electric cars"):
            with self.subTest(phrase=phrase):
                self.assertTrue(self.gil.understand(phrase).intent.requires_web)

    def test_a_local_reading_is_not_flagged_as_web(self) -> None:
        intent = self.gil.understand("am I online").intent
        self.assertEqual(intent.intent, IntentName.NETWORK_STATUS)
        self.assertFalse(intent.requires_web)

    def test_confirmation_flag_follows_the_capability_risk_table(self) -> None:
        self.assertTrue(self.gil.understand("delete report.pdf").intent.requires_confirmation)
        self.assertTrue(self.gil.understand("close chrome").intent.requires_confirmation)
        self.assertTrue(
            self.gil.understand("run command git status").intent.requires_confirmation
        )
        # Opening an application is low risk: no confirmation demanded.
        self.assertFalse(self.gil.understand("open chrome").intent.requires_confirmation)

    def test_conversational_intents_need_no_tool(self) -> None:
        intent = self.gil.understand("what is a black hole").intent
        self.assertFalse(intent.requires_tools)

    def test_system_status_needs_the_monitor_not_a_model(self) -> None:
        intent = self.gil.understand("show my RAM").intent
        self.assertTrue(intent.requires_tools)
        self.assertFalse(intent.requires_llm)


class UserIntentContractTests(unittest.TestCase):
    """The strict boundary model: validated on the way in, never executable."""

    def test_accepts_a_fraction_confidence(self) -> None:
        intent = UserIntent(intent="open_application", confidence=0.97, entities={"application": "chrome"})
        self.assertAlmostEqual(intent.confidence, 0.97)
        self.assertEqual(intent.intent, IntentName.OPEN_APPLICATION)

    def test_reads_a_percentage_confidence_as_a_percentage(self) -> None:
        self.assertAlmostEqual(UserIntent(intent="chat", confidence=97).confidence, 0.97)

    def test_the_unknown_spellings_land_on_clarification(self) -> None:
        """The taxonomy's own "ask a question" state, not a new intent."""
        for spelling in ("", "unknown", "none", "other", "ambiguous", "clarify"):
            with self.subTest(spelling=spelling):
                self.assertEqual(UserIntent(intent=spelling).intent, IntentName.CLARIFY)

    def test_an_invented_intent_name_is_rejected_outright(self) -> None:
        """Strictness is intentional: an unknown name is a red flag, not a match."""
        with self.assertRaises(ValidationError):
            UserIntent(intent="frobnicate")
        self.assertIsNone(parse_llm_output('{"intent": "delete_everything", "confidence": 0.9}'))

    def test_out_of_range_confidence_is_clamped(self) -> None:
        self.assertEqual(UserIntent(intent="chat", confidence=-5).confidence, 0.0)
        self.assertEqual(UserIntent(intent="chat", confidence=5000).confidence, 1.0)

    def test_missing_entities_and_actions_are_tolerated(self) -> None:
        parsed = parse_llm_output('{"intent": "open_application", "confidence": 0.8}')
        assert parsed is not None
        self.assertEqual(parsed.entities, {})
        self.assertEqual(parsed.actions, [])

    def test_json_wrapped_in_prose_and_fences_is_extracted(self) -> None:
        reply = 'Sure!\n```json\n{"intent": "memory_status", "confidence": 0.9}\n```\nHope that helps.'
        parsed = parse_llm_output(reply)
        assert parsed is not None
        self.assertEqual(parsed.intent, IntentName.MEMORY_STATUS)

    def test_braces_inside_strings_do_not_truncate_the_parse(self) -> None:
        reply = '{"intent": "chat", "goal": "explain {braces} inside strings", "confidence": 0.7}'
        parsed = parse_llm_output(reply)
        assert parsed is not None
        self.assertEqual(parsed.goal, "explain {braces} inside strings")

    def test_prose_without_json_is_rejected(self) -> None:
        self.assertIsNone(parse_llm_output("I think the user wants to open a browser."))
        self.assertIsNone(parse_llm_output(""))
        self.assertIsNone(parse_llm_output('{"intent": "chat",'))

    def test_extract_json_object_ignores_a_leading_array(self) -> None:
        payload = extract_json_object('[{"intent": "chat", "confidence": 0.6}]')
        assert payload is not None
        self.assertEqual(payload["intent"], "chat")

    def test_round_trips_through_the_internal_contract(self) -> None:
        structured = StructuredIntent(
            raw_input="open chrome",
            normalized_input="open chrome",
            intent=IntentName.OPEN_APPLICATION,
            entities={"application": "chrome"},
            confidence=0.9,
            requires_tools=True,
        )
        user_intent = UserIntent.from_structured(structured)
        restored = user_intent.to_structured(raw_input="open chrome", normalized_input="open chrome")
        self.assertEqual(restored.intent, structured.intent)
        self.assertEqual(restored.entities, structured.entities)
        self.assertTrue(restored.requires_tools)

    def test_the_contract_carries_no_executable_payload(self) -> None:
        """A model understands and requests; it never names a tool or a command."""
        fields = set(UserIntent.model_fields)
        for forbidden in ("tool", "tool_name", "shell", "command", "executable", "script", "argv"):
            self.assertNotIn(forbidden, fields)


class _FakeProvider:
    """Async provider double returning a canned reply (or raising)."""

    name = "fake"
    model = "fake-model"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[Any] = []

    async def complete(self, messages: Any, **kwargs: Any) -> str:
        self.calls += 1
        self.prompts.append(messages)
        return self.reply


class LanguageModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The model is the LAST layer, and it must answer in the same schema."""

    async def test_low_confidence_escalates_and_returns_structured_output(self) -> None:
        provider = _FakeProvider(
            json.dumps(
                {
                    "intent": "memory_status",
                    "goal": "find out what is using memory",
                    "entities": {"kind": "memory"},
                    "actions": ["read_telemetry"],
                    "confidence": 0.85,
                    "requires_tools": True,
                }
            )
        )
        gil = _engine(completion_provider=provider)
        understood = await gil.understand_async("frobnicate the widget")
        self.assertEqual(understood.strategy, "semantic")
        self.assertEqual(understood.intent.intent, IntentName.MEMORY_STATUS)
        self.assertEqual(understood.intent.source, "semantic")
        self.assertEqual(provider.calls, 1)
        # The escalation is recorded as such, and the request is marked as
        # needing the model (which is what the UI reports).
        self.assertTrue(understood.intent.requires_llm)
        self.assertEqual(understood.intent.decision.get("route"), Route.LLM.value)
        self.assertGreater(understood.intent.latency_ms, 0)
        self.assertTrue(gil.telemetry.to_dict()["escalations"])

    async def test_the_model_prompt_requests_the_full_schema(self) -> None:
        provider = _FakeProvider('{"intent": "chat", "confidence": 0.9}')
        gil = _engine(completion_provider=provider)
        await gil.understand_async("frobnicate the widget")
        prompt = provider.prompts[0][0]["content"]
        for field in ("intent", "goal", "entities", "actions", "confidence", "requires_vision"):
            self.assertIn(field, prompt)
        # It is told not to invent tools or commands.
        self.assertIn("Never invent", prompt)

    async def test_a_prose_answer_is_rejected_in_favour_of_a_question(self) -> None:
        gil = _engine(completion_provider=_FakeProvider("I am not sure what you mean."))
        understood = await gil.understand_async("frobnicate the widget")
        self.assertEqual(understood.strategy, "clarification")
        self.assertTrue(understood.intent.needs_clarification)

    async def test_a_model_confidence_never_outranks_a_rule(self) -> None:
        gil = _engine(completion_provider=_FakeProvider('{"intent": "chat", "confidence": 1.0}'))
        understood = await gil.understand_async("frobnicate the widget")
        self.assertLessEqual(understood.intent.confidence, 0.9)

    async def test_deterministic_layer_prevents_the_model_from_being_called(self) -> None:
        provider = _FakeProvider('{"intent": "chat", "confidence": 0.5}')
        gil = _engine(completion_provider=provider)
        understood = await gil.understand_async("open chrome")
        self.assertEqual(understood.intent.intent, IntentName.OPEN_APPLICATION)
        self.assertEqual(provider.calls, 0, "a fast-path reading must not reach the model")

    async def test_escalation_can_be_disabled(self) -> None:
        provider = _FakeProvider('{"intent": "chat", "confidence": 0.9}')
        gil = _engine(completion_provider=provider, thresholds=NluThresholds(allow_llm=False))
        understood = await gil.understand_async("frobnicate the widget")
        self.assertEqual(understood.strategy, "clarification")
        self.assertEqual(provider.calls, 0)


class TelemetryTests(unittest.TestCase):
    def test_latency_and_route_are_recorded(self) -> None:
        gil = _engine()
        gil.understand("open chrome")
        gil.understand("show my RAM")
        payload = gil.telemetry.to_dict()
        self.assertGreaterEqual(payload["latency_ms"]["count"], 2)
        self.assertGreater(payload["latency_ms"]["avg"], 0)
        self.assertIn(Route.FAST.value, payload["routes"])

    def test_escalation_rate_is_reported(self) -> None:
        gil = _engine()
        gil.telemetry.record_escalation(reason="low confidence", normalized="x")
        gil.telemetry.record_resolution(intent="chat", strategy="fast_path", confidence=0.9)
        payload = gil.telemetry.to_dict()
        self.assertIn("escalation_rate", payload)
        self.assertEqual(payload["escalations"]["low confidence"], 1)

    def test_findings_flag_an_expensive_understanding_path(self) -> None:
        gil = _engine()
        for _ in range(4):
            gil.telemetry.record_resolution(intent="chat", strategy="semantic", confidence=0.7)
            gil.telemetry.record_escalation(reason="low confidence")
        findings = gil.telemetry.improvement_findings()
        self.assertTrue(any("language model" in finding for finding in findings), findings)


class _FakeBackend:
    """Model backend double: no server, no models on disk."""

    name = "fake"

    def __init__(self, *, resident: tuple[tuple[str, int], ...] = (), available: int | None = 8 * 1024**3) -> None:
        self._resident = resident
        self._available = available
        self.loaded: list[str] = []
        self.unloaded: list[str] = []

    def list_models(self) -> tuple[str, ...]:
        return ("chat-model", "vision-model")

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        return self._resident

    def load(self, model: str) -> bool:
        self.loaded.append(model)
        self._resident = ((model, 4 * 1024**3),)
        return True

    def unload(self, model: str) -> bool:
        self.unloaded.append(model)
        self._resident = tuple(entry for entry in self._resident if entry[0] != model)
        return True

    def available_memory_bytes(self) -> int | None:
        return self._available

    def model_size_bytes(self, model: str) -> int | None:
        return 4 * 1024**3


class ModelLifecycleTests(unittest.TestCase):
    def test_is_loaded_and_active_model(self) -> None:
        backend = _FakeBackend(resident=(("chat-model", 5 * 1024**3),))
        manager = ModelManager(backend)
        self.assertTrue(manager.is_loaded("chat-model"))
        self.assertFalse(manager.is_loaded("vision-model"))
        self.assertEqual(manager.get_active_model(), "chat-model")
        self.assertEqual(manager.get_available_memory(), 8 * 1024**3)

    def test_status_reports_reachability(self) -> None:
        manager = ModelManager(_FakeBackend(resident=(("chat-model", 1),)))
        status = manager.get_model_status().to_dict()
        self.assertEqual(status["backend"], "fake")
        self.assertEqual(status["loaded_models"], ["chat-model"])
        self.assertTrue(status["reachable"])

    def test_unreachable_backend_reports_unknown_rather_than_empty(self) -> None:
        backend = _FakeBackend()
        backend.resident_models = lambda: None  # type: ignore[method-assign]
        manager = ModelManager(backend)
        self.assertEqual(manager.get_model_status().loaded_models, ())
        self.assertFalse(manager.get_model_status().reachable)

    def test_load_evicts_the_other_model_first(self) -> None:
        backend = _FakeBackend(resident=(("chat-model", 5 * 1024**3),))
        manager = ModelManager(backend)
        result = manager.load_model("vision-model")
        self.assertTrue(result.loaded)
        self.assertIn("chat-model", result.evicted)
        self.assertEqual(backend.unloaded, ["chat-model"])

    def test_unload_all_frees_everything(self) -> None:
        backend = _FakeBackend(resident=(("a", 1), ("b", 2)))
        manager = ModelManager(backend)
        self.assertEqual(set(manager.unload_all()), {"a", "b"})
        self.assertEqual(manager.get_active_model(), "")

    def test_ensure_exclusive_keeps_the_named_model(self) -> None:
        backend = _FakeBackend(resident=(("a", 1), ("b", 2)))
        manager = ModelManager(backend)
        self.assertEqual(manager.ensure_exclusive("a"), ("b",))
        self.assertTrue(manager.is_loaded("a"))


class ApplicationRequestFlowTests(unittest.IsolatedAsyncioTestCase):
    """The real pipeline: understand → route → plan, with no model in the way."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = NovaControlApplication(data_dir=self._tmp.name)

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def test_memory_question_is_answered_from_measured_telemetry(self) -> None:
        response = await self.app.handle_request("how much RAM do I have?")
        self.assertEqual(response.route, "system")
        nlu = response.payload["nlu"]
        self.assertEqual(nlu["intent"], "memory_status")
        self.assertEqual(nlu["understanding"], "Fast NLU")
        self.assertEqual(nlu["model"], "", "no model may be used for a measured reading")
        self.assertFalse(nlu["requires_llm"])
        memory = response.payload["metrics"]["memory"]
        self.assertTrue(memory.get("available"), memory)
        # The answer states a measured figure, not a guess.
        self.assertIn("RAM", response.summary)
        self.assertTrue(response.summary.strip())

    async def test_status_intents_are_answered_locally(self) -> None:
        for phrase in ("show my cpu usage", "is my battery full", "am I online"):
            with self.subTest(phrase=phrase):
                response = await self.app.handle_request(phrase)
                self.assertEqual(response.route, "system")
                self.assertEqual(response.payload["nlu"]["model"], "")

    async def test_fast_path_command_never_reaches_the_model(self) -> None:
        response = await self.app.handle_request("open chrome")
        nlu = response.payload["nlu"]
        self.assertEqual(nlu["intent"], "open_application")
        self.assertEqual(nlu["understanding"], "Fast NLU")
        self.assertEqual(nlu["model"], "")
        self.assertFalse(nlu["requires_llm"])

    async def test_understanding_latency_is_measured_and_small(self) -> None:
        response = await self.app.handle_request("show my RAM")
        latency = response.payload["nlu"]["latency_ms"]
        self.assertGreater(latency, 0)
        self.assertLess(latency, 250, "deterministic understanding must be fast")

    async def test_the_understanding_block_exposes_no_reasoning(self) -> None:
        response = await self.app.handle_request("open chrome")
        nlu = response.payload["nlu"]
        for forbidden in ("reasoning", "chain_of_thought", "thoughts", "thinking", "raw_output"):
            self.assertNotIn(forbidden, nlu)

    async def test_malformed_requests_are_handled_without_crashing(self) -> None:
        for phrase in ("", "   ", "?!", "...", "a" * 5000, "\u0000\u200b", "🙂"):
            with self.subTest(phrase=phrase[:20]):
                response = await self.app.handle_request(phrase)
                self.assertIsInstance(response.summary, str)
                self.assertIn("nlu", response.payload)

    async def test_unknown_request_asks_rather_than_acting(self) -> None:
        understanding = self.app.intelligence.understand("frobnicate the widget")
        self.assertEqual(understanding.intent.intent, IntentName.CLARIFY)

    async def test_status_surface_exposes_thresholds_and_metrics(self) -> None:
        """"Why did that go to the model?" must be answerable from the running app."""
        intelligence = self.app.status()["intelligence"]
        self.assertIn("thresholds", intelligence)
        self.assertIn("latency_ms", intelligence["telemetry"])
        self.assertIn("routes", intelligence["telemetry"])
        self.assertGreater(intelligence["lexical_exemplars"], 0)


class CapabilityRegistryIntegrationTests(unittest.TestCase):
    """The registry stays the single source of truth the flags are derived from."""

    def test_every_new_intent_has_a_capability(self) -> None:
        gil = _engine()
        for intent in (
            IntentName.MEMORY_STATUS,
            IntentName.CPU_STATUS,
            IntentName.GPU_STATUS,
            IntentName.BATTERY_STATUS,
            IntentName.NETWORK_STATUS,
            IntentName.SYSTEM_STATUS,
            IntentName.VOLUME_CONTROL,
            IntentName.BRIGHTNESS_CONTROL,
            IntentName.MEDIA_CONTROL,
            IntentName.SCREENSHOT_ANALYSIS,
            IntentName.FIND_FILE,
            IntentName.MODIFY_FILE,
            IntentName.DELETE_FILE,
            IntentName.MOVE_FILE,
            IntentName.COPY_FILE,
            IntentName.BROWSER_ACTION,
            IntentName.CODE_GENERATION,
            IntentName.CODE_EXPLANATION,
            IntentName.CODE_DEBUGGING,
            IntentName.PROJECT_ANALYSIS,
            IntentName.CALCULATE,
            IntentName.GENERAL_QUESTION,
            IntentName.CONVERSATION,
        ):
            with self.subTest(intent=intent.value):
                capability = gil.capabilities.best(intent)
                self.assertIsNotNone(capability, f"{intent.value} has no capability")
                assert capability is not None
                self.assertTrue(capability.verifier, "every capability must declare how success is verified")

    def test_destructive_capabilities_are_risky(self) -> None:
        gil = _engine()
        self.assertEqual(gil.capabilities.best(IntentName.DELETE_FILE).risk, RiskLevel.HIGH)  # type: ignore[union-attr]
        self.assertEqual(gil.capabilities.best(IntentName.MODIFY_FILE).risk, RiskLevel.MEDIUM)  # type: ignore[union-attr]

    def test_calculate_stays_a_general_question_for_plain_arithmetic(self) -> None:
        """\"what is 15 times 3\" is the scratch brain's job, not a new intent."""
        gil = _engine()
        self.assertEqual(gil.understand("what is 15 times 3").intent.intent, IntentName.ANSWER_QUESTION)
        self.assertEqual(gil.understand("calculate 15% of 240").intent.intent, IntentName.CALCULATE)


class _CountingProvider:
    """A provider that records every call, so a test can prove ZERO calls."""

    name = "counting"
    model = "counting-1"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        return "REWORDED BY THE MODEL"


class AlreadyWordedResultTests(unittest.IsolatedAsyncioTestCase):
    """A result carrying its own sentence is never re-worded through a model.

    The fast NLU path can resolve a command in ~1 ms and still take over a
    minute end to end, because the shaping step asked a local model to
    paraphrase a sentence that already existed (measured: 86 s for
    "open chrome", 2 s once the result speaks for itself). Wording is therefore
    reserved for results that arrive with no user-facing sentence at all.
    """

    async def test_a_planned_command_is_not_rewritten(self) -> None:
        provider = _CountingProvider()
        brain = NovaBrain(completion_provider=provider)
        planned = {"summary": "Planned: Open chrome", "steps": ["Open chrome"]}
        response = await brain.shape_response(
            BrainRequest("open chrome"),
            BrainDecision(BrainIntent.DESKTOP_AUTOMATION, "test", 0.9),
            planned,
        )
        self.assertEqual(provider.calls, 0, "a plan that already reads as a sentence hit the model")
        self.assertEqual(response.summary, "Planned: Open chrome")

    async def test_a_chat_answer_is_not_reshaped(self) -> None:
        provider = _CountingProvider()
        brain = NovaBrain(completion_provider=provider)
        response = await brain.shape_response(
            BrainRequest("hi"),
            BrainDecision(BrainIntent.CHAT, "test", 0.8),
            {"message": "Hello! How can I help?"},
        )
        self.assertEqual(provider.calls, 0, "a finished answer must not be summarized again")
        self.assertEqual(response.summary, "Hello! How can I help?")

    async def test_a_measured_reading_is_returned_verbatim(self) -> None:
        provider = _CountingProvider()
        brain = NovaBrain(completion_provider=provider)
        response = await brain.shape_response(
            BrainRequest("how much RAM do I have?"),
            BrainDecision(BrainIntent.SYSTEM_STATUS, "test", 0.92),
            {"deterministic": True, "summary": "You are using 6.1 GB of 16 GB RAM."},
        )
        self.assertEqual(provider.calls, 0)
        self.assertIn("6.1 GB", response.summary)

    async def test_a_result_without_its_own_sentence_still_uses_the_model(self) -> None:
        """The capability is kept — a bare payload has nothing to say for itself."""
        provider = _CountingProvider()
        brain = NovaBrain(completion_provider=provider)
        response = await brain.shape_response(
            BrainRequest("plan my day"),
            BrainDecision(BrainIntent.PLAN, "test", 0.7),
            {"plan": {"steps": [{"description": "one"}]}},
        )
        self.assertEqual(provider.calls, 1, "a wordless payload must still be shaped")
        self.assertIn("model", response.summary.lower())


class RoutingDriftGuardTests(unittest.TestCase):
    """Every intent the NLU rules can produce must be accounted for.

    An intent with rules but no dispatcher route falls through to the legacy
    classifier. That is safe (nothing executes a capability the dispatcher does
    not know) but it means the reported understanding can name a capability
    this build cannot serve. That gap is allowed only when it is written down
    here, so adding rules for an unimplemented capability stays a deliberate
    act with a test to update — and wiring one up removes its exemption.
    """

    # Understanding shipped ahead of execution: each of these is resolved by
    # the deterministic rules, then served by the legacy classifier until its
    # executor is wired. Deleting a name here requires having wired it up.
    UNDERSTOOD_NOT_YET_DISPATCHABLE = frozenset(
        {
            "brightness_control", "check_status", "code_debugging", "code_explanation",
            "code_generation", "copy_file", "delete_file", "find_file", "list_files",
            "media_control", "modify_file", "move_file", "organize_files",
            "project_analysis", "read_file", "run_command", "system_info",
            "volume_control", "write_file",
        }
    )

    def test_every_rule_intent_is_either_dispatchable_or_documented(self) -> None:
        routes = set(NovaControlApplication._GIL_ROUTES)
        claimed = {rule.intent for rule in default_rules()}
        undocumented = {
            intent.value for intent in claimed - routes
        } - self.UNDERSTOOD_NOT_YET_DISPATCHABLE
        self.assertEqual(
            undocumented, set(),
            "these intents have rules but no route: wire them up or document them",
        )

    def test_documented_exemptions_are_real(self) -> None:
        """No stale entries: each exemption must really have rules and no route."""
        routes = set(NovaControlApplication._GIL_ROUTES)
        claimed = {rule.intent.value for rule in default_rules()}
        for name in sorted(self.UNDERSTOOD_NOT_YET_DISPATCHABLE):
            with self.subTest(intent=name):
                self.assertIn(name, claimed, "exemption has no rules — remove it")
                self.assertNotIn(name, routes, "this intent IS routed — remove the exemption")


class ToolRegistryUntouchedTests(unittest.TestCase):
    """Existing tool execution and approval behaviour must be unchanged."""

    def test_understanding_never_executes_a_tool(self) -> None:
        """A resolved intent carries no side effects — it is data."""
        gil = _engine()
        intent = gil.understand("delete report.pdf").intent
        self.assertEqual(intent.intent, IntentName.DELETE_FILE)
        self.assertTrue(intent.requires_confirmation)
        # Nothing in the produced intent can be executed directly.
        self.assertNotIn("tool", intent.parameters)
        self.assertEqual(set(intent.entities), {"file"})

    def test_high_risk_intents_are_still_confirmation_gated(self) -> None:
        gil = _engine()
        for phrase in ("delete report.pdf", "run command git status", "close chrome"):
            with self.subTest(phrase=phrase):
                self.assertTrue(gil.understand(phrase).intent.requires_confirmation)


if __name__ == "__main__":
    unittest.main()
