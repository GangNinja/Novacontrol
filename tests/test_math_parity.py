"""Detection parity: scratch.py vs the routing explorer's JS mirror.

The routing explorer's offline fallback classifies utterances with a hand-kept
JS copy of the Python gate predicates (``mirrorScratchable``). Whenever the
Python side learns a new form — spelled-out add operands, ``half of``/``double``
unary rows, variable assignment, percent phrasing — the mirror can silently
drift, and the explorer would then mislabel the offline trace.

This corpus drives EVERY utterance through BOTH engines — Python's
:func:`novacontrol.brain.scratch.scratchable_intent` here, the REAL
routing-explorer.js in node — and fails on the first classification that
differs. The corpus must grow whenever a math form is added to either engine.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from novacontrol.brain.scratch import scratchable_intent

ROUTING_JS = Path("src/novacontrol/web/static/js/routing-explorer.js")

# Every math shape scratch knows, plus the classic forms of the other narrow
# kinds, plus the decoys that must NOT drift into math or greeting.
PARITY_CORPUS: tuple[str, ...] = (
    # greetings (narrow matches the WHOLE text only)
    "hello",
    "hi",
    "good morning",
    "hello there",
    # symbol math
    "5 plus 3",
    "2+2",
    "10 / 4",
    # worded operators and phrasings
    "what is 15 percent of 200",
    "what does 3 times 4 equal",
    "what is 2 cubed plus the square root of 9",
    # add form: digit twin and spelled-out twin
    "add 5 and 7",
    "add five and seven",
    # unary registry rows
    "half of 10",
    "double 7",
    "double nine",
    "double two million",
    "5 squared",
    "3 cubed",
    "square root of 256",
    "sqrt of 81",
    # fraction family (bare / article / spelled numerator / plural denominator)
    "one quarter of 8",
    "a third of 90",
    "an eighth of 64",
    "three quarters of 200",
    "two thirds of 300",
    "a fifth of 100",
    "quarter of 60",
    "half of three million",
    # digit+scale operands on binary rows
    "3 million times 2",
    "2.5 billion divided by 4",
    "2.5 million plus 500 thousand",
    # variable assignment
    "if x is 5, what is x times 3",
    "if x = 4 and y = 6, what is x plus y",
    # conversion
    "convert 10 km to miles",
    "35 celsius to fahrenheit",
    # knowledge
    "what is the capital of france",
    "who invented the telephone",
    "population of japan",
    # recommendation
    "recommend a good movie",
    "suggest a book to read",
    # decoys — none of these may route to a narrow scratch kind
    "open notepad and type hello",
    "remember this: buy milk",
    "tell me about black holes",
    "search the web for cats",
    "this sentence is totally random",
)

# Utterances that MUST classify as math on BOTH sides — the anchor set that
# keeps this parity test meaningful (it fails loudly if either engine stops
# detecting math at all, which an empty-corpus pass could hide).
MUST_BE_MATH: tuple[str, ...] = (
    "5 plus 3",
    "add five and seven",
    "add 5 and 7",
    "half of 10",
    "double 7",
    "one quarter of 8",
    "a third of 90",
    "three quarters of 200",
    "3 million times 2",
    "half of three million",
    "what does 3 times 4 equal",
    "what is 15 percent of 200",
    "what is 2 cubed plus the square root of 9",
    "if x is 5, what is x times 3",
)

# Decoys that must stay OUT of the narrow scratch kinds on BOTH sides.
MUST_BE_NONE: tuple[str, ...] = (
    "open notepad and type hello",
    "remember this: buy milk",
    "tell me about black holes",
    "search the web for cats",
    "this sentence is totally random",
    "hello there",
)


class MathDetectionParityTests(unittest.TestCase):
    """One corpus, two engines, zero tolerated drift."""

    NODE = shutil.which("node")

    def test_python_side_detects_math_on_the_anchor_set(self) -> None:
        for text in MUST_BE_MATH:
            with self.subTest(text=text):
                self.assertEqual(scratchable_intent(text.lower()), "math")

    def test_python_side_keeps_decoys_out_of_the_narrow_kinds(self) -> None:
        for text in MUST_BE_NONE:
            with self.subTest(text=text):
                self.assertIsNone(scratchable_intent(text.lower()))

    def test_corpus_agrees_with_the_real_js_mirror(self) -> None:
        """Every corpus utterance classifies identically in Python and the mirror.

        Runs the REAL static routing-explorer.js in node (same harness as the
        explorer's own tests) and compares ``mirrorTrace().classifiers.narrow``
        against ``scratchable_intent`` for the whole corpus.
        """
        if self.NODE is None:
            self.skipTest("node is not installed")
        python_results = {text: scratchable_intent(text.lower()) for text in PARITY_CORPUS}
        script = (
            "global.window = global;"
            "const fs = require('fs');"
            f"eval(fs.readFileSync({json.dumps(str(ROUTING_JS))},'utf8'));"
            f"const python = {json.dumps(python_results)};"
            "let failed = 0;"
            "for (const [text, pyNarrow] of Object.entries(python)) {"
            "  const r = window.routingExplorerMirror.mirrorTrace(text);"
            "  const jsNarrow = r.classifiers.narrow;"
            "  if (jsNarrow !== pyNarrow) {"
            "    console.error('DRIFT ' + JSON.stringify(text) + ': python=' + pyNarrow + ' mirror=' + jsNarrow);"
            "    failed++;"
            "  }"
            "}"
            "console.error(failed ? failed + ' drift case(s)' : 'all corpus cases agree');"
            "process.exit(failed ? 1 : 0);"
        )
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.NODE, "-e", script], capture_output=True, text=True, timeout=60  # type: ignore[list-item]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all corpus cases agree", proc.stderr)
    def test_mirror_knowledge_keys_match_the_python_dict(self) -> None:
        """The mirror's MIRROR_KNOWLEDGE_KEYS literal must equal _KNOWLEDGE's keys.

        The corpus only exercises a few knowledge keys; this pins ALL of them,
        so a fact added to the Python knowledge base without regenerating the
        mirror list fails here instead of silently mislabeling offline traces.
        """
        if self.NODE is None:
            self.skipTest("node is not installed")
        from novacontrol.brain import scratch as scratch_module

        py_keys = set(scratch_module._KNOWLEDGE)
        # The key list lives at module scope, not on the exported object —
        # extract it straight from the source with the same shape the runtime
        # sees (eval would also expose it, but a plain regex is deterministic).
        js = ROUTING_JS.read_text(encoding="utf-8")
        match = re.search(r"var MIRROR_KNOWLEDGE_KEYS = \[(.*?)\];", js, re.S)
        self.assertIsNotNone(match, "MIRROR_KNOWLEDGE_KEYS literal missing from routing-explorer.js")
        js_keys = set(json.loads("[" + match.group(1).rstrip().rstrip(",") + "]"))
        self.assertEqual(js_keys, py_keys, "knowledge keys drifted between scratch.py and routing-explorer.js")
        self.assertTrue(py_keys, "knowledge base must not be empty")


if __name__ == "__main__":
    unittest.main()
