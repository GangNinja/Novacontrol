"""Tests for Explore module: research, synthesis, caching, parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.explore import ExploreModule, ExploreRequest, ExploreService, ResearchSource
from novacontrol.explore.providers import _DuckDuckGoLiteParser, _extract_youtube_videos
from conftest import (
    FakeSearchProvider,
    FakeVideoProvider,
    FailingSearchProvider,
    collect_events,
)


class SynthesizingLLMProvider:
    """LLM that returns a realistic synthesized answer for testing."""

    @property
    def name(self) -> str:
        return "test-synth"

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        for msg in messages:
            if msg.get("role") == "user":
                content = msg["content"]
                if "composting" in content.lower():
                    return (
                        "Home composting is a straightforward process that transforms kitchen "
                        "scraps and yard waste into nutrient-rich soil. You need a bin, brown "
                        "materials like dry leaves for carbon, and green materials like food "
                        "scraps for nitrogen. Keep the pile moist and turn it weekly for "
                        "best results. A tumbler-style bin makes turning easy."
                    )
                return (
                    "Based on the research sources, this topic covers key concepts that "
                    "are worth understanding in depth. The sources provide complementary "
                    "perspectives that together form a comprehensive picture."
                )
        return "Fallback answer."


class ExploreSynthesisTests(unittest.IsolatedAsyncioTestCase):
    """Tests that Explore synthesizes real answers from source data."""

    def _make_service(self, search_provider=None, video_provider=None):
        return ExploreService(
            search_provider=search_provider or FakeSearchProvider(),
            video_provider=video_provider or FakeVideoProvider(),
        )

    async def test_creates_explanation_with_videos(self) -> None:
        report = await self._make_service().research(ExploreRequest("transformers in AI"))

        self.assertIn("transformers in AI", report.overview)
        self.assertEqual(report.sources[0].url, "https://example.com/basics")
        self.assertEqual(report.videos[0].channel, "Example Channel")
        self.assertTrue(report.answer)
        self.assertTrue(report.answer_highlights)
        self.assertTrue(report.sections)
        self.assertTrue(report.source_chips)
        self.assertIn("claims", report.verification)
        self.assertTrue(report.follow_up_questions)

    async def test_answer_contains_topic(self) -> None:
        report = await self._make_service().research(
            ExploreRequest("Explain how noise-cancelling headphones actually work")
        )
        self.assertEqual(report.topic, "noise-cancelling headphones")
        self.assertTrue(len(report.answer) > 50)
        self.assertIn("noise-cancelling headphones", report.answer.lower())
        self.assertTrue(len(report.sections) >= 1)

    async def test_answer_not_hardcoded(self) -> None:
        report = await self._make_service().research(
            ExploreRequest("How exactly does blockchain technology work, in simple terms?")
        )
        self.assertEqual(report.topic, "blockchain technology")
        self.assertTrue(len(report.answer) > 50)
        self.assertNotIn("How exactly", report.answer)

    async def test_answer_uses_source_snippets(self) -> None:
        class RichProvider:
            async def search(self, query, *, limit=6):
                from novacontrol.explore import ResearchSource
                return (
                    ResearchSource("Composting Guide", "https://example.com/compost",
                        "Home composting requires a bin, brown carbon materials like dry leaves, "
                        "and green nitrogen materials like food scraps."),
                    ResearchSource("Composting Tips", "https://example.com/tips",
                        "The best compost bins are tumbler style for easy turning."),
                )[:limit]

        service = self._make_service(search_provider=RichProvider())
        report = await service.research(ExploreRequest("How do I set up a home composting system?"))

        self.assertEqual(report.topic, "home composting system")
        answer_lower = report.answer.lower()
        self.assertTrue(
            any(w in answer_lower for w in ("bin", "compost", "carbon")),
            f"Answer should contain source-derived facts: {report.answer[:200]}",
        )
        self.assertNotIn("Learn how", report.answer)


class FakeWikiProvider:
    """Wikipedia provider returning deterministic results for offline tests."""

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        lower = query.lower()
        # Return topic-appropriate snippets so offline tests don't depend on live API
        if "react" in lower or "vue" in lower or "svelte" in lower:
            return (
                ResearchSource("React", "https://en.wikipedia.org/wiki/React_(JavaScript)",
                    "React is a JavaScript library for building user interfaces.", "wiki"),
                ResearchSource("Vue.js", "https://en.wikipedia.org/wiki/Vue.js",
                    "Vue.js is a progressive framework for building user interfaces.", "wiki"),
                ResearchSource("Svelte", "https://en.wikipedia.org/wiki/Svelte",
                    "Svelte is a compiler-based UI framework.", "wiki"),
            )[:limit]
        if "email" in lower or "chat" in lower or "phone" in lower:
            return (
                ResearchSource("Email", "https://en.wikipedia.org/wiki/Email",
                    "Email is a method of exchanging messages between people.", "wiki"),
                ResearchSource("Instant messaging", "https://en.wikipedia.org/wiki/Instant_messaging",
                    "Instant messaging is a type of online chat.", "wiki"),
                ResearchSource("Telephone", "https://en.wikipedia.org/wiki/Telephone",
                    "A telephone is a telecommunications device.", "wiki"),
            )[:limit]
        return ()


class ExploreOfflineTests(unittest.IsolatedAsyncioTestCase):
    """Tests for Explore when search fails (offline fallback)."""

    def _make_service(self):
        return ExploreService(
            search_provider=FailingSearchProvider(),
            video_provider=FakeVideoProvider(),
            wiki_provider=FakeWikiProvider(),
        )

    async def test_offline_gives_structural_answer(self) -> None:
        report = await self._make_service().research(
            ExploreRequest("How exactly does photosynthesis work in simple terms?")
        )
        self.assertEqual(report.topic, "photosynthesis")
        self.assertTrue(len(report.answer) > 50)
        self.assertNotIn("How exactly", report.answer)

    async def test_comparison_works_offline(self) -> None:
        report = await self._make_service().research(
            ExploreRequest("Compare React, Vue, and Svelte for building a dashboard")
        )
        self.assertEqual(report.topic, "React, Vue, and Svelte")
        self.assertIn("For building a dashboard", report.answer)
        section_titles = [s["title"] for s in report.sections]
        self.assertIn("Quick Comparison", section_titles)
        self.assertIn("How To Decide", section_titles)

    async def test_unseen_comparison_works(self) -> None:
        report = await self._make_service().research(
            ExploreRequest("Compare email, chat, and phone calls for customer support")
        )
        self.assertIn("For customer support", report.answer)
        section_titles = [s["title"] for s in report.sections]
        self.assertIn("Quick Comparison", section_titles)
        self.assertIn("How To Decide", section_titles)

    async def test_cause_and_recommendation(self) -> None:
        cause = await self._make_service().research(
            ExploreRequest("Why do leaves change color in autumn?")
        )
        rec = await self._make_service().research(
            ExploreRequest("Recommend a laptop for video editing under 1000")
        )
        self.assertEqual(cause.topic, "leaves change color in autumn")
        self.assertTrue(len(cause.answer) > 50)
        self.assertNotIn("Why do", cause.answer)
        self.assertIn("laptop for video editing under 1000", rec.answer.lower())

    async def test_search_errors_become_warnings(self) -> None:
        report = await self._make_service().research(ExploreRequest("chatgpt"))
        self.assertTrue(len(report.key_points) > 0)
        self.assertNotIn("socket blocked", " ".join(report.key_points))


class ExploreCachingTests(unittest.IsolatedAsyncioTestCase):

    async def test_repeated_request_returns_cached(self) -> None:
        search = FakeSearchProvider()
        service = ExploreService(search_provider=search, video_provider=FakeVideoProvider())
        request = ExploreRequest("cached topic")

        first = await service.research(request)
        calls_after = search.calls
        second = await service.research(request)

        self.assertEqual(first.id, second.id)
        self.assertEqual(search.calls, calls_after)


class ExploreProgressTests(unittest.IsolatedAsyncioTestCase):
    """Tests for progress events emitted during research."""

    async def test_service_emits_progress_events(self) -> None:
        bus = EventBus()
        service = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            event_bus=bus,
        )
        seen = await collect_events(
            bus, "explore.progress",
            "explore.topic_requested", {"topic": "test"},
            start_fn=service.start if hasattr(service, "start") else None,
        )
        # Since service doesn't have start(), we need to call research directly
        # and collect events ourselves
        bus2 = EventBus()
        service2 = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            event_bus=bus2,
        )
        events: list[Event] = []

        async def capture(event: Event) -> None:
            events.append(event)

        await bus2.subscribe("explore.progress", capture)
        await service2.research(ExploreRequest("quantum computing"))

        steps = [e.payload["step"] for e in events]
        self.assertIn("searching", steps)
        self.assertIn("sources_found", steps)
        self.assertIn("searching_videos", steps)
        self.assertIn("videos_found", steps)
        self.assertIn("synthesizing", steps)
        self.assertIn("complete", steps)

    async def test_service_emits_cached_event(self) -> None:
        bus = EventBus()
        service = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            event_bus=bus,
        )
        events: list[Event] = []

        async def capture(event: Event) -> None:
            events.append(event)

        await bus.subscribe("explore.progress", capture)
        request = ExploreRequest("cached progress topic")
        await service.research(request)
        first_steps = [e.payload["step"] for e in events]
        self.assertIn("searching", first_steps)
        self.assertIn("complete", first_steps)

        # Second call should emit cached
        events.clear()
        await service.research(request)
        second_steps = [e.payload["step"] for e in events]
        self.assertIn("cached", second_steps)
        self.assertNotIn("searching", second_steps)

    async def test_service_without_bus_emits_nothing(self) -> None:
        service = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
        )
        # Should not raise
        report = await service.research(ExploreRequest("no bus topic"))
        self.assertTrue(report.answer)


class ExploreEventTests(unittest.IsolatedAsyncioTestCase):

    async def test_module_emits_report_event(self) -> None:
        bus = EventBus()
        service = ExploreService(
            search_provider=FakeSearchProvider(), video_provider=FakeVideoProvider()
        )
        module = ExploreModule(service)
        seen = await collect_events(
            bus, "explore.report_created",
            "explore.topic_requested", {"topic": "Python"},
            start_fn=module.start,
        )
        self.assertEqual(seen[0].payload["topic"], "Python")
        self.assertEqual(seen[0].payload["videos"][0]["title"], "Python full course")


class ExploreLLMSynthesisTests(unittest.IsolatedAsyncioTestCase):
    """Tests for LLM-powered answer synthesis in Explore."""

    async def test_llm_synthesis_used_when_provider_available(self) -> None:
        service = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            completion_provider=SynthesizingLLMProvider(),
        )
        report = await service.research(ExploreRequest("How do I set up a home composting system?"))

        # Should use LLM-synthesized answer, not template concatenation
        self.assertIn("composting", report.answer.lower())
        self.assertTrue(len(report.answer) > 50)

    async def test_llm_synthesis_falls_back_on_failure(self) -> None:
        from conftest import FailingProvider
        service = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            completion_provider=FailingProvider(),
        )
        report = await service.research(ExploreRequest("transformers in AI"))

        # Should fall back to template answer, not crash
        self.assertTrue(report.answer)
        self.assertTrue(len(report.answer) > 20)

    async def test_llm_synthesis_skips_for_echo_provider(self) -> None:
        from conftest import EchoProvider
        service = ExploreService(
            search_provider=FakeSearchProvider(),
            video_provider=FakeVideoProvider(),
            completion_provider=EchoProvider(),
        )
        report = await service.research(ExploreRequest("quantum computing basics"))

        # Echo provider should be skipped (name is "test-echo")
        self.assertTrue(report.answer)
        self.assertNotIn("Echo:", report.answer)

    async def test_llm_synthesis_skips_when_no_sources(self) -> None:
        service = ExploreService(
            search_provider=FailingSearchProvider(),
            video_provider=FakeVideoProvider(),
            completion_provider=SynthesizingLLMProvider(),
        )
        report = await service.research(ExploreRequest("obscure topic"))

        # No sources means no LLM synthesis, should use offline fallback
        self.assertTrue(report.answer)
        self.assertTrue(len(report.answer) > 20)

    async def test_explainer_accepts_provider_directly(self) -> None:
        from novacontrol.explore.explainer import ResearchExplainer
        provider = SynthesizingLLMProvider()
        explainer = ResearchExplainer(completion_provider=provider)
        self.assertEqual(provider.name, "test-synth")


class SearchParserTests(unittest.IsolatedAsyncioTestCase):

    async def test_ddg_parser_extracts_results(self) -> None:
        parser = _DuckDuckGoLiteParser(limit=1)
        parser.feed("""
            <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com"
               class="result-link">Example</a>
            <td class="result-snippet">A useful snippet.</td>
        """)
        result = parser.results()[0]
        self.assertEqual(result.title, "Example")
        self.assertEqual(result.url, "https://example.com")
        self.assertEqual(result.snippet, "A useful snippet.")

    async def test_ddg_parser_skips_ads(self) -> None:
        parser = _DuckDuckGoLiteParser(limit=2)
        parser.feed("""
            <a rel="nofollow" href="https://duckduckgo.com/y.js?ad_domain=example.com"
               class="result-link">Ad</a>
            <td class="result-snippet">Ad text.</td>
            <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org"
               class="result-link">Real</a>
            <td class="result-snippet">Useful source.</td>
        """)
        results = parser.results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Real")

    async def test_youtube_parser_extracts_thumbnails(self) -> None:
        html = (
            '<script>var ytInitialData = {"contents":{"videoRenderer":{'
            '"videoId":"abc123","title":{"runs":[{"text":"Demo video"}]},'
            '"ownerText":{"runs":[{"text":"Demo Channel"}]},'
            '"lengthText":{"simpleText":"4:20"},'
            '"thumbnail":{"thumbnails":[{"url":"https://i.ytimg.com/vi/abc123/hqdefault.jpg"}]}'
            '}}};</script>'
        )
        videos = _extract_youtube_videos(html, limit=1)
        self.assertEqual(videos[0].title, "Demo video")
        self.assertEqual(videos[0].thumbnail_url, "https://i.ytimg.com/vi/abc123/hqdefault.jpg")


class FollowUpResolutionTests(unittest.TestCase):
    """Test vague follow-up reference resolution."""

    def test_tell_me_more_resolves(self) -> None:
        from novacontrol.explore.query import resolve_followup_topic
        result = resolve_followup_topic("tell me more about that", "quantum computing")
        self.assertIn("quantum", result)
        self.assertNotIn("that", result)

    def test_go_deeper_resolves(self) -> None:
        from novacontrol.explore.query import resolve_followup_topic
        result = resolve_followup_topic("go deeper", "machine learning")
        self.assertIn("machine learning", result)

    def test_continue_resolves(self) -> None:
        from novacontrol.explore.query import resolve_followup_topic
        result = resolve_followup_topic("continue", "photosynthesis")
        self.assertIn("photosynthesis", result)

    def test_elaborate_on_that_resolves(self) -> None:
        from novacontrol.explore.query import resolve_followup_topic
        result = resolve_followup_topic("elaborate on that", "climate change")
        self.assertIn("climate change", result)

    def test_no_prior_topic_returns_original(self) -> None:
        from novacontrol.explore.query import resolve_followup_topic
        result = resolve_followup_topic("tell me more about that", None)
        self.assertEqual(result, "tell me more about that")

    def test_real_topic_not_modified(self) -> None:
        from novacontrol.explore.query import resolve_followup_topic
        result = resolve_followup_topic("quantum entanglement explained", "photosynthesis")
        self.assertEqual(result, "quantum entanglement explained")

    def test_service_resolves_before_search(self) -> None:
        """Service should resolve vague follow-ups using recent topic history."""
        from unittest.mock import AsyncMock, MagicMock
        from novacontrol.explore.service import ExploreService

        service = ExploreService()
        service._recent_topics.append("quantum computing")
        # Mock the search to capture the resolved topic
        captured_request = []
        original_explain = service.explainer.explain

        async def capture_explain(req, *args, **kwargs):
            captured_request.append(req)
            from novacontrol.explore.models import ExploreReport
            return ExploreReport(topic=req.topic, overview="test", key_points=(), detailed_explanation="test", sources=())

        service.explainer.explain = capture_explain
        # Also mock the search to avoid actual web calls
        service.search_provider = MagicMock()
        service.search_provider.search = AsyncMock(return_value=())

        import asyncio
        asyncio.run(service.research(ExploreRequest(topic="tell me more about that")))

        self.assertTrue(len(captured_request) >= 1)
        resolved_topic = captured_request[0].topic
        self.assertIn("quantum", resolved_topic.lower())


class ShoppingQueryContextTests(unittest.TestCase):
    """Shopping queries: price/currency/buy signals drive query grounding and relevance."""

    def test_detect_shopping_context(self) -> None:
        from novacontrol.explore.query import detect_shopping_context
        cases = [
            # (topic, expected) — amounts and price wording signal buying intent
            ("best budget laptops under 500", True),
            ("best budget laptops under $500", True),
            ("recommend a laptop for video editing under 1000", True),
            ("budget friendly laptops under 50000 rupees in india", True),
            ("best budget laptops", True),  # best + budget = commerce wording
            ("cheap 4k monitor for sale", True),
            # ...but policy/fiscal topics with just the word 'budget' are not shopping
            ("how does the federal budget work", False),
            ("explain the union budget 2025", False),
            ("why buy organic food", False),
            ("diet for 3 days to lose weight", False),
        ]
        for topic, expected in cases:
            with self.subTest(topic=topic):
                self.assertEqual(detect_shopping_context(topic), expected)

    def test_shopping_query_keeps_price_constraint(self) -> None:
        """Capped amounts like 'under 1000' survive into the search query."""
        from novacontrol.explore.query import build_search_queries
        queries = build_search_queries("best laptop for video editing under 1000", 6)
        first = queries[0][0]
        self.assertIn("laptop", first)
        self.assertIn("under 1000", first)

    def test_plain_query_unchanged_by_shopping_grounding(self) -> None:
        from novacontrol.explore.query import build_search_queries
        self.assertEqual(
            build_search_queries("how does quantum computing work", 6)[0][0],
            "quantum computing",
        )

    def test_relevance_drops_fiscal_pages_for_product_queries(self) -> None:
        from novacontrol.explore.query import is_relevant
        topic = "best budget laptops under 500"
        fiscal = [
            ("The Budget | U.S. Department of the Treasury",
             "Learn about the federal budget process and how taxpayer dollars are spent.",
             "https://home.treasury.gov/policy-issues/budget"),
            ("Federal Budget: What It Is and How It Works",
             "The government's annual spending plan; Congress appropriates funds each year.",
             "https://www.whitehouse.gov/omb/budget/"),
            ("India Budget 2025",
             "Finance Minister presents the Union Budget; fiscal deficit target announced.",
             "https://www.indiabudget.gov.in/"),
        ]
        for title, snippet, url in fiscal:
            with self.subTest(title=title):
                self.assertFalse(
                    is_relevant(title, snippet, url, topic),
                    f"{title!r} must be dropped for a laptop-shopping query",
                )

    def test_relevance_keeps_real_product_pages(self) -> None:
        from novacontrol.explore.query import is_relevant
        topic = "best budget laptops under 500"
        product = [
            ("The Best Laptops Under $500 for 2026",
             "We tested budget laptops and found great Windows and Chromebook options.",
             "https://www.pcmag.com/picks/the-best-laptops-under-500"),
            ("Best budget laptops 2025: 4 laptops under $500",
             "Expert-tested picks for budget performance and everyday use.",
             "https://www.laptopmag.com/articles/best-laptop-under-500"),
        ]
        for title, snippet, url in product:
            with self.subTest(title=title):
                self.assertTrue(is_relevant(title, snippet, url, topic))

    def test_fiscal_topic_still_matches_fiscal_pages(self) -> None:
        """The same page stays relevant when the user actually asked about budgets."""
        from novacontrol.explore.query import is_relevant
        self.assertTrue(is_relevant(
            "Federal Budget: What It Is and How It Works",
            "The government's annual spending plan; Congress appropriates funds each year.",
            "https://www.whitehouse.gov/omb/budget/",
            "how does the federal budget work",
        ))

    def test_comparison_topic_matches_punctuated_sources(self) -> None:
        """Punctuation in the topic must not block matching its own sources."""
        from novacontrol.explore.query import is_relevant
        self.assertTrue(is_relevant(
            "React",
            "React is a JavaScript library for building user interfaces.",
            "https://en.wikipedia.org/wiki/React",
            "Compare React, Vue, and Svelte for building a dashboard",
        ))
        self.assertTrue(is_relevant(
            "Vue.js",
            "Vue.js is a progressive framework for building user interfaces.",
            "https://en.wikipedia.org/wiki/Vue.js",
            "Compare React, Vue, and Svelte for building a dashboard",
        ))


if __name__ == "__main__":
    unittest.main()
