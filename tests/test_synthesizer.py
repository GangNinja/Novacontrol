"""Synthesizer regression tests.

Pins the fact-density guarantee: whatever shape the incoming snippets take
(definition-heavy, cause-heavy, a lone single fact) and whatever query kind
the user asked for, the template synthesizer must emit a non-empty answer
whose body carries the extracted facts — never a bare intro shell and never
an empty string. This is the regression guard for the bucket fallback
machinery in synthesizer.py (each builder falls back to _any_facts when its
preferred bucket is empty).
"""

from __future__ import annotations

import re
import unittest

from novacontrol.explore.models import ResearchSource
from novacontrol.explore.query import parse_query
from novacontrol.explore.synthesizer import synthesize_answer


# Every query kind parse_query can produce, with a phrasing that reaches it.
_QUERY_KINDS: tuple[tuple[str, str], ...] = (
    ("how_to", "how to compost at home"),
    ("explanation", "what is photosynthesis"),
    ("cause", "why do leaves change color in autumn"),
    ("recommendation", "recommend a laptop for video editing under 1000"),
    ("comparison", "compare python and javascript for web development"),
    ("ideas", "give me ideas for a birthday scavenger hunt"),
    ("mechanism", "explain how a refrigerator keeps food cold"),
)

_DEFINITION_SNIPPETS: tuple[str, ...] = (
    "Photosynthesis is the process that plants use to convert sunlight, water, "
    "and carbon dioxide into glucose and oxygen. It is defined as the way plants "
    "make their own food from light energy.",
    "A black hole is a region of spacetime where gravity is so strong that nothing, "
    "not even light, can escape it. It is known as the densest object in the universe.",
)

_CAUSE_SNIPPETS: tuple[str, ...] = (
    "Leaves change color in autumn because shorter days reduce chlorophyll "
    "production, which lets carotenoid pigments show through.",
    "Sea levels rise due to thermal expansion caused by warmer ocean temperatures "
    "and because glaciers and ice sheets melt into the ocean.",
)

_SINGLE_FACT = (
    "The Great Barrier Reef is the largest coral reef system in the world, "
    "stretching over 2300 kilometers."
)

_FACT_LINE = re.compile(r"^\s*(?:\u2022|\d+\.)\s")


def _answer(query: str, snippets: tuple[str, ...]) -> str:
    """Synthesize a template answer for a query against raw snippets (no LLM)."""
    frame = parse_query(query)
    sources = tuple(
        ResearchSource(title=f"Source {i}", url=f"https://example.com/{i}", snippet=s)
        for i, s in enumerate(snippets)
    )
    return synthesize_answer(frame.subject, frame, sources)


class FactDensityGuaranteeTests(unittest.TestCase):
    """Definition-heavy, cause-heavy, and single-fact inputs stay fact-dense
    for every query kind."""

    def test_query_phrases_reach_their_intended_kind(self) -> None:
        """Guard the table: each phrase must actually exercise its named kind,
        not silently fall back to a generic explanation build."""
        for kind, query in _QUERY_KINDS:
            with self.subTest(kind=kind, query=query):
                self.assertEqual(parse_query(query).kind, kind)

    def test_definition_heavy_snippets(self) -> None:
        for kind, query in _QUERY_KINDS:
            with self.subTest(kind=kind):
                answer = _answer(query, _DEFINITION_SNIPPETS)
                self.assertTrue(answer.strip(), f"{kind}: answer must not be empty")
                self.assertIn("glucose", answer, f"{kind}: definition fact missing")
                self.assertIn("black hole", answer, f"{kind}: second definition fact missing")
                self.assertTrue(
                    any(_FACT_LINE.match(line) for line in answer.splitlines()),
                    f"{kind}: answer body carries no fact lines",
                )

    def test_cause_heavy_snippets(self) -> None:
        for kind, query in _QUERY_KINDS:
            with self.subTest(kind=kind):
                answer = _answer(query, _CAUSE_SNIPPETS)
                self.assertTrue(answer.strip(), f"{kind}: answer must not be empty")
                self.assertIn("chlorophyll", answer, f"{kind}: causal fact missing")
                self.assertIn("thermal", answer, f"{kind}: second causal fact missing")
                self.assertTrue(
                    any(_FACT_LINE.match(line) for line in answer.splitlines()),
                    f"{kind}: answer body carries no fact lines",
                )

    def test_single_fact_input(self) -> None:
        for kind, query in _QUERY_KINDS:
            with self.subTest(kind=kind):
                answer = _answer(query, (_SINGLE_FACT,))
                self.assertTrue(answer.strip(), f"{kind}: answer must not be empty")
                self.assertIn("coral reef", answer, f"{kind}: the lone fact is missing")
                self.assertTrue(
                    any(_FACT_LINE.match(line) for line in answer.splitlines()),
                    f"{kind}: the lone fact never reaches the answer body",
                )


class SynthesizeEdgeCaseTests(unittest.TestCase):
    """Near-boundary inputs to the synthesizer."""

    def test_empty_snippets_return_empty(self) -> None:
        """No usable facts at all means no fabricated content."""
        frame = parse_query("what is photosynthesis")
        self.assertEqual(synthesize_answer(frame.subject, frame, ()), "")

    def test_title_only_fallback_is_nonempty(self) -> None:
        """Snippets too short to be facts still produce a source-title answer."""
        source = ResearchSource(
            title="Photosynthesis Guide",
            url="https://example.com/photosynthesis",
            snippet="Tiny.",
        )
        frame = parse_query("what is photosynthesis")
        answer = synthesize_answer(frame.subject, frame, (source,))
        self.assertTrue(answer.strip())
        self.assertIn("Photosynthesis Guide", answer)


if __name__ == "__main__":
    unittest.main()
