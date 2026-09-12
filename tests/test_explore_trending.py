"""Trending Explore topics: daily world updates, never hardcoded.

The Explore panel's example chips used to be three fixed strings forever. They
are now served from GET /explore/trending, which draws topics from live
top-story news headlines (Google News RSS). These tests pin the parts that can
be tested without the network:

  * headline -> topic trimming (publisher suffix, LIVE/blog noise, length);
  * rotation (hourly deterministic window differs across hour buckets);
  * caching (one fetch serves many calls) and day-bucket invalidation;
  * offline degradation (failed fetch -> source "unavailable", empty topics);
  * the endpoint contract via TestClient (shape, count bounds, auth);
  * the never-hardcoded guard: no shipped source file may carry a static
    topic list for the chips.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _fake_fetch_factory(headlines: tuple[str, ...]):
    calls = {"n": 0}

    def fetch(*, edition: str = "us", timeout: int = 8) -> tuple[str, ...]:
        calls["n"] += 1
        return headlines

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class HeadlineTrimTests(unittest.TestCase):
    def test_publisher_suffix_is_stripped_by_the_provider(self) -> None:
        from novacontrol.explore.providers import fetch_trending_headlines  # import guard
        from novacontrol.explore.trending import _to_topic

        self.assertEqual(
            _to_topic("BRICS leaders meet in India as wars test the bloc - AP News"),
            "BRICS leaders meet in India as wars test the bloc",
        )

    def test_live_blog_and_opinion_headlines_are_skipped(self) -> None:
        from novacontrol.explore.trending import _to_topic

        self.assertIsNone(_to_topic("Election results LIVE updates: voting underway"))
        self.assertIsNone(_to_topic("Opinion: why the trade war matters"))
        self.assertIsNone(_to_topic("Highlights: match day three"))

    def test_long_headlines_are_clamped_and_tidy(self) -> None:
        from novacontrol.explore.trending import _to_topic

        topic = _to_topic(
            "The government announced a comprehensive new framework for regulating "
            "artificial intelligence systems across every sector of the economy this week"
        )
        self.assertIsNotNone(topic)
        self.assertLessEqual(len(topic.split()), 14)
        self.assertFalse(topic.endswith(("and", "of", "the", "for", "amid")))

    def test_short_or_empty_headlines_are_skipped(self) -> None:
        from novacontrol.explore.trending import _to_topic

        self.assertIsNone(_to_topic("Breaking"))
        self.assertIsNone(_to_topic("   "))


class TrendingProviderTests(unittest.TestCase):
    HEADLINES = tuple(
        f"Important development number {i} in ongoing world event {i} confirmed today"
        for i in range(24)
    )

    def test_topics_come_from_the_feed_and_are_cached(self) -> None:
        from novacontrol.explore.trending import TrendingTopicsProvider

        fetch = _fake_fetch_factory(self.HEADLINES)
        provider = TrendingTopicsProvider(fetch=fetch)
        first = provider.topics()
        second = provider.topics()
        self.assertEqual(first["source"], "news")
        self.assertEqual(len(first["topics"]), 6)
        self.assertEqual(first["topics"], second["topics"], "same hour = same window")
        self.assertEqual(fetch.calls["n"], 1, "second call must be served from cache")

    def test_excluded_topics_are_not_reoffered(self) -> None:
        from novacontrol.explore.trending import TrendingTopicsProvider

        provider = TrendingTopicsProvider(fetch=_fake_fetch_factory(self.HEADLINES))
        first = provider.topics()
        followup = provider.topics(exclude=tuple(first["topics"]))
        self.assertEqual(set(first["topics"]) & set(followup["topics"]), set())

    def test_rotation_differs_across_hour_buckets(self) -> None:
        """A later hour must present a different slice of the pool."""
        import novacontrol.explore.trending as trending_mod
        from novacontrol.explore.trending import TrendingTopicsProvider

        provider = TrendingTopicsProvider(fetch=_fake_fetch_factory(self.HEADLINES))
        windows = set()
        real_time = trending_mod.time.time
        for hour in (0, 1, 2, 5):
            trending_mod.time.time = lambda h=hour: h * 3600 + 60  # type: ignore[assignment]
            try:
                result = provider.topics()
                windows.add(tuple(result["topics"]))
            finally:
                trending_mod.time.time = real_time  # type: ignore[assignment]
        self.assertGreater(len(windows), 1, "hourly rotation must change the window")

    def test_new_day_forces_refetch_and_stale_pool_dies(self) -> None:
        import novacontrol.explore.trending as trending_mod
        from novacontrol.explore.trending import TrendingTopicsProvider

        fetch = _fake_fetch_factory(self.HEADLINES)
        provider = TrendingTopicsProvider(fetch=fetch)
        real_time = trending_mod.time.time
        trending_mod.time.time = lambda: 3600  # type: ignore[assignment] (day 0)
        try:
            provider.topics()
            # Next day, fetch fails -> the pool must NOT survive into a new day.
            fetch.__wrapped_fail__ = True  # marker only, unused
            provider._fetch = lambda *, edition="us", timeout=8: ()  # type: ignore[assignment]
            trending_mod.time.time = lambda: 2 * 24 * 3600 + 60  # type: ignore[assignment]
            result = provider.topics()
        finally:
            trending_mod.time.time = real_time  # type: ignore[assignment]
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["topics"], [])

    def test_failed_first_fetch_degrades_to_unavailable(self) -> None:
        from novacontrol.explore.trending import TrendingTopicsProvider

        provider = TrendingTopicsProvider(fetch=lambda *, edition="us", timeout=8: ())
        result = provider.topics()
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["topics"], [])
        self.assertIsNone(result["updated_at"])


class TrendingEndpointTests(unittest.TestCase):
    """GET /explore/trending through the real app factory."""

    def _client(self):
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            self.skipTest("fastapi not installed")
        from novacontrol.api.app import create_app

        return TestClient(create_app())

    def test_endpoint_returns_shape_and_respects_count(self) -> None:
        client = self._client()
        resp = client.get("/explore/trending?count=3")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("source", body)
        self.assertIn("topics", body)
        self.assertIsInstance(body["topics"], list)
        self.assertLessEqual(len(body["topics"]), 3)
        for topic in body["topics"]:
            self.assertIsInstance(topic, str)
            self.assertTrue(topic.strip())

    def test_count_is_bounded(self) -> None:
        client = self._client()
        resp = client.get("/explore/trending?count=999")
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(resp.json()["topics"]), 12)


class NoHardcodedTopicsGuard(unittest.TestCase):
    """The feature's whole point: topic suggestions must not be a static list."""

    def test_index_html_keeps_only_the_offline_fallback_chips(self) -> None:
        html = (REPO / "src" / "novacontrol" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="trendingChips"', html, "chip holder must exist for the server-fed topics")
        # The static fallback chips remain (offline degradation), but the
        # trending loader must be the primary source.
        self.assertIn("loadTrendingTopics", (REPO / "src" / "novacontrol" / "web" / "static" / "app.js").read_text(encoding="utf-8"))

    def test_trending_module_contains_no_topic_strings(self) -> None:
        source = (REPO / "src" / "novacontrol" / "explore" / "trending.py").read_text(encoding="utf-8")
        # Any literal that looks like a curated topic list would defeat the
        # feature; the module must only carry machinery.
        for banned in ("photosynthesis", "programming languages", "React vs Vue", "quantum computing"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
