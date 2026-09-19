"""Ranked evidence: the answer is chosen by usefulness, not by keyword lists.

The failure this guards: a question whose answer lives in one sentence of a long
page, while every page repeats the question's own words. Ranking must surface
the SPECIFIC sentence — and it must do so for two unrelated topics with no
per-topic rules anywhere, which is why every case here is paired with a second
topic that exercises the same code path.

Also pinned: the composer never echoes the question as a framing line, never
repeats a sentence between the answer and the sections below it, and never
invents prose (every sentence in the answer came from a source).
"""

from __future__ import annotations

from collections.abc import Sequence
import unittest

from novacontrol.explore import ExploreRequest, ExploreService
from novacontrol.explore.evidence import build_evidence, page_text_of
from novacontrol.explore.models import ResearchSource
from novacontrol.explore.query import parse_query
from novacontrol.explore.sections import answer_highlights, build_sections, key_points
from novacontrol.explore.synthesizer import (
    compose_answer,
    select_answer_sentences,
    synthesize_answer,
)

from conftest import FakeVideoProvider

# Two topics, same shape: a page full of the topic's generic words, with the
# actual answer in one specific sentence. Nothing in the ranker knows either
# topic — the same code has to find the answer in both.
_CATS = (
    ResearchSource(
        "Purring: an overview",
        "https://vet.example.com/purring",
        "Purring is one of the many sounds a cat makes. Purring is common in cats of every age. "
        "Purring is discussed in this overview of cat behaviour. "
        "A cat's purr comes from the rapid vibration of the vocal folds, at roughly 25 cycles per second.",
    ),
    ResearchSource(
        "Cat sounds",
        "https://cat.example.org/sounds",
        "Meowing, hissing, and purring are the sounds a cat uses to communicate. "
        "Cats purr when they are content and also when they are in pain or frightened.",
    ),
)

_LAW = (
    ResearchSource(
        "Abetment under the Code",
        "https://law.example.com/abetment",
        "Abetment is an offence that the code sets out and charges on its own terms. "
        "Abetment is a topic that the code addresses in more than one of its chapters. "
        "Abetment is discussed in this article about the code. "
        "Section 45 of the code defines abetment as intentionally aiding the doing of a thing by any act or illegal omission.",
    ),
)

_QUESTION_SHAPES = (
    ("why do cats purr", _CATS),
    ("how is abetment defined", _LAW),
)


class RankingTests(unittest.TestCase):
    """The specific sentence outranks the sentences that repeat the question."""

    def test_sentences_about_the_page_never_enter_the_pool(self) -> None:
        """"Purring is discussed in this overview of cat behaviour" says nothing.

        This is the shape of sentence a reader complained about — it repeats the
        topic and reports only that the page exists, and it was being served as
        a research finding. It is filtered as page-chrome, not as a topic rule.
        """
        for question, sources in _QUESTION_SHAPES:
            with self.subTest(question=question):
                frame = parse_query(question)
                evidence = build_evidence(question, frame, sources)
                blob = " ".join(sentence.text.lower() for sentence in evidence.sentences)
                for phrasing in ("this article", "this overview", "this guide", "will discuss"):
                    self.assertNotIn(phrasing, blob)

    def test_the_answer_sentence_is_found_even_when_it_is_last_on_the_page(self) -> None:
        for question, sources in _QUESTION_SHAPES:
            with self.subTest(question=question):
                frame = parse_query(question)
                evidence = build_evidence(question, frame, sources)
                texts = [sentence.text for sentence in evidence.sentences]
                marker = "vibration" if "cats" in question else "Section 45"
                self.assertIn(marker, " ".join(texts[:2]))
                self.assertGreater(evidence.sentences[0].score, 0.5)

    def test_page_content_is_preferred_over_the_search_blurb(self) -> None:
        source = ResearchSource(
            "Title",
            "https://example.com/a",
            snippet="A search blurb that mentions cats purring only in passing.",
            content="The page itself says purring is produced by vibrating vocal folds at 25 Hz.",
        )
        self.assertIn("vibrating vocal folds", page_text_of(source))
        frame = parse_query("why do cats purr")
        evidence = build_evidence("why do cats purr", frame, (source,))
        self.assertTrue(evidence.has_page_text)
        self.assertIn("vibrating vocal folds", evidence.sentences[0].text)

    def test_near_duplicate_sentences_collapse(self) -> None:
        sources = (
            ResearchSource("A", "https://a.example/1", "Cats purr by vibrating their vocal folds at about 25 cycles per second."),
            ResearchSource("B", "https://b.example/2", "Cats purr by vibrating their vocal folds at about 25 cycles per second today."),
        )
        evidence = build_evidence("why do cats purr", parse_query("why do cats purr"), sources)
        self.assertEqual(len(evidence.sentences), 1)

    def test_unrelated_sources_still_rank_something(self) -> None:
        """A question nothing matches must not produce an empty pool."""
        sources = (ResearchSource("A", "https://a.example/1", "The river runs through the valley for many kilometres."),)
        evidence = build_evidence("quantum chromodynamics", parse_query("quantum chromodynamics"), sources)
        self.assertTrue(evidence)

    def test_no_sources_means_no_evidence(self) -> None:
        self.assertFalse(build_evidence("anything", parse_query("anything"), ()))

    def test_truncated_blurbs_are_marked_and_can_be_excluded(self) -> None:
        sources = (ResearchSource("A", "https://a.example/1", "Cats purr when content, in pain, or frightened and the rumble\u2026"),)
        frame = parse_query("why do cats purr")
        evidence = build_evidence("why do cats purr", frame, sources)
        self.assertTrue(evidence)
        self.assertEqual(evidence.take(3, complete_only=True), ())

    def test_take_skips_sentences_already_spent(self) -> None:
        frame = parse_query("why do cats purr")
        evidence = build_evidence("why do cats purr", frame, _CATS)
        first = evidence.take(1)
        rest = evidence.take(3, exclude=[sentence.text for sentence in first])
        self.assertTrue(rest)
        self.assertNotIn(first[0].text, [sentence.text for sentence in rest])


class CompositionTests(unittest.TestCase):
    """What the local answer looks like, and what it must never look like."""

    def _answer(self, question: str, sources: tuple[ResearchSource, ...]) -> str:
        frame = parse_query(question)
        return synthesize_answer(frame.subject, frame, sources, question=question)

    def test_answer_is_prose_then_supporting_detail(self) -> None:
        answer = self._answer("why do cats purr", _CATS)
        lines = answer.splitlines()
        self.assertIn("purr", lines[0])
        self.assertNotIn("**Supporting detail:**", lines[0])
        self.assertIn("**Supporting detail:**", answer)

    def test_answer_never_contains_page_chrome(self) -> None:
        """Sentences about the page itself are not shown as answers."""
        for question, sources in _QUESTION_SHAPES:
            with self.subTest(question=question):
                answer = self._answer(question, sources).lower()
                for phrasing in ("this article", "this overview", "this guide", "will discuss"):
                    self.assertNotIn(phrasing, answer)

    def test_answer_never_echoes_the_question_as_a_framing_line(self) -> None:
        for question, sources in _QUESTION_SHAPES:
            with self.subTest(question=question):
                answer = self._answer(question, sources)
                lowered = answer.lower()
                self.assertNotIn(question.lower(), lowered)
                self.assertNotIn("here is what the sources say", lowered)

    def test_attribution_names_the_source_domains_at_the_end(self) -> None:
        answer = self._answer("why do cats purr", _CATS)
        self.assertTrue(answer.rstrip().endswith("."))
        self.assertIn("Sources:", answer)
        self.assertIn("vet.example.com", answer)
        self.assertIn("cat.example.org", answer)

    def test_every_sentence_in_the_answer_came_from_a_source(self) -> None:
        answer = self._answer("why do cats purr", _CATS)
        pool = " ".join(page_text_of(source) for source in _CATS)
        for line in answer.splitlines():
            sentence = line.lstrip("\u2022 ").strip()
            if not sentence or sentence.startswith(("**", "Sources:")):
                continue
            for part in sentence.split(". "):
                if len(part) < 40:
                    continue
                self.assertIn(part[:40], pool, f"invented text: {part[:60]!r}")

    def test_summaries_alone_still_produce_a_digest(self) -> None:
        """Snippets only (no page text): bullets, and honest attribution."""
        sources = (
            ResearchSource("A", "https://a.example/1", "Cats purr when content, and also when in pain or frightened."),
            ResearchSource("B", "https://b.example/2", "Purring may help a cat heal, and it changes with stress."),
        )
        answer = self._answer("why do cats purr", sources)
        self.assertIn("Cats purr when content", answer)
        self.assertIn("Sources:", answer)

    def test_nothing_quotable_names_the_pages_instead_of_a_shell(self) -> None:
        sources = (ResearchSource("Photosynthesis Guide", "https://example.com/p", "Tiny."),)
        frame = parse_query("what is photosynthesis")
        answer = synthesize_answer(frame.subject, frame, sources, question="what is photosynthesis")
        self.assertIn("Photosynthesis Guide", answer)
        self.assertNotIn("here is what the sources say", answer.lower())

    def test_compose_answer_signature_is_evidence_first(self) -> None:
        frame = parse_query("why do cats purr")
        evidence = build_evidence("why do cats purr", frame, _CATS)
        self.assertEqual(compose_answer(evidence, _CATS), self._answer("why do cats purr", _CATS))


class NoRepetitionTests(unittest.TestCase):
    """One page, one showing: the answer's sentences do not reappear below it.

    The user saw the same blurbs once inside the answer and again as highlight
    cards underneath it. The fix is that all of those surfaces slice ONE ranked
    pool, and the answer tells the rest of the report what it took.
    """

    def test_highlights_and_sections_skip_what_the_answer_used(self) -> None:
        question = "why do cats purr"
        frame = parse_query(question)
        long_sources = tuple(
            ResearchSource(
                source.title,
                source.url,
                source.snippet + " " + " ".join(
                    f"Additional detail sentence {i} about purring that a reader has not seen yet."
                    for i in range(1, 5)
                ),
            )
            for source in _CATS
        )
        evidence = build_evidence(question, frame, long_sources)
        answer = compose_answer(evidence, long_sources, question=question)
        # The pipeline's own contract: the composer reports the sentences it
        # spent, and everything below the answer is filtered by them.
        lead, support = select_answer_sentences(evidence)
        spent = tuple(sentence.text for sentence in (*lead, *support))

        highlights = answer_highlights(frame, long_sources, evidence=evidence, used=spent)
        points = key_points(frame, long_sources, evidence=evidence, used=spent)
        sections = build_sections(frame, long_sources, evidence=evidence, used=spent)

        shown = answer
        for item in (*highlights, *points):
            self.assertNotIn(item, shown, "a highlight repeated the answer verbatim")
        for section in sections:
            for item in section.get("items", ()):
                text = str(item.get("text", ""))
                if len(text) > 45:
                    self.assertNotIn(text, shown, "a section repeated the answer verbatim")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """Explore reads the pages, and the answer is built from what they say.

    The search summaries are scrubbed of anything useful on purpose: if the
    answer only knows the blurb, it cannot answer; if reading worked, it does.
    """

    # Relevant enough to pass the search gate, useless enough that a blurb-based
    # answer could not say anything: the page text is the only real evidence.
    _SUMMARIES = (
        ResearchSource(
            "Why do cats purr? Purring explained",
            "https://vet.example.com/purring",
            "Cats purr in many situations and the reasons are not always obvious.",
        ),
        ResearchSource(
            "Cat purring meaning",
            "https://cat.example.org/sounds",
            "Purring is a low rumble that cats produce in several situations.",
        ),
    )

    _PAGES = {
        "https://vet.example.com/purring": (
            "<html><body><p>A cat's purr comes from the rapid vibration of the vocal folds, "
            "at roughly 25 cycles per second, which is far too low for a meow.</p>"
            "</body></html>"
        ),
        "https://cat.example.org/sounds": (
            "<html><body><p>Cats also purr when they are frightened or in pain, so the sound "
            "is not a reliable sign of contentment on its own.</p></body></html>"
        ),
    }

    class _Search:
        def __init__(self, results: tuple[ResearchSource, ...]) -> None:
            self._results = results

        async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
            return self._results[:limit]

    class _Reader:
        """Page reader over fixture HTML (real extraction, no network)."""

        def __init__(self, pages: dict[str, str]) -> None:
            self.pages = pages

        async def read_many(self, urls: Sequence[str], *, limit: int | None = None) -> dict[str, str]:
            from novacontrol.explore.page_reader import extract_prose

            found: dict[str, str] = {}
            for url in urls:
                html = self.pages.get(url)
                if not html:
                    continue
                if text := extract_prose(html):
                    found[url] = text
            return found

    async def _research(self, reader: object) -> object:
        service = ExploreService(
            search_provider=self._Search(self._SUMMARIES),
            # The empty wiki double keeps the fallback provider offline: this
            # test is about page reading, not about where else results come from.
            wiki_provider=self._Search(()),
            video_provider=FakeVideoProvider(),
            page_reader=reader,  # type: ignore[arg-type]
        )
        return await service.research(ExploreRequest("why do cats purr"))

    async def test_sources_carry_the_page_text(self) -> None:
        report = await self._research(self._Reader(self._PAGES))
        contents = [source.content for source in report.sources]
        self.assertTrue(all(contents), "every readable page must attach its text")
        self.assertIn("vocal folds", " ".join(contents))

    async def test_answer_quotes_the_page_not_the_summary(self) -> None:
        """The answer says what the PAGE says — the blurb could not have."""
        report = await self._research(self._Reader(self._PAGES))
        self.assertIn("vocal folds", report.answer)
        self.assertIn("25 cycles per second", report.answer)
        for source in report.sources:
            if source.snippet:
                self.assertNotIn(source.snippet.rstrip("."), report.answer)

    async def test_unreadable_pages_fall_back_to_the_summaries(self) -> None:
        from conftest import OfflinePageReader

        report = await self._research(OfflinePageReader())
        self.assertTrue(report.answer.strip())
        self.assertTrue(any("could not be read" in warning for warning in report.warnings))

    async def test_reading_can_be_switched_off(self) -> None:
        service = ExploreService(
            search_provider=self._Search(self._SUMMARIES),
            wiki_provider=self._Search(()),
            video_provider=FakeVideoProvider(),
            page_reader=self._Reader(self._PAGES),  # type: ignore[arg-type]
            read_pages=False,
        )
        report = await service.research(ExploreRequest("why do cats purr"))
        self.assertFalse(any(source.content for source in report.sources))


if __name__ == "__main__":
    unittest.main()
