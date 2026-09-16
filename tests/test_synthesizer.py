"""Synthesizer regression tests.

Pins the fact-density guarantee: whatever shape the incoming snippets take
(definition-heavy, cause-heavy, a lone single fact) and whatever query kind
the user asked for, the template synthesizer must emit a non-empty answer
whose body carries the extracted facts — never a bare intro shell and never
an empty string. This is the regression guard for the bucket fallback
machinery in synthesizer.py (each builder falls back to _any_facts when its
preferred bucket is empty).

Also pins the site-chrome guard: search snippets glue consent banners,
footers, and trademark lines onto real content, and none of that may reach an
answer as a "fact" (the real-world failure was Chat answering "explain
quantum computing" with a Terms-of-Use sentence).
"""

from __future__ import annotations

import re
import unittest

from novacontrol.explore.models import ResearchSource
from novacontrol.explore.query import parse_query
from novacontrol.explore.sections import (
    answer_highlights,
    build_sections,
    detailed_explanation,
    key_points,
)
from novacontrol.explore.synthesizer import (
    clean_snippet,
    extract_facts,
    is_boilerplate,
    synthesize_answer,
)


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


# ────────────────────────────────────────────────────────────
# Site chrome
# ────────────────────────────────────────────────────────────

# Verbatim text from a real Chat research answer ("explain quantum computing
# in simple terms"): a Wikipedia footer and a trademark line were served as
# the answer's "What it is" facts.
_SCREENSHOT_SNIPPETS: tuple[str, ...] = (
    "By using this site, you agree to the Terms of Use and Privacy Policy. "
    "Wikipedia\u00ae is a registered trademark of the Wikimedia Foundation, Inc., a non-profit organization.",
    "Quantum computing terms explained simply \u2013 from qubits to error correction \u2013 "
    "in a plain language guide for anyone curious about what quantum really means.",
    "This page was last edited on 5 September 2026, at 10:49 (UTC).",
    "Quantum computing is a rapidly-emerging technology that harnesses the laws of "
    "quantum mechanics to solve problems too complex for classical computers.",
)

_CHROME_MARKERS: tuple[str, ...] = (
    "terms of use",
    "privacy policy",
    "registered trademark",
    "last edited",
    "all rights reserved",
    "enable javascript",
    "accept all cookies",
)

_CHROME_ONLY: tuple[str, ...] = (
    "By using this site, you agree to the Terms of Use and Privacy Policy.",
    "Wikipedia\u00ae is a registered trademark of the Wikimedia Foundation, Inc.",
    "This page was last edited on 5 September 2026, at 10:49 (UTC).",
    "We use cookies to improve your experience.",
    "Accept all cookies to continue.",
    "\u00a9 2026 Example Corporation. All rights reserved.",
    "Please enable JavaScript to view this page.",
)

_REAL_CONTENT: tuple[str, ...] = (
    "Photosynthesis converts light energy into chemical energy stored as glucose.",
    "A privacy policy is a statement that explains how an organization gathers, uses, "
    "and shares user data.",
    "Quantum computing harnesses quantum mechanics to solve problems classical "
    "computers cannot.",
)


def _chrome_sources() -> tuple[ResearchSource, ...]:
    return tuple(
        ResearchSource(title=f"Source {i}", url=f"https://example.com/{i}", snippet=s)
        for i, s in enumerate(_SCREENSHOT_SNIPPETS)
    )


class SiteChromeFilterTests(unittest.TestCase):
    """Consent banners and footers are chrome, never research facts."""

    def test_is_boilerplate_classifies_chrome_and_real_content(self) -> None:
        for text in _CHROME_ONLY:
            with self.subTest(chrome=text[:40]):
                self.assertTrue(is_boilerplate(text))
        for text in _REAL_CONTENT:
            with self.subTest(real=text[:40]):
                self.assertFalse(is_boilerplate(text))

    def test_clean_snippet_returns_empty_for_pure_chrome(self) -> None:
        """Callers treat "" as "skip me", so pure chrome must yield ""."""
        for text in _CHROME_ONLY:
            with self.subTest(chrome=text[:40]):
                self.assertEqual(clean_snippet(text), "")

    def test_mixed_snippet_keeps_content_and_drops_chrome(self) -> None:
        """Chrome glued onto a real sentence must not cost us the sentence."""
        mixed = (
            "By using this site, you agree to the Terms of Use and Privacy Policy. "
            "Quantum computing harnesses quantum mechanics to solve problems classical "
            "computers cannot."
        )
        cleaned = clean_snippet(mixed)
        self.assertIn("harnesses quantum mechanics", cleaned)
        self.assertNotIn("Terms of Use", cleaned)

    def test_screenshot_answer_contains_no_chrome(self) -> None:
        """The exact snippets behind the reported Chat failure."""
        sources = _chrome_sources()
        frame = parse_query("explain quantum computing in simple terms")
        answer = synthesize_answer(frame.subject, frame, sources).lower()
        for marker in _CHROME_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, answer)
        self.assertIn("harnesses the laws of quantum mechanics", answer)

    def test_extract_facts_skips_chrome_sources(self) -> None:
        facts = extract_facts(_chrome_sources())
        self.assertTrue(facts, "real snippets must still produce facts")
        for fact in facts:
            with self.subTest(fact=fact[:40]):
                self.assertFalse(is_boilerplate(fact))

    def test_rendered_sections_and_highlights_carry_no_chrome(self) -> None:
        """Every surface a report renders (Chat and Explore share these)."""
        sources = _chrome_sources()
        frame = parse_query("explain quantum computing in simple terms")
        rendered: list[str] = [
            *key_points(frame, sources),
            *answer_highlights(frame, sources),
            detailed_explanation(frame, sources, "standard"),
        ]
        for section in build_sections(frame, sources):
            rendered.append(str(section.get("title", "")))
            rendered.extend(str(item.get("text", "")) for item in section.get("items", ()))
        blob = "\n".join(rendered).lower()
        for marker in _CHROME_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, blob)


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
