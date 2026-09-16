"""Drift guard for the worded-arithmetic rule registry (_MATH_WORD_RULES).

The registry is ONE table consumed by two surfaces: routing (is_arithmetic_query
/ scratchable_intent -> "math") and the answer builder (_math_answer). A rule
row that routes but cannot evaluate (or vice versa) means a user's phrase is
recognized as math yet produces no answer — the exact drift this module's
comment block warns about. These tests walk every row with real phrase samples
and assert both surfaces agree, plus that the sample table itself covers every
registered row so a future rule without samples fails loudly.
"""

from __future__ import annotations

import re
import unittest

from novacontrol.brain.scratch import (
    ScratchReasoningEngine,
    _MATH_WORD_RULES,
    is_arithmetic_query,
    rewrite_worded_math,
    scratchable_intent,
)

# One or more phrase samples per registry row, keyed by a human-readable row
# name. Every row in _MATH_WORD_RULES must have at least one entry here.
_ROW_SAMPLES: list[tuple[str, list[str]]] = [
    ("to the power of", ["2 to the power of 8", "three to the power of four"]),
    ("square root of", ["square root of 256", "the square root of 144"]),
    ("sqrt of", ["sqrt of 81"]),
    ("squared", ["5 squared", "twelve squared"]),
    ("cubed", ["3 cubed", "ten cubed"]),
    # Simple binary rows: spelled-out operators -> symbols ("15 times 3").
    ("plus", ["5 plus 3", "five plus three", "what is 12 plus 30"]),
    ("minus", ["9 minus 4", "twenty minus fifteen"]),
    ("times", ["15 times 3", "fifteen times three", "6 times 7"]),
    ("over", ["10 over 2", "nine over three"]),
    ("multiplied by", ["8 multiplied by 4", "three multiplied by six"]),
    ("divided by", ["20 divided by 5", "eight divided by two"]),
    # Compound rows: binary operators whose neighbors may already be symbolic
    # ("2 cubed plus the square root of 9" -> "2**3 plus sqrt(9)"). Every sample
    # exercises the compound path — a purely worded phrase ("9 minus 4") is
    # consumed by the simple row earlier in the walk, so it would never reach
    # the compound row and proves nothing about it.
    ("compound plus", ["2 cubed plus the square root of 9", "what is 3 cubed plus 4 squared"]),
    ("compound minus", ["the square root of 81 minus 5 squared", "5 squared minus the square root of 4"]),
    ("compound times", ["5 squared times 2", "2 cubed times 3"]),
    ("compound multiplied by", ["2 cubed multiplied by 3", "three squared multiplied by two"]),
    ("compound divided by", ["the square root of 81 divided by 3", "5 squared divided by 4"]),
    ("compound over", ["2 to the power of 4 over 2", "the square root of 16 over 2"]),
    ("percent of", ["15 percent of 200", "what is 15 percent of 200", "ten percent of 300", "12.5 percent of 80"]),
    # Natural doubling/fraction phrasings. The fraction row covers the whole
    # family — bare, article ('a third of 90'), spelled numerator ('two thirds
    # of 300'), and scale-word operands ('half of three million').
    ("double", ["double 7", "double 15", "double nine", "double two million"]),
    ("fraction of", ["half of 10", "what is half of 50", "half of twenty",
                     "one quarter of 8", "a third of 90", "three quarters of 200",
                     "two thirds of 300", "a fifth of 100", "an eighth of 64",
                     "half of three million"]),
    # ── JEE / exam rows ─────────────────────────────────────────
    ("log base of", ["log base 2 of 8", "log base 10 of 1000"]),
    ("log of base", ["log of 100 base 10", "log 27 base 3"]),
    ("trig degrees", ["sin 30 degrees", "cos 60 degrees", "tan 45 degrees"]),
    ("trig radians", ["cos of 1", "sin 0.5"]),
    ("choose", ["8 choose 2", "ten choose three"]),
    ("p", ["5 p 3", "ten p two"]),
    ("glued nCr", ["10C3", "5C2"]),
    ("glued nPr", ["10P3", "4P2"]),
    ("factorial of", ["factorial of 5", "factorial of six"]),
    ("postfix factorial", ["5 factorial", "six factorial"]),
]

# Row identity: the operator/function text that built the row's pattern. Binary
# rules embed the literal operator phrase; unary/power rows embed the function
# word. Matching a sample against its row's pattern proves coverage per ROW,
# not merely per phrase.
def _row_fires(row_index: int, phrase_lower: str) -> bool:
    """Does registry row `row_index` fire for this phrase in the REAL walk?

    Faithful simulation of rewrite_worded_math's fixed-point loop: every pass
    applies rows in registry order, and the row under test is checked (and
    reported) at exactly the state the walk hands it — after all earlier rows
    of that pass and everything previous passes rewrote. A simple row that a
    later compound row subsumes still fires first (registry order), and a
    compound row only fires once its neighbors are symbolic — this function
    proves which, instead of guessing from raw text.
    """
    expr = phrase_lower
    pattern, _ = _MATH_WORD_RULES[row_index]
    for _ in range(8):
        changed = False
        for i, (row_pattern, replacement) in enumerate(_MATH_WORD_RULES):
            if i == row_index and row_pattern.search(expr):
                return True
            updated = row_pattern.sub(replacement, expr)
            if updated != expr:
                expr, changed = updated, True
        if not changed:
            break
    return False


class MathWordRulesDriftGuardTests(unittest.TestCase):
    """Every registry row routes to math AND evaluates to a non-empty answer."""

    def setUp(self) -> None:
        self.engine = ScratchReasoningEngine()

    def test_every_row_routes_and_evaluates(self) -> None:
        for row_name, phrases in _ROW_SAMPLES:
            for phrase in phrases:
                with self.subTest(row=row_name, phrase=phrase):
                    lower = phrase.lower()
                    # 1. The routing surface classifies it as scratch math.
                    self.assertEqual(
                        scratchable_intent(lower),
                        "math",
                        f"[{row_name}] routing drift: {phrase!r} no longer routes to math",
                    )
                    self.assertTrue(
                        is_arithmetic_query(lower),
                        f"[{row_name}] routing drift: is_arithmetic_query({phrase!r}) is False",
                    )
                    # 2. The answer builder produces a non-empty answer...
                    answer = self.engine.answer(phrase, {})
                    message = str(answer.get("message", "")).strip()
                    self.assertTrue(
                        message,
                        f"[{row_name}] evaluation drift: {phrase!r} routed to math "
                        "but produced an empty answer",
                    )
                    # ...and the answer actually contains the computed number,
                    # not a fallback line (guards against silent evaluator rot).
                    self.assertRegex(
                        message.lower(),
                        r"result|is \*?\*?-?\d",
                        f"[{row_name}] {phrase!r} answered without a computed result: {message!r}",
                    )

    def test_sample_table_covers_every_registry_row(self) -> None:
        """A future rule row added without samples here must fail this test."""
        self.assertEqual(
            len(_ROW_SAMPLES),
            len(_MATH_WORD_RULES),
            "_MATH_WORD_RULES grew/shrank without matching _ROW_SAMPLES coverage: "
            f"{len(_MATH_WORD_RULES)} rows vs {len(_ROW_SAMPLES)} sample groups",
        )
        for index, (row_name, phrases) in enumerate(_ROW_SAMPLES):
            with self.subTest(row=row_name):
                self.assertLess(
                    index, len(_MATH_WORD_RULES), f"sample row {row_name!r} has no registry row"
                )
                # At least one sample must actually fire its row in the walk.
                hits = sum(1 for phrase in phrases if _row_fires(index, phrase.lower()))
                self.assertGreater(
                    hits,
                    0,
                    f"sample row {row_name!r}: no sample phrase fires registry row {index} in the walk",
                )

    def test_registry_rows_are_distinct_and_ordered(self) -> None:
        """Duplicate patterns or a non-tuple row would silently break the walk."""
        patterns = [pattern.pattern for pattern, _ in _MATH_WORD_RULES]
        self.assertEqual(len(patterns), len(set(patterns)), "duplicate rule patterns in registry")

    def test_natural_phrasing_answers(self) -> None:
        """Natural phrasings beyond the operator rows: 'what does 3 times 4
        equal' (question verb), 'half of 10' / 'double 7' (fraction/doubling
        rows), and variable assignment ('if x is 5, what is x times 3'). Each
        pins BOTH surfaces — routing and the numeric answer."""
        cases = [
            ("what does 3 times 4 equal", "12"),
            ("what does fifteen times three equal", "45"),
            ("half of 10", "5"),
            ("what is half of 50", "25"),
            ("double 7", "14"),
            ("double fifteen", "30"),
            ("one quarter of 8", "2"),
            ("a third of 90", "30"),
            ("three quarters of 200", "150"),
            ("two thirds of 300", "200"),
            ("3 million times 2", "6000000"),
            ("2.5 billion divided by 4", "625000000"),
            ("three billion minus seven", "2999999993"),
            ("2.5 million plus 500 thousand", "3000000"),
            ("half of three million", "1500000"),
            ("if x is 5, what is x times 3", "15"),
            ("if x = 4 and y = 6, what is x plus y", "10"),
            ("let x be 9, what is x squared", "81"),
        ]
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                lower = phrase.lower()
                # Routing: the narrow classifier must claim it as math.
                self.assertEqual(scratchable_intent(lower), "math", f"{phrase!r} not routed to math")
                # Answer: the numeric result is in the message.
                answer = self.engine.answer(phrase, {})
                message = str(answer.get("message", ""))
                self.assertIn(expected, message, f"{phrase!r} answered {message!r}, expected {expected}")

    def test_bare_assignment_does_not_route_to_math(self) -> None:
        """An assignment with no question attached ('if x is 5') is not an
        arithmetic request — routing must not claim it."""
        from novacontrol.brain.scratch import is_arithmetic_query
        self.assertFalse(is_arithmetic_query("if x is 5"))
        self.assertIsNone(scratchable_intent("if x is 5"))



if __name__ == "__main__":
    unittest.main()
