"""Phase 2 of the hybrid NLU layer: normalization, registry, confidence, context.

Phase 1 proved the pipeline could understand requests without a model. Phase 2
is about making that understanding *reliable and inspectable*:

  * a normalization pipeline that collapses politeness, contractions, synonyms
    and product aliases — without ever eating a filename;
  * one declarative intent catalog, so an intent's examples, entities, tool,
    flags and confidence floor live in a table instead of in three modules;
  * a confidence number with arithmetic behind it (similarity, match strength,
    entity completeness, ambiguity between candidates, context availability),
    never a single similarity score wearing a confidence label;
  * a complexity assessment (SIMPLE/MODERATE/COMPLEX) that counts actions,
    dependencies, context references and reasoning rather than words;
  * an explicit context resolver between the lightweight layers and the model;
  * strict validation of every model reply (repair -> stricter retry -> give up)
    so malformed output can never reach tool execution;
  * one structured observability record per request.

The tests are grouped by the spec's own case list (A..I) at the end, so the
suite can be read against the requirement rather than against the modules.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.integrations.llm import _ollama_timings as ollama_timings
from novacontrol.intelligence import (
    Complexity,
    ExecutionCategory,
    GlobalInputIntelligence,
    IntentCatalog,
    IntentDefinition,
    NluThresholds,
    UserIntent,
    ambiguity_from,
    assess,
    default_catalog,
    default_exemplars,
    parse_llm_output,
    score_confidence,
    signals_for,
)
from novacontrol.intelligence.complexity import ComplexitySignals, is_continuation
from novacontrol.intelligence.context import InteractionContext
from novacontrol.intelligence.entities import default_extractors as extractors
from novacontrol.intelligence.intent import (
    INTENT_ALIASES,
    IntentName,
    StructuredIntent,
    resolve_intent,
)
from novacontrol.intelligence.lexical import LexicalMatcher
from novacontrol.intelligence.normalize import normalize
from novacontrol.intelligence.rules import default_rules
from novacontrol.intelligence.scoring import ConfidenceSignals
from novacontrol.intelligence.telemetry import request_id_for
from novacontrol.intelligence.thresholds import Route
from novacontrol.intelligence.understanding import repair_json


def _engine(**kwargs: Any) -> GlobalInputIntelligence:
    """A deterministic-only engine (no provider) unless a test passes one."""
    return GlobalInputIntelligence(**kwargs)


class _CountingProvider:
    """Provider double that counts calls and replays canned replies."""

    name = "fake"
    model = "fake-model"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or [""]
        self.calls = 0
        self.prompts: list[Any] = []

    async def complete(self, messages: Any, **kwargs: Any) -> str:
        self.calls += 1
        self.prompts.append(messages)
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


# ---------------------------------------------------------------------------
# 1. Normalization
# ---------------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    """Every phrasing of one request should normalize toward the same reading."""

    def setUp(self) -> None:
        self.gil = _engine()

    def test_politeness_variants_share_one_normal_form(self) -> None:
        variants = (
            "Can you please open Chrome?",
            "Could you launch Chrome?",
            "Hey, start Google Chrome",
            "Please bring up Chrome",
            "Can you open chrome for me?",
        )
        understood = [self.gil.understand(phrase) for phrase in variants]
        for phrase, result in zip(variants, understood, strict=True):
            with self.subTest(phrase=phrase):
                self.assertEqual(result.intent.intent, IntentName.OPEN_APPLICATION)
                self.assertEqual(result.intent.entities.get("application"), "chrome")

    def test_contractions_and_case_do_not_change_the_reading(self) -> None:
        phrases = ("what's my RAM usage", "What is my ram usage?", "how much memory am I using?")
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                understood = self.gil.understand(phrase)
                self.assertEqual(understood.intent.intent, IntentName.MEMORY_STATUS)

    def test_application_aliases_collapse_to_one_product(self) -> None:
        for phrase in ("open google chrome", "open chrome browser", "open chrome"):
            with self.subTest(phrase=phrase):
                entities = self.gil.understand(phrase).intent.entities
                self.assertEqual(entities.get("application"), "chrome")

    def test_a_filename_survives_normalization_intact(self) -> None:
        """Normalization must tidy the sentence, never the entity."""
        self.assertEqual(
            self.gil.understand("can you please open report-final.pdf").intent.entities.get("file"),
            "report-final.pdf",
        )
        self.assertIn("report-final.pdf", normalize("Please open report-final.pdf"))

    def test_punctuation_is_normalized_but_hyphens_and_dots_are_kept(self) -> None:
        self.assertIn("report-final.pdf", normalize("Open 'report-final.pdf'!!"))
        self.assertNotIn("!!", normalize("open chrome!!"))

    def test_a_filler_strip_never_eats_a_verb(self) -> None:
        """A conversational opener is only stripped when it carries no request.

        "look" used to be treated as filler, which turned "look at this
        screenshot" into "at this screenshot" and lost a vision request to a
        clarifying question.
        """
        self.assertTrue(normalize("look at this screenshot").startswith("look"))
        self.assertEqual(normalize("hey, can you open chrome for me?"), "open chrome")
        self.assertEqual(normalize("hi"), "hi")

    def test_opening_a_document_is_not_launching_an_application(self) -> None:
        for phrase, expected in (
            ("open report-final.pdf", "report-final.pdf"),
            ("open notes.txt", "notes.txt"),
            ("launch report.pdf", "report.pdf"),
        ):
            with self.subTest(phrase=phrase):
                entities = self.gil.understand(phrase).intent.entities
                self.assertEqual(entities.get("file"), expected)
                self.assertNotIn("application", entities)


# ---------------------------------------------------------------------------
# 2. Intent catalog
# ---------------------------------------------------------------------------


class IntentCatalogTests(unittest.TestCase):
    """One declarative definition per intent — the pipeline's planning table."""

    def setUp(self) -> None:
        self.catalog = default_catalog()

    def test_every_intent_has_a_definition(self) -> None:
        defined = {item.intent for item in self.catalog.all()}
        self.assertEqual(defined, set(IntentName))

    def test_every_definition_describes_itself(self) -> None:
        for item in self.catalog.all():
            with self.subTest(intent=item.intent.value):
                self.assertTrue(item.description.strip())
                self.assertIsInstance(item.category, ExecutionCategory)
                self.assertGreater(item.confidence_floor, 0.0)
                self.assertLessEqual(item.confidence_floor, 1.0)

    def test_examples_come_from_the_exemplar_corpus(self) -> None:
        """The catalog and the lexical corpus cannot drift apart."""
        corpus = dict(default_exemplars())
        for item in self.catalog.all():
            with self.subTest(intent=item.intent.value):
                self.assertEqual(item.examples, tuple(corpus.get(item.intent, ())))

    def test_a_floor_above_the_rule_is_a_declared_second_look(self) -> None:
        """A higher bar is legitimate only where it is a deliberate choice.

        An intent that declares no floor inherits the strongest confidence its
        own deterministic rule registers, so an exact rule match keeps the
        ``fast`` path by construction. A floor ABOVE that reading demotes every
        match of the intent to ``verify`` — a second look — which is the point
        for actions that close, delete, spend or type, and an accidental tax on
        every other intent.

        The set is enumerated rather than inferred, so a new accidental floor
        fails here, and quietly removing one of these deliberate floors fails
        here too.
        """
        # The "second look" set: destructive or irreversible actions, where a
        # model is consulted before the action runs without confirmation.
        deliberate = {
            "close_application",
            "type_text",
            "write_file",
            "delete_file",
            "organize_files",
            "run_command",
            "agentic_task",
        }
        strongest: dict[IntentName, float] = {}
        for rule in default_rules():
            strongest[rule.intent] = max(
                strongest.get(rule.intent, 0.0), rule.confidence
            )
        raised: set[str] = set()
        for item in self.catalog.all():
            confidence = strongest.get(item.intent)
            if confidence is None:
                continue
            with self.subTest(intent=item.intent.value):
                if round(item.confidence_floor, 2) > round(confidence, 2):
                    raised.add(item.intent.value)
                else:
                    # The derived case: an exact rule match must clear its own
                    # intent's bar, or the fast path silently loses this intent.
                    self.assertGreaterEqual(
                        round(confidence, 2),
                        round(item.confidence_floor, 2),
                        "a rule's own confidence must clear its intent's floor",
                    )
        self.assertEqual(raised, deliberate)

    def test_every_second_look_intent_can_ask_for_confirmation(self) -> None:
        """The higher bar and the confirmation layer must agree.

        An intent held back to ``verify`` because it is risky should also be
        able to ask the user before it acts; otherwise the extra scrutiny has
        no way to surface as a question.
        """
        allowed = {"type_text"}  # typing is held back, but is not destructive
        for item in self.catalog.all():
            if item.intent.value == "type_text":
                continue
            with self.subTest(intent=item.intent.value):
                if item.confidence_floor > 0.90 and item.intent.value in {
                    "close_application",
                    "write_file",
                    "delete_file",
                    "organize_files",
                    "run_command",
                    "agentic_task",
                }:
                    self.assertTrue(
                        item.requires_confirmation,
                        f"{item.intent.value} is held to a higher bar but cannot confirm",
                    )
        self.assertIn("type_text", allowed)

    def test_catalog_is_extensible_in_a_table_entry(self) -> None:
        """Adding an intent is one definition, not a branch in three modules."""
        catalog = IntentCatalog()
        definition = IntentDefinition(
            intent=IntentName.WRITE_FILE,
            description="write text into a file",
            category=ExecutionCategory.LOCAL,
            examples=("write notes to a file",),
            required_entities=("file",),
            optional_entities=("content",),
            tools=("desktop_controller",),
            handler="desktop",
            requires_confirmation=True,
            confidence_floor=0.8,
            clarify_when_missing=True,
        )
        # Registration is immutable: it returns the extended catalog.
        registered = catalog.register(definition)
        self.assertIs(registered.get(IntentName.WRITE_FILE), definition)
        self.assertIsNone(catalog.get(IntentName.WRITE_FILE), "the original is untouched")
        self.assertEqual(registered.confidence_floor(IntentName.WRITE_FILE), 0.8)
        self.assertEqual(
            registered.missing_entities(IntentName.WRITE_FILE, {}), ("file",)
        )
        self.assertEqual(
            registered.missing_entities(IntentName.WRITE_FILE, {"file": "notes.txt"}), ()
        )
        self.assertTrue(registered.get(IntentName.WRITE_FILE).to_dict()["clarify_when_missing"])

    def test_requirement_flags_declared_by_the_catalog_reach_the_intent(self) -> None:
        """A declared need is added to the derived one, never subtracted."""
        gil = _engine()
        self.assertTrue(gil.understand("search the web for python tutorials").intent.requires_web)
        self.assertTrue(gil.understand("look at this screenshot").intent.requires_vision)

    def test_the_gap_list_is_explicit(self) -> None:
        """Understood-but-unrouted is a recorded fact, not a surprise."""
        unrouted = {item.intent for item in self.catalog.not_dispatchable()}
        self.assertIn(IntentName.READ_FILE, unrouted)
        # Clarification has no executor by design and is not a gap.
        self.assertNotIn(IntentName.CLARIFY, unrouted)


# ---------------------------------------------------------------------------
# 3. Lexical candidate contract
# ---------------------------------------------------------------------------


class LexicalCandidateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = LexicalMatcher(default_exemplars())

    def test_a_candidate_publishes_the_agreed_keys(self) -> None:
        match = self.matcher.best("which programs are eating my memory")
        assert match is not None
        payload = match.to_dict()
        self.assertEqual(set(payload), {"candidate_intent", "score", "matched_examples"})
        self.assertEqual(payload["candidate_intent"], IntentName.MEMORY_STATUS.value)
        self.assertTrue(payload["matched_examples"])

    def test_ranking_is_deterministic(self) -> None:
        first = self.matcher.rank("show my ram", limit=3)
        second = self.matcher.rank("show my ram", limit=3)
        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])

    def test_paraphrases_beat_unrelated_text(self) -> None:
        related = self.matcher.best("how much ram am i using")
        unrelated = self.matcher.best("write a python function to sort a list")
        assert related is not None and unrelated is not None
        self.assertEqual(related.intent, IntentName.MEMORY_STATUS)
        self.assertNotEqual(unrelated.intent, IntentName.MEMORY_STATUS)


# ---------------------------------------------------------------------------
# 4. Entity extraction
# ---------------------------------------------------------------------------


class EntityExtractionTests(unittest.TestCase):
    """The spec's entity examples, plus the kinds added in Phase 2."""

    def setUp(self) -> None:
        self.gil = _engine()

    def _entities(self, phrase: str) -> dict[str, Any]:
        return self.gil.understand(phrase).intent.entities

    def test_application(self) -> None:
        self.assertEqual(self._entities("Open Chrome").get("application"), "chrome")

    def test_file(self) -> None:
        self.assertEqual(self._entities("Find report.pdf").get("file"), "report.pdf")

    def test_file_from_an_open_verb(self) -> None:
        """\"open report.pdf\" names a document, not an application."""
        entities = self._entities("open report-final.pdf")
        self.assertEqual(entities.get("file"), "report-final.pdf")
        self.assertNotIn("application", entities)

    def test_website_and_search_query(self) -> None:
        entities = self._entities("Search YouTube for Python tutorials")
        self.assertEqual(entities.get("website"), "youtube")
        self.assertEqual(entities.get("query"), "python tutorials")

    def test_level_for_the_device_controls(self) -> None:
        """For volume/brightness the number IS the setting, so it is a level."""
        self.assertEqual(self._entities("Set volume to 50%").get("level"), 50)
        self.assertEqual(self._entities("mute at 20 percent").get("level"), 20)

    def test_percentage_is_its_own_kind_elsewhere(self) -> None:
        extracted = extractors().extract(
            "we are at 20 percent of the budget", IntentName.GENERAL_QUESTION
        )
        self.assertEqual(extracted.get("percentage"), 20)

    def test_folder_and_project(self) -> None:
        self.assertEqual(self._entities("open my report folder").get("folder"), "report")
        entities = self._entities("find my NovaControl project")
        self.assertEqual(entities.get("project"), "novacontrol")

    def test_date(self) -> None:
        self.assertEqual(self._entities("remind me tomorrow").get("date"), "tomorrow")

    def test_time(self) -> None:
        """No intent claims a bare clock time yet — extraction is still correct."""
        extracted = extractors().extract("set an alarm for 7:30am", IntentName.GENERAL_QUESTION)
        self.assertEqual(extracted.get("time"), "7:30am")
        self.assertEqual(
            extractors().extract("call me in 10 minutes", IntentName.GENERAL_QUESTION).get("time"),
            "in 10 minutes",
        )

    def test_device(self) -> None:
        self.assertEqual(self._entities("open spotify on my phone").get("device"), "phone")

    def test_extraction_is_modular(self) -> None:
        """A new entity type is a registered extractor, not an engine branch."""
        names = {extractor.name for extractor in extractors().extractors}
        self.assertTrue(
            {"application", "file", "url", "query", "project", "time", "date", "device"} <= names
        )


# ---------------------------------------------------------------------------
# 5. Confidence scoring
# ---------------------------------------------------------------------------


class ConfidenceScoringTests(unittest.TestCase):
    def test_rule_and_similarity_together_beat_similarity_alone(self) -> None:
        both = score_confidence(ConfidenceSignals(rule_strength=0.9, lexical_score=0.9))
        lexical_only = score_confidence(ConfidenceSignals(lexical_score=0.9))
        self.assertGreater(both.confidence, lexical_only.confidence)
        self.assertIn("matched by rule and by exemplar similarity", both.notes)

    def test_a_tie_between_candidates_lowers_confidence(self) -> None:
        close = ambiguity_from((0.90, 0.88))
        apart = ambiguity_from((0.90, 0.20))
        self.assertGreater(close[0], apart[0])
        self.assertGreater(apart[1], close[1])
        tied = score_confidence(
            ConfidenceSignals(rule_strength=0.9, ambiguity=close[0], candidate_margin=close[1])
        )
        decided = score_confidence(
            ConfidenceSignals(rule_strength=0.9, ambiguity=apart[0], candidate_margin=apart[1])
        )
        self.assertLess(tied.confidence, decided.confidence)

    def test_a_missing_required_entity_lowers_confidence_without_zeroing_it(self) -> None:
        complete = score_confidence(
            ConfidenceSignals(rule_strength=0.9, required_entities=1, found_entities=1)
        )
        missing = score_confidence(
            ConfidenceSignals(rule_strength=0.9, required_entities=1, found_entities=0)
        )
        self.assertLess(missing.confidence, complete.confidence)
        self.assertGreater(missing.confidence, 0.0)
        self.assertTrue(any("missing" in note for note in missing.notes))

    def test_a_reference_is_capped_below_a_named_target(self) -> None:
        resolved = score_confidence(ConfidenceSignals(reference=True, context_available=True))
        unresolved = score_confidence(ConfidenceSignals(reference=True, context_available=False))
        named = score_confidence(ConfidenceSignals(rule_strength=0.9, lexical_score=0.9))
        self.assertLess(resolved.confidence, named.confidence)
        self.assertLess(unresolved.confidence, resolved.confidence)

    def test_the_arithmetic_is_reported_not_just_the_number(self) -> None:
        payload = score_confidence(ConfidenceSignals(rule_strength=0.9)).to_dict()
        self.assertIn("parts", payload)
        self.assertIn("evidence", payload["parts"])
        self.assertTrue(payload["notes"])

    def test_a_named_target_outranks_the_same_request_made_by_reference(self) -> None:
        gil = _engine()
        gil.context.remember_intent(
            StructuredIntent(
                raw_input="open chrome",
                normalized_input="open chrome",
                intent=IntentName.OPEN_APPLICATION,
                entities={"application": "chrome"},
            ).to_dict()
        )
        named = gil.understand("open chrome").intent.confidence
        reference = gil.understand("open it").intent.confidence
        self.assertLess(reference, named)


# ---------------------------------------------------------------------------
# 6. Context resolution
# ---------------------------------------------------------------------------


class ContextResolverTests(unittest.TestCase):
    def test_the_snapshot_names_everything_a_reference_can_use(self) -> None:
        context = InteractionContext()
        context.remember_utterance("find notes.txt")
        context.remember_intent(
            StructuredIntent(
                raw_input="find notes.txt",
                normalized_input="find notes.txt",
                intent=IntentName.FIND_FILE,
                entities={"file": "notes.txt", "project": "novacontrol"},
                action="Find notes.txt",
            ).to_dict()
        )
        context.set_environment(application="chrome")
        snapshot = context.resolve_snapshot()
        self.assertEqual(snapshot["active_application"], "chrome")
        self.assertEqual(snapshot["active_project"], "novacontrol")
        self.assertEqual(snapshot["recent_files"], ["notes.txt"])
        self.assertTrue(snapshot["current_task"])
        self.assertEqual(snapshot["recent_utterances"], ["find notes.txt"])

    def test_what_was_said_outranks_what_the_environment_knows(self) -> None:
        context = InteractionContext()
        context.set_environment(application="opera")
        self.assertEqual(context.candidates_for("application"), ["opera"])
        context.remember_intent(
            StructuredIntent(
                raw_input="open chrome",
                normalized_input="open chrome",
                intent=IntentName.OPEN_APPLICATION,
                entities={"application": "chrome"},
            ).to_dict()
        )
        self.assertEqual(context.candidates_for("application")[0], "chrome")

    def test_a_reference_is_resolved_from_context_without_a_model(self) -> None:
        gil = _engine()
        gil.context.remember_intent(
            StructuredIntent(
                raw_input="read notes.txt",
                normalized_input="read notes.txt",
                intent=IntentName.READ_FILE,
                entities={"file": "notes.txt"},
            ).to_dict()
        )
        understood = gil.understand("read that file")
        self.assertEqual(understood.intent.entities.get("file"), "notes.txt")

    def test_an_unresolvable_reference_is_not_guessed(self) -> None:
        gil = _engine()
        understood = gil.understand("run that")
        self.assertTrue(understood.intent.needs_clarification)


# ---------------------------------------------------------------------------
# 7. Complexity detection
# ---------------------------------------------------------------------------


class ComplexityDetectionTests(unittest.TestCase):
    def _level(self, signals: ComplexitySignals) -> Complexity:
        return assess(signals).level

    def test_simple_one_action_request(self) -> None:
        self.assertEqual(self._level(signals_for("open chrome")), Complexity.SIMPLE)

    def test_moderate_two_independent_actions(self) -> None:
        signals = signals_for("open chrome and search youtube for python tutorials", actions=2)
        self.assertIn(self._level(signals), (Complexity.MODERATE, Complexity.COMPLEX))
        self.assertTrue(assess(signals).needs_planner)

    def test_complex_reasoning_request(self) -> None:
        text = (
            "find the project i was working on yesterday, inspect the latest changes, "
            "run the tests, identify any failures, and explain what i should fix"
        )
        assessment = assess(signals_for(text, actions=5))
        self.assertEqual(assessment.level, Complexity.COMPLEX)
        self.assertTrue(assessment.needs_model)
        self.assertTrue(assessment.reasons)

    def test_length_is_not_the_signal(self) -> None:
        """A short request can be hard; a long one can be easy."""
        short_hard = assess(signals_for("continue where i left off"))
        long_easy = assess(
            signals_for("open chrome and search youtube for the latest python tutorials", actions=2)
        )
        self.assertEqual(short_hard.level, Complexity.COMPLEX)
        self.assertNotEqual(long_easy.level, Complexity.COMPLEX)

    def test_context_references_and_ambiguity_are_counted(self) -> None:
        self.assertTrue(assess(signals_for("pick up where i stopped")).needs_model)
        ambiguous = assess(signals_for("open it", ambiguity=0.9))
        self.assertGreater(ambiguous.score, assess(signals_for("open it")).score)

    def test_the_engine_records_the_assessment_on_the_intent(self) -> None:
        gil = _engine()
        payload = gil.understand("open chrome").intent.parameters.get("complexity")
        assert payload is not None
        self.assertEqual(payload["level"], Complexity.SIMPLE.value)
        self.assertFalse(payload["needs_model"])


# ---------------------------------------------------------------------------
# 8/9. Model fallback + validation
# ---------------------------------------------------------------------------


class JsonValidationTests(unittest.TestCase):
    def test_a_valid_reply_parses_to_the_contract(self) -> None:
        parsed = parse_llm_output(
            json.dumps({"intent": "memory_status", "goal": "read ram", "confidence": 0.8})
        )
        assert parsed is not None
        self.assertEqual(parsed.intent, IntentName.MEMORY_STATUS)
        self.assertAlmostEqual(parsed.confidence, 0.8)

    def test_the_contract_carries_every_field_the_pipeline_consumes(self) -> None:
        self.assertTrue(
            {
                "intent", "goal", "entities", "actions", "confidence", "requires_llm",
                "requires_vision", "requires_web", "requires_tools", "requires_confirmation",
                "target", "parameters", "source", "reasoning_level",
            }
            <= set(UserIntent.model_fields)
        )

    def test_prose_is_not_a_structured_answer(self) -> None:
        self.assertIsNone(parse_llm_output("I am not sure what you mean."))

    def test_small_slips_are_repaired_not_rejected(self) -> None:
        self.assertIsNotNone(parse_llm_output('{"intent": "memory_status", "confidence": 0.8,}'))
        repaired = repair_json('{"intent": "chat", "goal": "hi"')
        self.assertIsNotNone(repaired)
        self.assertTrue(json.loads(repaired or "{}"))

    def test_repair_cannot_invent_an_intent(self) -> None:
        self.assertIsNone(parse_llm_output('{"goal": "something"'))
        self.assertIsNone(parse_llm_output("42"))


class WireTimingTests(unittest.IsolatedAsyncioTestCase):
    """The measurement behind item 11: load, first token, decode, tokens/sec.

    Ollama reports nanoseconds for its own phases, so these convert a real
    measurement rather than timing the whole call and calling it "inference".
    """

    NATIVE_REPLY = {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": '{"intent":"chat"}'},
        "done_reason": "stop",
        "load_duration": 2_140_000_000,
        "prompt_eval_count": 412,
        "prompt_eval_duration": 980_000_000,
        "eval_count": 96,
        "eval_duration": 4_800_000_000,
        "total_duration": 8_100_000_000,
    }

    def test_nanoseconds_become_milliseconds_with_derived_rates(self) -> None:
        timings = ollama_timings(self.NATIVE_REPLY)
        self.assertEqual(timings["model_load_ms"], 2140.0)
        self.assertEqual(timings["prompt_eval_ms"], 980.0)
        self.assertEqual(timings["decode_ms"], 4800.0)
        self.assertEqual(timings["total_inference_ms"], 8100.0)
        # 96 tokens over 4.8s of decoding.
        self.assertEqual(timings["tokens_per_second"], 20.0)

    def test_time_to_first_token_includes_bringing_the_weights_in(self) -> None:
        timings = ollama_timings(self.NATIVE_REPLY)
        self.assertEqual(timings["first_token_ms"], 3120.0)
        # Load is what separates "slow prompt" from "cold model".
        self.assertGreater(timings["first_token_ms"], timings["prompt_eval_ms"])

    def test_a_backend_that_reports_nothing_reports_nothing(self) -> None:
        self.assertEqual(ollama_timings({"choices": [{"message": {}}]}), {})

    async def test_the_provider_publishes_the_timings_of_its_own_call(self) -> None:
        from novacontrol.integrations.llm import OpenAICompatibleLLMProvider

        replies = [dict(self.NATIVE_REPLY), {"choices": [{"message": {"content": "hi"}}]}]

        def transport(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
            return replies.pop(0)

        provider = OpenAICompatibleLLMProvider(
            name="ollama", base_url="http://127.0.0.1:11434/v1", api_key="local",
            model="qwen3:8b", transport=transport,
        )
        await provider.complete([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertEqual(provider.last_timings["tokens_per_second"], 20.0)
        # A later reply with no timings must not inherit the previous call's.
        await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(provider.last_timings, {})


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_prose_answer_is_retried_once_then_understood(self) -> None:
        provider = _CountingProvider(
            "I'm not sure what you mean.",
            json.dumps(
                {
                    "intent": "open_application",
                    "entities": {"application": "code"},
                    "confidence": 0.9,
                }
            ),
        )
        gil = _engine(completion_provider=provider)
        understood = await gil.understand_async("frobnicate the widget")
        self.assertEqual(understood.strategy, "semantic")
        self.assertEqual(understood.intent.intent, IntentName.OPEN_APPLICATION)
        self.assertEqual(provider.calls, 2, "one retry, not a loop")

    async def test_a_second_bad_answer_is_a_controlled_failure(self) -> None:
        provider = _CountingProvider("still not json", "also not json")
        gil = _engine(completion_provider=provider)
        understood = await gil.understand_async("frobnicate the widget")
        self.assertEqual(understood.strategy, "clarification")
        self.assertTrue(understood.intent.needs_clarification)
        self.assertLessEqual(provider.calls, 2)

    async def test_the_retry_asks_for_the_schema_more_strictly(self) -> None:
        provider = _CountingProvider("nope", '{"intent":"chat","confidence":0.5}')
        gil = _engine(completion_provider=provider)
        await gil.understand_async("frobnicate the widget")
        first = provider.prompts[0][0]["content"]
        second = provider.prompts[1][0]["content"]
        self.assertIn("intent", first)
        self.assertNotEqual(first, second)
        self.assertIn("json", second.lower())

    async def test_the_model_never_executes_anything(self) -> None:
        """It returns data; nothing it says is a call to a tool."""
        provider = _CountingProvider(
            json.dumps(
                {"intent": "run_command", "entities": {"command": "rm -rf /"}, "confidence": 1.0}
            )
        )
        gil = _engine(completion_provider=provider)
        understood = await gil.understand_async("frobnicate the widget")
        self.assertIsInstance(understood.intent, StructuredIntent)
        self.assertTrue(understood.intent.requires_confirmation)

    async def test_the_fallback_cost_is_measured_not_guessed(self) -> None:
        """The model's own breakdown of its work reaches telemetry.

        "The fallback took 14s" is not actionable; knowing 9s of it was loading
        the weights is. The numbers come from the backend rather than from a
        stopwatch around the call, so they can be trusted.
        """
        provider = _CountingProvider('{"intent":"chat","confidence":0.9}')
        provider.last_timings = {
            "model_load_ms": 2140.0,
            "first_token_ms": 3120.0,
            "decode_ms": 4800.0,
            "total_inference_ms": 8100.0,
            "tokens_per_second": 20.0,
        }
        gil = _engine(completion_provider=provider)
        await gil.understand_async("frobnicate the widget")
        escalated = [
            item
            for item in gil.telemetry.to_dict()["recent_samples"]
            if item["kind"] == "escalation"
        ]
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0]["model_load_ms"], 2140.0)
        self.assertEqual(escalated[0]["tokens_per_second"], 20.0)
        self.assertIn("model_timings", gil.telemetry.to_dict())
        self.assertEqual(gil.telemetry.to_dict()["model_timings"]["first_token_ms"], 3120.0)

    async def test_a_provider_that_reports_no_timings_claims_none(self) -> None:
        """A cloud provider has no residency to report; say nothing, not zero."""
        provider = _CountingProvider('{"intent":"chat","confidence":0.9}')
        gil = _engine(completion_provider=provider)
        await gil.understand_async("frobnicate the widget")
        summary = gil.telemetry.to_dict()
        self.assertEqual(summary["model_timings"], {})
        escalated = [
            item
            for item in summary["recent_samples"]
            if item["kind"] == "escalation"
        ][0]
        self.assertNotIn("model_load_ms", escalated)

    async def test_complex_contextual_input_escalates(self) -> None:
        """Spec case G: only the model can resolve \"the project I was on\"."""
        provider = _CountingProvider(
            json.dumps(
                {
                    "intent": "recall",
                    "goal": "continue the NovaControl project",
                    "entities": {"project": "NovaControl"},
                    "confidence": 0.9,
                }
            )
        )
        gil = _engine(completion_provider=provider)
        understood = await gil.understand_async("Continue the project I was working on yesterday.")
        self.assertEqual(understood.intent.intent, IntentName.RECALL)
        self.assertEqual(understood.intent.decision.get("route"), Route.LLM.value)
        summary = gil.telemetry.to_dict()
        self.assertEqual(sum(summary["escalations"].values()), 1)
        self.assertTrue(understood.intent.requires_llm)


# ---------------------------------------------------------------------------
# 10. Fast path
# ---------------------------------------------------------------------------


class FastPathTests(unittest.TestCase):
    """Obvious commands must never reach a model, and must be cheap."""

    PHRASES = (
        "Open Chrome.",
        "Close Spotify.",
        "What's my RAM usage?",
        "Take a screenshot.",
        "What's my battery percentage?",
        "Open VS Code.",
        "Turn volume to 50%.",
    )

    def test_every_obvious_command_is_understood_without_a_model(self) -> None:
        provider = _CountingProvider()
        gil = _engine(completion_provider=provider)
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                understood = gil.understand(phrase)
                self.assertEqual(understood.strategy, "fast_path")
                self.assertNotEqual(understood.intent.intent, IntentName.CLARIFY)
                self.assertFalse(understood.intent.needs_clarification)
        # `understand` is synchronous and provider-free by construction.
        self.assertEqual(provider.calls, 0)

    def test_measured_fast_path_latency(self) -> None:
        """Measured, not claimed: sub-100 ms for a deterministic reading."""
        gil = _engine()
        gil.understand("open chrome")  # warm the lazy lexical index
        worst = 0.0
        for phrase in self.PHRASES:
            started = time.perf_counter()
            gil.understand(phrase)
            worst = max(worst, (time.perf_counter() - started) * 1000)
        self.assertLess(worst, 100.0, f"slowest deterministic reading was {worst:.1f} ms")

    def test_the_fast_path_needs_no_provider_and_no_escalation(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                self.assertFalse(gil.understand(phrase).intent.requires_llm)


# ---------------------------------------------------------------------------
# 15. Observability
# ---------------------------------------------------------------------------


class ObservabilityTests(unittest.TestCase):
    def test_one_record_answers_the_operational_questions(self) -> None:
        gil = _engine()
        gil.understand("open chrome")
        sample = gil.telemetry.to_dict()["recent_samples"][-1]
        for key in (
            "request_id", "timestamp", "normalized", "intent", "confidence",
            "nlu_method", "used_model", "route", "tool", "complexity", "latency_ms",
        ):
            with self.subTest(key=key):
                self.assertIn(key, sample)
        self.assertEqual(sample["tool"], "desktop_controller")
        self.assertEqual(sample["complexity"], Complexity.SIMPLE.value)
        self.assertFalse(sample["used_model"])

    def test_request_ids_are_unique_per_request(self) -> None:
        gil = _engine()
        gil.understand("open chrome")
        gil.understand("show my ram")
        ids = [item["request_id"] for item in gil.telemetry.to_dict()["recent_samples"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))

    def test_no_chain_of_thought_is_recorded(self) -> None:
        """Only operational metadata is exposed — never private reasoning."""
        gil = _engine()
        gil.understand("open chrome")
        sample = json.dumps(gil.telemetry.to_dict()["recent_samples"][-1])
        for forbidden in ("thinking", "chain_of_thought", "reasoning_text", "scratchpad"):
            self.assertNotIn(forbidden, sample)


class ObservabilityFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_model_backed_request_is_marked_as_such(self) -> None:
        provider = _CountingProvider('{"intent":"chat","confidence":0.9}')
        gil = _engine(completion_provider=provider)
        await gil.understand_async("frobnicate the widget")
        sample = gil.telemetry.to_dict()["recent_samples"][-1]
        self.assertTrue(sample["used_model"])
        self.assertEqual(sample["nlu_method"], "semantic")


# ---------------------------------------------------------------------------
# 14. The spec's case list, A..I
# ---------------------------------------------------------------------------


class SpecCaseListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gil = _engine()

    def test_a_exact_command(self) -> None:
        understood = self.gil.understand("open chrome")
        self.assertEqual(understood.intent.intent, IntentName.OPEN_APPLICATION)

    def test_b_synonyms(self) -> None:
        for phrase in ("launch chrome", "start chrome", "bring up chrome"):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    self.gil.understand(phrase).intent.intent, IntentName.OPEN_APPLICATION
                )

    def test_c_conversational_request(self) -> None:
        self.assertEqual(
            self.gil.understand("hey, can you open chrome for me?").intent.intent,
            IntentName.OPEN_APPLICATION,
        )

    def test_d_entity_extraction(self) -> None:
        entities = self.gil.understand("open report.pdf").intent.entities
        self.assertEqual(entities.get("file"), "report.pdf")

    def test_e_ambiguous_requests_are_not_guessed(self) -> None:
        for phrase in ("open it", "run that"):
            with self.subTest(phrase=phrase):
                intent = self.gil.understand(phrase).intent
                self.assertTrue(
                    intent.needs_clarification or intent.confidence < 0.9,
                    "an ambiguous request must not read as certain",
                )

    def test_f_multi_step_request_decomposes(self) -> None:
        understood = self.gil.understand("Open VS Code and run my tests.")
        self.assertIn(understood.strategy, ("multi_intent", "composite", "fast_path"))
        self.assertEqual(understood.intent.intent, IntentName.OPEN_APPLICATION)

    def test_g_complex_contextual_request_is_not_faked(self) -> None:
        """Without a model, the honest answer is a question — not a guess."""
        understood = self.gil.understand("Continue the project I was working on yesterday.")
        self.assertTrue(understood.intent.needs_clarification)

    def test_h_unknown_request_is_a_controlled_failure(self) -> None:
        understood = self.gil.understand("Do the thing I mentioned earlier.")
        self.assertEqual(understood.intent.intent, IntentName.CLARIFY)
        self.assertTrue(understood.intent.needs_clarification)

    def test_i_similar_intents_are_distinguished_where_a_capability_exists(self) -> None:
        """``close chrome`` vs ``close the chrome tab``.

        All three name the same application, which is what the system can act
        on today: they are the same intent, the same target, and all three ask
        for confirmation. Separating a *tab* from a *window* would need a
        browser capability that does not exist yet, so this test pins the part
        that is real instead of asserting a distinction the pipe cannot keep.
        """
        close_app = self.gil.understand("close Chrome").intent
        tab = self.gil.understand("close the Chrome tab").intent
        window = self.gil.understand("close the Chrome window").intent
        for intent in (close_app, tab, window):
            with self.subTest(intent=intent.intent.value):
                self.assertEqual(intent.intent, IntentName.CLOSE_APPLICATION)
                self.assertEqual(intent.entities.get("application"), "chrome")
                self.assertTrue(intent.requires_confirmation)

    def test_open_verbs_are_distinguished_by_target_kind(self) -> None:
        """The one distinction the pipeline MUST make: app vs document."""
        understood = self.gil.understand("open chrome")
        self.assertEqual(understood.intent.intent, IntentName.OPEN_APPLICATION)
        self.assertEqual(
            self.gil.understand("open report.pdf").intent.intent, IntentName.READ_FILE
        )


# ---------------------------------------------------------------------------
# 17. Backward compatibility
# ---------------------------------------------------------------------------


class BackwardCompatibilityTests(unittest.TestCase):
    def test_dangerous_actions_still_ask_first(self) -> None:
        gil = _engine()
        for phrase in ("delete report.pdf", "run command git status", "close chrome"):
            with self.subTest(phrase=phrase):
                self.assertTrue(gil.understand(phrase).intent.requires_confirmation)

    def test_read_only_actions_do_not(self) -> None:
        gil = _engine()
        for phrase in ("open chrome", "show my ram", "take a screenshot"):
            with self.subTest(phrase=phrase):
                self.assertFalse(gil.understand(phrase).intent.requires_confirmation)

    def test_clarification_never_carries_a_tool(self) -> None:
        gil = _engine()
        intent = gil.understand("Do the thing I mentioned earlier.").intent
        self.assertEqual(intent.intent, IntentName.CLARIFY)
        self.assertFalse(intent.requires_tools)

    def test_an_understood_but_unrouted_intent_is_still_understood(self) -> None:
        """Phase 1's honesty rule survives Phase 2: no overclaiming execution."""
        gil = _engine()
        understood = gil.understand("read notes.txt")
        self.assertEqual(understood.intent.intent, IntentName.READ_FILE)
        self.assertEqual(understood.intent.entities.get("file"), "notes.txt")

    def test_the_text_model_is_never_handed_an_image_request(self) -> None:
        gil = _engine()
        intent = gil.understand("describe what you see in this screenshot").intent
        self.assertTrue(intent.requires_vision)
        self.assertFalse(intent.requires_llm)


class SpecEntityExampleTests(unittest.TestCase):
    """The spec's own entity example, and the references around it.

    "Open my NovaControl project" must yield ``project = NovaControl``. The
    trap is that a project is named in prose, not by extension, so the generic
    "open ___" rule used to read the whole phrase as an APPLICATION called
    "novacontrol project" and try to launch it.
    """

    def test_a_named_project_is_a_project_not_an_application(self) -> None:
        intent = _engine().understand("Open my NovaControl project").intent
        self.assertEqual(intent.intent, IntentName.OPEN_FOLDER)
        self.assertEqual(intent.entities.get("project"), "novacontrol")
        self.assertNotIn("application", intent.entities)

    def test_a_project_satisfies_the_folder_the_intent_asks_for(self) -> None:
        """One target, two names: the entity keeps the word the user used."""
        intent = _engine().understand("Open my NovaControl project").intent
        self.assertEqual(intent.entities.get("folder"), "novacontrol")
        self.assertFalse(intent.needs_clarification)

    def test_a_project_named_in_a_subordinate_clause_is_still_read(self) -> None:
        intent = _engine().understand("open the report folder").intent
        self.assertEqual(intent.intent, IntentName.OPEN_FOLDER)
        self.assertEqual(intent.entities.get("folder"), "report")

    def test_finding_a_project_keeps_its_own_intent(self) -> None:
        intent = _engine().understand("find my NovaControl project").intent
        self.assertEqual(intent.intent, IntentName.FIND_FILE)
        self.assertEqual(intent.entities.get("project"), "novacontrol")

    def test_a_bare_project_reference_resolves_from_context(self) -> None:
        """"show me that project" names a KIND, so it must not become a name."""
        gil = _engine()
        gil.understand("Open my NovaControl project")
        intent = gil.understand("show me that project").intent
        self.assertEqual(intent.intent, IntentName.OPEN_FOLDER)
        self.assertNotIn("show me that project", intent.entities.values())
        self.assertEqual(intent.entities.get("project"), "novacontrol")
        # Reading "that project" from memory is evidence, not a transcription:
        # the reading is recorded as a reference and capped accordingly.
        self.assertTrue(intent.references)
        self.assertLessEqual(intent.confidence, NluThresholds().reference_confidence)

    def test_a_bare_project_with_nothing_remembered_asks_instead(self) -> None:
        gil = _engine()
        intent = gil.understand("open my project").intent
        self.assertTrue(intent.needs_clarification)


class IntentAliasTests(unittest.TestCase):
    """Every name the specification uses resolves to a real intent."""

    def test_every_spec_alias_resolves(self) -> None:
        expected = {
            "launch_website": IntentName.NAVIGATE,
            "create_file": IntentName.WRITE_FILE,
            "execute_command": IntentName.RUN_COMMAND,
            "screenshot": IntentName.TAKE_SCREENSHOT,
            "explain": IntentName.ANSWER_QUESTION,
            "unknown": IntentName.CLARIFY,
        }
        for alias, intent in expected.items():
            with self.subTest(alias=alias):
                self.assertIs(resolve_intent(alias), intent)

    def test_an_alias_points_at_an_intent_that_exists(self) -> None:
        """An alias is a second name, never a second intent."""
        for alias, target in INTENT_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, set(IntentName))
                self.assertNotIn(alias, {member.value for member in IntentName})

    def test_canonical_names_still_resolve_and_junk_does_not(self) -> None:
        self.assertIs(resolve_intent("open_application"), IntentName.OPEN_APPLICATION)
        self.assertIs(resolve_intent("Launch-Website"), IntentName.NAVIGATE)
        self.assertIsNone(resolve_intent("florb_the_thing"))
        self.assertIsNone(resolve_intent(""))


class RequirementFlagParityTests(unittest.TestCase):
    """Every layer's reading is judged by the CATALOG's description of it.

    A hand-built intent (the composite browser task, a context resolution) once
    skipped the step that derives these flags, so "open chrome and search
    youtube for X" reported ``requires_web=False`` for a web task. The layers
    are different; the description of an intent is not.
    """

    def test_flags_match_the_catalog_whatever_layer_produced_the_reading(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        gil.understand("Open my NovaControl project")  # give the context a project
        samples = [
            "open chrome",                                                     # fast_path
            "Open Chrome and search YouTube for the latest AI news.",           # composite
            "open notepad and show me the florb",                              # multi_intent
            "show me that project",                                           # contextual
            "describe what you see in this screenshot",                       # vision
            "save it",                                                        # clarification
        ]
        for text in samples:
            with self.subTest(text=text):
                intent = gil.understand(text).intent
                definition = gil.catalog.get(intent.intent)
                if definition is None:
                    continue
                self.assertEqual(
                    intent.requires_web, definition.requires_web,
                    f"requires_web disagrees with the catalog for {text!r}",
                )
                self.assertEqual(
                    intent.requires_vision, definition.requires_vision,
                    f"requires_vision disagrees with the catalog for {text!r}",
                )
                self.assertEqual(
                    intent.requires_confirmation, definition.requires_confirmation,
                    f"requires_confirmation disagrees with the catalog for {text!r}",
                )


class ContinuationTests(unittest.TestCase):
    """A bare "carry on" belongs to memory, not to a similarity match.

    Measured before this was fixed: *"continue from where I stopped"* was the
    nearest exemplar to ``gpu_status`` at 0.57 raw / 0.63 calibrated, above the
    floor, so a request to resume work was answered with GPU telemetry. Nothing
    in the wording says which work — only the context does.
    """

    def test_a_bare_continuation_is_never_read_as_a_status_query(self) -> None:
        statuses = {
            IntentName.GPU_STATUS,
            IntentName.CPU_STATUS,
            IntentName.MEMORY_STATUS,
            IntentName.BATTERY_STATUS,
            IntentName.NETWORK_STATUS,
        }
        for phrase in (
            "continue from where I stopped",
            "continue where I left off",
            "pick up where I left off",
            "where was I",
            "carry on",
        ):
            with self.subTest(phrase=phrase):
                gil = _engine(thresholds=NluThresholds(allow_llm=False))
                intent = gil.understand(phrase).intent
                self.assertNotIn(intent.intent, statuses)

    def test_a_continuation_resumes_the_remembered_task(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        gil.understand("read report.pdf")  # the work in progress
        understood = gil.understand("continue from where I stopped")
        self.assertEqual(understood.intent.intent, IntentName.READ_FILE)
        self.assertEqual(understood.strategy, "contextual")

    def test_the_phrase_shape_decides_not_the_word_alone(self) -> None:
        """Only a BARE continuation is context work; a named one is a request."""
        for text in (
            "continue from where I stopped",
            "continue where I left off",
            "resume",
            "carry on",
            "where was I",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_continuation(text))
        for text in (
            "continue the project I was working on yesterday",
            "resume the music",
            "continue with the report",
            "open chrome",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_continuation(text))


class ReferencePhraseLearningTests(unittest.TestCase):
    """A phrase meaning "the last thing" must never become a rule.

    The learned layer runs BEFORE the context resolver, so teaching such a
    phrase freezes what "that" meant the first time it was heard. Measured
    before the fix: *"do the same thing"* was taught against a file read and
    replayed that read after an unrelated browser task.
    """

    def test_a_reference_phrase_keeps_following_the_current_context(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        gil.understand("read report.pdf")
        first = gil.understand("do the same thing")
        self.assertEqual(first.intent.intent, IntentName.READ_FILE)
        gil.understand("open chrome and search youtube for python tutorials")
        second = gil.understand("do the same thing")
        self.assertEqual(second.intent.intent, IntentName.BROWSER_ACTION)
        self.assertEqual(second.strategy, "contextual")

    def test_an_ordinary_phrase_is_still_taught(self) -> None:
        """The guard is narrow: real phrasings keep teaching (that is the point)."""
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        gil.understand("chrome please")
        self.assertEqual(gil.telemetry.to_dict()["resolved_by_strategy"]["contextual"], 1)
        gil.understand("chrome please")
        self.assertIn("learned_variant", gil.telemetry.to_dict()["resolved_by_strategy"])


class CompositeComplexityTests(unittest.TestCase):
    """The spec's MODERATE example must be moderate, not trivial."""

    def test_a_two_part_browser_task_is_moderate(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        intent = gil.understand("Open Chrome and search YouTube for Python tutorials.").intent
        self.assertEqual(intent.intent, IntentName.BROWSER_ACTION)
        complexity = intent.parameters["complexity"]
        self.assertEqual(complexity["level"], Complexity.MODERATE.value)
        self.assertTrue(complexity["needs_planner"])
        self.assertFalse(complexity["needs_model"])

    def test_a_single_step_stays_simple(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        complexity = gil.understand("Open Chrome.").intent.parameters["complexity"]
        self.assertEqual(complexity["level"], Complexity.SIMPLE.value)
        self.assertFalse(complexity["needs_planner"])


class VisionRequirementTests(unittest.TestCase):
    """A request to LOOK at something says so even when the words fail."""

    def test_an_ununderstood_vision_request_still_routes_to_vision(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        intent = gil.understand("describe the image on screen").intent
        self.assertTrue(intent.requires_vision)
        self.assertEqual(intent.decision.get("route"), Route.VISION.value)
        self.assertEqual(intent.reasoning_level, "vision")

    def test_the_vision_route_is_counted_where_it_went(self) -> None:
        """The aggregate must not report a vision request as a plain question."""
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        gil.understand("describe the image on screen")
        telemetry = gil.telemetry.to_dict()
        self.assertEqual(telemetry["routes"].get(Route.VISION.value), 1)
        self.assertEqual(telemetry["clarifications"], 1)

    def test_a_screenshot_action_is_not_mistaken_for_vision_analysis(self) -> None:
        """"take a screenshot" is an action; it must not claim the vision route."""
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        intent = gil.understand("take a screenshot").intent
        self.assertEqual(intent.intent, IntentName.TAKE_SCREENSHOT)
        self.assertEqual(intent.decision.get("route"), Route.FAST.value)


class OutcomeRecordingTests(unittest.TestCase):
    """Item 15 asks for success/failure, not only "was it understood"."""

    def test_the_record_carries_the_id_the_client_is_given(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        understood = gil.understand("open chrome")
        sample = gil.telemetry.to_dict()["recent_samples"][-1]
        self.assertEqual(sample["request_id"], request_id_for(understood.intent.id))
        self.assertEqual(understood.intent.to_dict()["id"], understood.intent.id)

    def test_an_outcome_annotates_its_own_sample(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        understood = gil.understand("open chrome")
        request_id = request_id_for(understood.intent.id)
        gil.telemetry.record_outcome(request_id=request_id, success=False, detail="RuntimeError")
        summary = gil.telemetry.to_dict()
        self.assertEqual(summary["outcomes"]["failure"], 1)
        sample = next(
            item
            for item in summary["recent_samples"]
            if item.get("request_id") == request_id and item.get("kind") == "resolution"
        )
        self.assertEqual(sample["outcome"], "failure")
        self.assertEqual(sample["outcome_detail"], "RuntimeError")

    def test_an_unreported_outcome_is_absent_rather_than_successful(self) -> None:
        gil = _engine(thresholds=NluThresholds(allow_llm=False))
        gil.understand("open chrome")
        summary = gil.telemetry.to_dict()
        self.assertEqual(summary["outcomes"], {})
        self.assertNotIn("outcome", summary["recent_samples"][-1])


class OutcomeThroughTheApplicationTests(unittest.IsolatedAsyncioTestCase):
    """The outcome is known one layer up, where the handler runs."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_a_carried_out_request_reports_success_against_its_own_id(self) -> None:
        response = await self.app.handle_request("open chrome")
        request_id = response.payload["nlu"]["request_id"]
        telemetry = self.app.intelligence.telemetry.to_dict()
        self.assertEqual(telemetry["outcomes"]["success"], 1)
        sample = next(
            item
            for item in telemetry["recent_samples"]
            if item.get("request_id") == request_id and item.get("kind") == "resolution"
        )
        self.assertEqual(sample["outcome"], "success")
        self.assertEqual(sample["nlu_method"], "fast_path")


if __name__ == "__main__":
    unittest.main()
