"""Explore orchestration service."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from novacontrol.explore.explainer import ResearchExplainer
from novacontrol.explore.query import (
    build_search_queries,
    extract_search_topic,
    is_relevant,
    is_wiki_film_result,
    wiki_search_variations,
)
from novacontrol.explore.models import ExploreReport, ExploreRequest, ResearchSource, VideoResult
from novacontrol.explore.providers import (
    ResilientSearchProvider,
    SearchProvider,
    VideoProvider,
    YouTubeSearchVideoProvider,
)
from novacontrol.core.events import Event, EventBus
from novacontrol.explore.web_search import WikipediaSearchProvider
from novacontrol.performance import TtlCache


class ExploreService:
    """Runs online research, explanation, and related video discovery."""

    def __init__(
        self,
        *,
        search_provider: SearchProvider | None = None,
        video_provider: VideoProvider | None = None,
        wiki_provider: SearchProvider | None = None,
        explainer: ResearchExplainer | None = None,
        completion_provider: object | None = None,
        cache: TtlCache[ExploreReport] | None = None,
        cache_ttl_seconds: float = 900,
        event_bus: EventBus | None = None,
    ) -> None:
        self.search_provider = search_provider or ResilientSearchProvider()
        self._wiki_provider = wiki_provider or WikipediaSearchProvider()
        self.video_provider = video_provider or YouTubeSearchVideoProvider()
        self.explainer = explainer or ResearchExplainer(completion_provider=completion_provider)
        self.cache = cache or TtlCache()
        self.cache_ttl_seconds = cache_ttl_seconds
        self._event_bus = event_bus
        self._recent_topics: deque[str] = deque(maxlen=5)

    async def _emit(self, step: str, detail: str = "", **extra: Any) -> None:
        """Publish a progress event if an event bus is attached."""
        if self._event_bus is None:
            return
        payload: dict[str, Any] = {"step": step, "detail": detail}
        payload.update(extra)
        await self._event_bus.publish(Event(type="explore.progress", payload=payload, source="explore"))

    async def research(self, request: ExploreRequest) -> ExploreReport:
        from novacontrol.explore.query import resolve_followup_topic

        # Save the raw user input before any resolution
        raw_input = request.topic.strip()

        # Inject conversation context if not already provided and topic is new
        topic_lower = raw_input.lower()
        if not request.prior_topics and self._recent_topics and topic_lower not in self._recent_topics:
            request = replace(request, prior_topics=tuple(self._recent_topics))

        # Resolve vague follow-up references to concrete topics
        last_topic = self._recent_topics[-1] if self._recent_topics else None
        resolved = resolve_followup_topic(request.topic, last_topic)
        if resolved != request.topic:
            request = replace(request, topic=resolved)

        cache_key = _cache_key(request)
        cached = self.cache.get(cache_key)
        if cached is not None:
            await self._emit("cached", f"Returning cached result for '{request.topic}'")
            return cached

        await self._emit("searching", f"Searching for '{request.topic}'...", topic=request.topic)
        sources, search_warnings = await self._safe_search(request)
        await self._emit("sources_found", f"Found {len(sources)} source(s)", count=len(sources))

        if request.include_videos:
            await self._emit("searching_videos", "Searching for related videos...")
            videos, video_warnings = await self._safe_video_search(request)
            await self._emit("videos_found", f"Found {len(videos)} video(s)", count=len(videos))
        else:
            videos, video_warnings = ((), ())

        warnings = (*search_warnings, *video_warnings)
        await self._emit("synthesizing", "Synthesizing answer...")
        report = await self.explainer.explain(
            request,
            sources,
            videos,
            warnings=warnings,
            provider_status="offline" if search_warnings else "online",
        )
        self.cache.set(cache_key, report, ttl_seconds=self.cache_ttl_seconds)

        # Track the original user input for conversation context
        # If it was a vague follow-up, keep tracking the real topic it referred to
        if resolved != raw_input and last_topic is not None:
            track_topic = last_topic
        else:
            track_topic = raw_input
        track_lower = track_topic.strip().lower()
        if track_lower and track_lower not in self._recent_topics:
            self._recent_topics.append(track_lower)

        await self._emit("complete", f"Research complete: {len(sources)} sources, {len(videos)} videos", topic=request.topic)
        return report

    async def _safe_search(self, request: ExploreRequest) -> tuple[tuple[ResearchSource, ...], tuple[str, ...]]:
        try:
            sources = await self._multi_platform_search(request)
        except Exception:
            return (
                (),
                (
                    "Online search is blocked or unavailable right now. I used the local explanation workflow instead.",
                ),
            )
        # Try Wikipedia as a fallback when we have too few web results
        if len(sources) < 2:
            try:
                wiki_topics = wiki_search_variations(request.topic)
                for wiki_topic in wiki_topics:
                    if not wiki_topic:
                        continue
                    try:
                        wiki_results = await self._wiki_provider.search(wiki_topic, limit=3)
                        if wiki_results:
                            # Filter out obviously off-topic Wikipedia results
                            # (films, disambiguation, unrelated 'Budget' articles, etc.)
                            good = [
                                r for r in wiki_results
                                if not is_wiki_film_result(r.title, r.snippet)
                                and is_relevant(r.title, r.snippet, r.url, request.topic)
                            ]
                            if good:
                                # Merge with any existing web results
                                all_sources = list(sources)
                                for w in good:
                                    if not any(w.url == s.url for s in all_sources):
                                        all_sources.append(w)
                                return (tuple(all_sources[:request.max_sources]), ())
                    except Exception:
                        continue
            except Exception:
                pass
            return (
                (),
                (
                    "Online search returned no usable web results. I used the local explanation workflow instead.",
                ),
            )
        return (sources, ())

    async def _multi_platform_search(self, request: ExploreRequest) -> tuple[ResearchSource, ...]:
        topic = request.topic
        # Generate topic-aware search queries instead of generic ones
        queries = build_search_queries(topic, request.max_sources)
        limit_each = max(2, min(request.max_sources, 4))
        merged: list[ResearchSource] = []
        seen: set[str] = set()
        for query, source_type in queries:
            if len(merged) >= request.max_sources:
                break
            try:
                raw_results = await self.search_provider.search(query, limit=limit_each)
            except Exception:
                continue
            for source in raw_results:
                key = _source_key(source.url)
                if not key or key in seen:
                    continue
                # Filter out clearly irrelevant results
                if not is_relevant(source.title, source.snippet, source.url, topic):
                    continue
                seen.add(key)
                merged.append(replace(source, source_type=source_type))
                if len(merged) >= request.max_sources:
                    break
        return tuple(merged)

    async def _safe_video_search(self, request: ExploreRequest) -> tuple[tuple[VideoResult, ...], tuple[str, ...]]:
        try:
            return (await self.video_provider.search_videos(request.topic, limit=request.max_videos), ())
        except Exception:
            return (
                (),
                (
                    "Video search is unavailable right now. Try again after internet access is allowed.",
                ),
            )


def _cache_key(request: ExploreRequest) -> str:
    parts = [
        request.topic.strip().lower(),
        request.depth,
        str(request.include_videos),
        str(request.max_sources),
        str(request.max_videos),
    ]
    if request.prior_topics:
        parts.append("|".join(request.prior_topics))
    return "|".join(parts)


def _source_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path}".rstrip("/")


