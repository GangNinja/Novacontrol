"""Trending research topics: daily world updates as Explore suggestions.

The Explore panel's example chips used to be the same three hardcoded strings
forever. This module replaces them with topics drawn from live top-story news
headlines (Google News RSS — standard library, no API key):

  * fetched once per REFRESH_TTL (30 min) and cached in memory;
  * the visible window rotates through the pool on an hourly cadence, so two
    looks at the panel an hour apart offer DIFFERENT topics;
  * the cache key includes a day bucket, so a new day forces a fresh fetch —
    yesterday's stories cannot survive into today.

Nothing is hardcoded: no topic strings live here or anywhere else. When the
network is unavailable the provider reports ``source: "unavailable"`` and the
UI keeps its (few) static help examples — degradation, not breakage.
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections.abc import Callable

from novacontrol.explore.providers import fetch_trending_headlines

# Refetch headlines at most this often (seconds). News does not change minute
# to minute; 30 minutes keeps the feed fresh without hammering Google.
REFRESH_TTL = 30 * 60

# A new day invalidates the cache outright: the "daily updates" contract.
_DAY_BUCKET = 24 * 60 * 60

# How many topics one /explore/trending response shows.
PAGE_SIZE = 6

# Longest headline offered as a topic: RSS entries can be sentences; a
# research topic this long stops being a search query.
_MAX_TOPIC_WORDS = 14

# Noise that makes a headline useless as a research topic.
_SKIP_PATTERNS = (
    re.compile(r"\b(LIVE|live blog|updates|highlights|as it happened)(?::|\b)", re.IGNORECASE),
    re.compile(r"\b(opinion|editorial|cartoon|horoscope|quiz)\b", re.IGNORECASE),
)


def _to_topic(headline: str) -> str | None:
    """Trim a headline into a usable research topic, or None to skip."""
    text = " ".join(headline.split()).strip()
    if len(text) < 10:
        return None
    # Google News appends " - Publisher"; the research topic is the story.
    # Stripped here (not only in the fetcher) so ANY headline source is safe.
    text = re.sub(r"\s+-\s+[^-]{2,40}$", "", text).strip()
    if len(text) < 10:
        return None
    if any(pattern.search(text) for pattern in _SKIP_PATTERNS):
        return None
    # Drop attribution tails ("... says ministry official").
    words = text.split()
    if len(words) > _MAX_TOPIC_WORDS:
        words = words[:_MAX_TOPIC_WORDS]
        text = " ".join(words).rstrip(",:;–—-")
    # Trim a dangling conjunction/preposition ("... and", "... amid").
    while words and words[-1].lower() in {
        "and", "or", "but", "amid", "after", "before", "with", "as", "in",
        "on", "to", "for", "of", "the", "a", "an", "despite", "against",
    }:
        words = words[:-1]
        text = " ".join(words)
    return text or None


class TrendingTopicsProvider:
    """Cached, rotating view over live news headlines. Thread-safe."""

    def __init__(
        self,
        *,
        fetch: Callable[..., tuple[str, ...]] = fetch_trending_headlines,
        edition: str = "in",
    ) -> None:
        self._fetch = fetch
        self._edition = edition
        self._lock = threading.Lock()
        self._pool: tuple[str, ...] = ()
        self._fetched_at = 0.0
        self._day = -1

    def topics(self, *, count: int = PAGE_SIZE, exclude: tuple[str, ...] = ()) -> dict[str, object]:
        """Return `count` topics, a different window each hour.

        Response shape: {topics, source, updated_at, fetched_at}. ``source``
        is "news" when topics come from the live feed, "unavailable" when the
        fetch failed and no cached pool exists (topics is then empty — the UI
        falls back to its static help examples).
        """
        now = time.time()
        with self._lock:
            self._ensure_pool(now)
            if not self._pool:
                return {
                    "topics": [],
                    "source": "unavailable",
                    "updated_at": None,
                    "fetched_at": self._fetched_at or None,
                }
            # Deterministic hourly shuffle: every consumer within the same hour
            # sees the same rotation (cache-friendly), the next hour sees a
            # different one. Two looks in the SAME hour keep the same topics —
            # a page reload is not a slot machine.
            hour_bucket = int(now // 3600)
            rng = random.Random(hour_bucket)
            pool = list(self._pool)
            rng.shuffle(pool)
            excluded = {e.strip().lower() for e in exclude if e.strip()}
            window = [t for t in pool if t.lower() not in excluded]
            if len(window) < count:
                window = pool  # over-exclusion: better to repeat than to starve
            return {
                "topics": window[:count],
                "source": "news",
                "updated_at": int(now),
                "fetched_at": int(self._fetched_at),
            }

    def _ensure_pool(self, now: float) -> None:
        """Refresh the headline pool when stale (caller holds the lock)."""
        day = int(now // _DAY_BUCKET)
        if self._pool and self._day == day and (now - self._fetched_at) < REFRESH_TTL:
            return
        pool = tuple(
            topic
            for headline in self._fetch(edition=self._edition)
            if (topic := _to_topic(headline)) is not None
        )
        if pool:
            self._pool = pool
            self._fetched_at = now
            self._day = day
        elif not self._pool or self._day != day:
            # Fetch failed: keep yesterday's pool inside its day (stale-but-
            # real beats empty), never carry it into a NEW day.
            if self._day != day:
                self._pool = ()
            self._fetched_at = self._fetched_at if self._pool else 0.0
