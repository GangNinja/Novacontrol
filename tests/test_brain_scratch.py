"""Tests for the scratch brain's local answer capabilities."""

from __future__ import annotations

import unittest
from datetime import datetime

import novacontrol.brain.brain as brain_module
from novacontrol.brain.brain import BrainIntent, BrainRequest, NovaBrain
from novacontrol.brain.scratch import (
    ScratchReasoningEngine,
    _classify,
    _convert_temperature,
    _math_answer,
    _safe_eval_math,
    _time_answer,
    _try_convert,
    scratchable_intent,
)


# ---------------------------------------------------------------------------
# Safe math evaluator
# ---------------------------------------------------------------------------

class MathEvaluatorTests(unittest.TestCase):

    CALCULATIONS: list[tuple[str, float]] = [
        ("2+3", 5),
        ("10-4", 6),
        ("6*7", 42),
        ("100/4", 25),
        ("2**10", 1024),
        ("10%3", 1),
        ("10//3", 3),
        ("(2+3)*4", 20),
        ("sqrt(144)", 12),
        ("abs(-42)", 42),
        ("round(3.7)", 4),
        ("floor(3.9)", 3),
        ("ceil(3.1)", 4),
        ("pi", 3.14159265),
        ("e", 2.71828182),
    ]

    def test_calculations(self) -> None:
        for expr, expected in self.CALCULATIONS:
            with self.subTest(expr=expr):
                result = _safe_eval_math(expr)
                self.assertIsNotNone(result, f"_safe_eval_math({expr!r}) returned None")
                self.assertAlmostEqual(result, expected, places=4)

    def test_invalid_expressions_return_none(self) -> None:
        bad = ["hello", "import os", "__import__('os')", "open('file')", ""]
        for expr in bad:
            with self.subTest(expr=expr):
                self.assertIsNone(_safe_eval_math(expr))

    def test_math_answer_returns_message(self) -> None:
        result = _math_answer("calculate 2+3")
        self.assertIn("5", result["message"])
        self.assertTrue(result["sections"])

    def test_math_answer_bad_expression(self) -> None:
        result = _math_answer("calculate hello")
        self.assertIn("couldn't", result["message"].lower())


# ---------------------------------------------------------------------------
# Temperature conversion
# ---------------------------------------------------------------------------

class TemperatureTests(unittest.TestCase):

    def test_celsius_to_fahrenheit(self) -> None:
        self.assertAlmostEqual(_convert_temperature(100, "c", "f"), 212.0, places=1)

    def test_fahrenheit_to_celsius(self) -> None:
        self.assertAlmostEqual(_convert_temperature(32, "f", "c"), 0.0, places=1)

    def test_celsius_to_kelvin(self) -> None:
        self.assertAlmostEqual(_convert_temperature(0, "c", "k"), 273.15, places=1)

    def test_same_unit(self) -> None:
        self.assertEqual(_convert_temperature(100, "c", "c"), 100)


# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------

class UnitConversionTests(unittest.TestCase):

    def test_km_to_miles(self) -> None:
        result = _try_convert(100, "km", "mi")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 62.1371, places=2)

    def test_pounds_to_kg(self) -> None:
        result = _try_convert(1, "lb", "kg")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.4536, places=3)

    def test_liters_to_gallons(self) -> None:
        result = _try_convert(1, "l", "gal")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.2642, places=3)

    def test_unknown_units_return_none(self) -> None:
        self.assertIsNone(_try_convert(100, "foo", "bar"))


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class ScratchIntentClassificationTests(unittest.TestCase):

    INTENTS: list[tuple[str, str]] = [
        ("hi", "greeting"),
        ("hello", "greeting"),
        ("what time is it", "time"),
        ("current date", "time"),
        ("what day is today", "time"),
        ("2+3", "math"),
        ("calculate 100*37", "math"),
        ("sqrt(144)", "math"),
        ("what is sqrt(144)", "math"),
        ("what is the square root of 256", "math"),
        ("5 squared", "math"),
        ("8 cubed", "math"),
        ("100 km in miles", "conversion"),
        ("72 F to C", "conversion"),
        ("5 kg to lbs", "conversion"),
        ("what is the capital of france", "knowledge"),
        ("who invented the telephone", "knowledge"),
        ("how many planets are there", "knowledge"),
        ("recommend a movie", "recommendation"),
        ("suggest a book", "recommendation"),
        ("what should i eat", "recommendation"),
        ("open notepad", "desktop_control"),
        ("what can you do", "capabilities"),
    ]

    def test_intents(self) -> None:
        for text, expected in self.INTENTS:
            with self.subTest(text=text):
                self.assertEqual(_classify(text.lower()), expected)


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

class KnowledgeTests(unittest.TestCase):

    def test_known_facts(self) -> None:
        engine = ScratchReasoningEngine()
        queries = [
            ("what is the speed of light", "299,792,458"),
            ("how many planets", "8 planets"),
            ("capital of france", "Paris"),
            ("who invented the telephone", "Alexander Graham Bell"),
        ]
        for query, expected_fragment in queries:
            with self.subTest(query=query):
                result = engine.answer(query, {})
                self.assertIn(expected_fragment.lower(), result["message"].lower())

    def test_unknown_facts_suggest_explore(self) -> None:
        engine = ScratchReasoningEngine()
        # Topic not in knowledge base → should suggest Explore
        result = engine.answer("zyxqbot9 flux capacitor redesign", {"modules": ["explore"]})
        # Unknown topics suggest using Explore when the module is available
        self.assertIn("explore", result["message"].lower())
        self.assertEqual(result["intent"], "unknown")


# ---------------------------------------------------------------------------
# Brain routing — scratch answers bypass EXPLORE
# ---------------------------------------------------------------------------

class BrainScratchRoutingTests(unittest.TestCase):

    SCRATCHABLE: list[tuple[str, BrainIntent]] = [
        ("what is 2+2", BrainIntent.CHAT),
        ("what is 2+2?", BrainIntent.CHAT),
        ("calculate sqrt(144)", BrainIntent.CHAT),
        ("what is 15 times 3", BrainIntent.CHAT),
        ("what is 10 divided by 4", BrainIntent.CHAT),
        ("add 5 and 7", BrainIntent.CHAT),
        ("add 5 and 7 and show steps", BrainIntent.CHAT),
        ("what is sqrt(144)", BrainIntent.CHAT),
        ("what is the square root of 256", BrainIntent.CHAT),
        ("what is 5 squared", BrainIntent.CHAT),
        ("3 plus 4 minus 2", BrainIntent.CHAT),
        ("what time is it", BrainIntent.CHAT),
        ("current date", BrainIntent.CHAT),
        ("100 km in miles", BrainIntent.CHAT),
        ("what is the capital of france", BrainIntent.CHAT),
        ("how many planets", BrainIntent.CHAT),
        ("recommend a movie", BrainIntent.CHAT),
    ]

    def test_scratchable_queries_route_to_chat(self) -> None:
        brain = NovaBrain()
        for text, expected in self.SCRATCHABLE:
            with self.subTest(text=text):
                decision = brain.decide(BrainRequest(text))
                self.assertEqual(decision.intent, expected)

    def test_research_still_routes_to_explore(self) -> None:
        brain = NovaBrain()
        for text in ("explain transformers in AI", "why do birds migrate",
                      "compare React and Vue", "what is deep learning",
                      # Cross-intent boundary: a research verb wins over the
                      # mere presence of a plan noun (EXPLORE gate precedes PLAN).
                      "compare the plan", "what is the plan", "explain the plan",
                      "what is the roadmap"):
            with self.subTest(text=text):
                decision = brain.decide(BrainRequest(text))
                self.assertEqual(decision.intent, BrainIntent.EXPLORE)


# ---------------------------------------------------------------------------
# Scratch engine — full answer output
# ---------------------------------------------------------------------------

class ScratchableIntentSingleOwnerTests(unittest.TestCase):
    """decide() must route through ONE imported narrow classifier."""

    def test_brain_imports_only_scratchable_intent(self) -> None:
        import re
        with open(brain_module.__file__, encoding="utf-8") as fh:
            brain_src = fh.read()
        # The single import line must not re-import the replaced predicates.
        self.assertIn("scratchable_intent", brain_src)
        for banned in ("can_answer_directly", "is_arithmetic_query", "def _is_greeting"):
            self.assertNotIn(banned, brain_src, f"brain.py must not contain {banned}")
        # Greetings come from the shared scratch set, not a brain-local list.
        self.assertEqual(len(re.findall(r"good morning", brain_src)), 0)

    def test_scratchable_intent_is_narrow(self) -> None:
        # Canned local answers classify; unknown research topics stay None so
        # decide() sends them to Explore.
        for text, expected in [
            ("hi", "greeting"),
            ("what is 2+2", "math"),
            ("what time is it", "time"),
            ("100 km in miles", "conversion"),
            ("what is the capital of france", "knowledge"),
            ("recommend a movie", "recommendation"),
            ("what is dark matter", None),
            ("open notepad", None),  # engine intent, not a canned answer
            ("what can you do", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(scratchable_intent(text), expected)


class ScratchAnswerTests(unittest.TestCase):

    def test_math_answer(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("what is 2+2", {})
        self.assertEqual(result["intent"], "math")
        self.assertIn("4", result["message"])
        self.assertEqual(result["confidence"], "high")

    def test_worded_math_answer(self) -> None:
        engine = ScratchReasoningEngine()
        cases = [
            ("what is 15 times 3", "45"),
            ("what is 15 times 3?", "45"),
            ("what is 10 divided by 4", "2.5"),
            ("add 5 and 7", "12"),
            ("add 5 and 7 and show steps", "12"),
            ("what is sqrt(144)", "12"),
            ("what is the square root of 256", "16"),
            ("what is 5 squared", "25"),
            ("what is 8 cubed", "512"),
            ("3 plus 4 minus 2", "5"),
            ("6 multiplied by 7", "42"),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                result = engine.answer(query, {})
                self.assertEqual(result["intent"], "math", query)
                self.assertIn(expected, result["message"], query)

    def test_time_answer(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("what time is it", {})
        self.assertEqual(result["intent"], "time")
        now = datetime.now()
        # Message uses 12-hour format (e.g. "10:26 PM"), not 24-hour
        hour_12 = now.hour % 12 or 12
        self.assertIn(str(hour_12), result["message"])
        self.assertEqual(result["confidence"], "high")

    def test_conversion_answer(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("100 km in miles", {})
        self.assertEqual(result["intent"], "conversion")
        self.assertIn("62", result["message"])

    def test_knowledge_answer(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("what is the speed of light", {})
        self.assertEqual(result["intent"], "knowledge")
        self.assertIn("299", result["message"])

    def test_recommendation_answer(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("recommend a movie", {})
        self.assertEqual(result["intent"], "recommendation")
        self.assertIn("movie", result["message"].lower())
        self.assertTrue(result["sections"])

    def test_greeting_answer(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("hello", {})
        self.assertEqual(result["intent"], "greeting")
        self.assertIn("NovaControl", result["message"])

    def test_unknown_defers_to_explore(self) -> None:
        engine = ScratchReasoningEngine()
        result = engine.answer("explain quantum entanglement", {"modules": ["explore"]})
        self.assertEqual(result["intent"], "unknown")
        self.assertIn("explore", result["message"].lower())

    def test_all_answers_have_required_keys(self) -> None:
        engine = ScratchReasoningEngine()
        queries = ["hello", "2+3", "what time is it", "100 km in miles",
                    "what is pi", "recommend a movie", "open notepad", "random gibberish"]
        for q in queries:
            with self.subTest(query=q):
                result = engine.answer(q, {})
                self.assertIn("message", result)
                self.assertIn("intent", result)
                self.assertIn("brain_mode", result)
                self.assertEqual(result["brain_mode"], "scratch")
                self.assertIn("sections", result)
                self.assertIsInstance(result["sections"], list)


if __name__ == "__main__":
    unittest.main()
