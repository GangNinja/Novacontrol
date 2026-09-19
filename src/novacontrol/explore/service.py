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
    relevance_score,
    wiki_search_variations,
)
from novacontrol.explore.models import ExploreReport, ExploreRequest, ResearchSource, VideoResult
from novacontrol.explore.page_reader import PageReader
from novacontrol.explore.planner import ResearchPlan, plan_research
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
        page_reader: PageReader | None = None,
        read_pages: bool = True,
        cache: TtlCache[ExploreReport] | None = None,
        cache_ttl_seconds: float = 900,
        event_bus: EventBus | None = None,
    ) -> None:
        self.search_provider = search_provider or ResilientSearchProvider()
        self._wiki_provider = wiki_provider or WikipediaSearchProvider()
        self.video_provider = video_provider or YouTubeSearchVideoProvider()
        # Reading the pages is what turns a set of search blurbs into evidence
        # an answer can be built from; it is switchable only so tests and
        # offline runs can skip the network.
        self.page_reader = page_reader or PageReader()
        self.read_pages = read_pages
        self.explainer = explainer or ResearchExplainer(completion_provider=completion_provider)
        self.cache = cache or TtlCache()
        self.cache_ttl_seconds = cache_ttl_seconds
        # The synthesis brain behind the current cache namespace (set via
        # set_explore_cache_provider when the chat brain mode changes). Its
        # name rides on every cache key so a mode switch never serves a report
        # synthesized by the OLD brain.
        self._cache_provider: object | None = None
        self._event_bus = event_bus
        self._recent_topics: deque[str] = deque(maxlen=5)

    async def _emit(self, step: str, detail: str = "", *, correlation_id: str = "", **extra: Any) -> None:
        """Publish a progress event if an event bus is attached.

        ``correlation_id`` is the run-scoped id (the ExploreRequest id) so a UI
        can interleave two concurrent researches without mixing their rows.
        """
        if self._event_bus is None:
            return
        payload: dict[str, Any] = {"step": step, "detail": detail}
        payload.update(extra)
        if correlation_id:
            payload["correlation_id"] = correlation_id
        await self._event_bus.publish(Event(type="explore.progress", payload=payload, source="explore"))

    async def _announce_completion(self, topic: str) -> None:
        """Publish an explore.completed event so open tabs add the research to
        the Recent Activity timeline over SSE (the journal is recorded by the
        API layer that owns this request)."""
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(type="explore.completed", payload={"type": "research", "title": "Research complete", "detail": topic}, source="explore")
        )

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

        # The phrasing the answer will be judged against: the user's own words,
        # or the resolved topic when a vague follow-up was rewritten ("tell me
        # more about that" is not answerable, "cats purr" is).
        question = resolved if resolved != raw_input else raw_input

        cache_key = self._cache_key(request)
        cached = self.cache.get(cache_key)
        if cached is not None:
            await self._emit("cached", f"Returning cached result for '{request.topic}'", correlation_id=request.id)
            await self._announce_completion(request.topic)
            return cached

        # Search planning is the model's job when one is connected: it reads the
        # question and chooses the queries, so the evidence gathered is the
        # evidence that answers it instead of pages that share its keywords.
        plan = await plan_research(
            self.explainer.completion_provider,
            question,
            prior_topics=request.prior_topics,
            max_queries=max(2, min(request.max_sources, 4)),
        )
        if plan:
            await self._emit("planning", f"Researching: {plan.focus or question}", correlation_id=request.id, topic=request.topic)

        await self._emit("searching", f"Searching for '{request.topic}'...", correlation_id=request.id, topic=request.topic)
        sources, search_warnings = await self._search_with_retry(request, plan)
        await self._emit("sources_found", f"Found {len(sources)} source(s)", correlation_id=request.id, count=len(sources))
        sources = await self._read_sources(sources, request)

        if request.include_videos:
            await self._emit("searching_videos", "Searching for related videos...", correlation_id=request.id)
            videos, video_warnings = await self._safe_video_search(request)
            await self._emit("videos_found", f"Found {len(videos)} video(s)", correlation_id=request.id, count=len(videos))
        else:
            videos, video_warnings = ((), ())

        warnings = (*search_warnings, *video_warnings)
        await self._emit("synthesizing", "Synthesizing answer...", correlation_id=request.id)
        report = await self.explainer.explain(
            request,
            sources,
            videos,
            warnings=warnings,
            provider_status="offline" if search_warnings else "online",
            question=question,
            plan=plan,
        )
        # A degraded report (search failed → offline templates, no sources) is
        # cached only briefly so a transient blip doesn't pin the non-answer
        # for the full TTL; a healthy report earns the normal 15-minute TTL.
        degraded = bool(search_warnings)
        ttl = min(self.cache_ttl_seconds, 30.0) if degraded else self.cache_ttl_seconds
        self.cache.set(cache_key, report, ttl_seconds=ttl)

        # Track the original user input for conversation context
        # If it was a vague follow-up, keep tracking the real topic it referred to
        if resolved != raw_input and last_topic is not None:
            track_topic = last_topic
        else:
            track_topic = raw_input
        track_lower = track_topic.strip().lower()
        if track_lower and track_lower not in self._recent_topics:
            self._recent_topics.append(track_lower)

        await self._emit("complete", f"Research complete: {len(sources)} sources, {len(videos)} videos", correlation_id=request.id, topic=request.topic)
        await self._announce_completion(request.topic)
        return report

    async def _search_with_retry(
        self, request: ExploreRequest, plan: ResearchPlan | None = None
    ) -> tuple[tuple[ResearchSource, ...], tuple[str, ...]]:
        """Search once and retry once when it returns nothing usable.

        Web search providers fail transiently (rate limits, HTML shape drift,
        a dropped connection) far more often than they fail permanently — a
        retry seconds later usually succeeds. Without it, one blip produced
        the offline 'multiple dimensions' non-answer; with it, that answer
        only appears when search genuinely cannot serve the topic.
        """
        sources, warnings = await self._safe_search(request, plan)
        if sources or not warnings:
            return sources, warnings
        await self._emit("retrying", "Search came back empty — retrying once...", correlation_id=request.id)
        return await self._safe_search(request, plan)

    async def _safe_search(
        self, request: ExploreRequest, plan: ResearchPlan | None = None
    ) -> tuple[tuple[ResearchSource, ...], tuple[str, ...]]:
        try:
            sources = await self._multi_platform_search(request, plan)
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
        if not sources:
            return (
                (),
                (
                    "Online search returned no usable web results. I used the local explanation workflow instead.",
                ),
            )
        # Web search delivered something real — even a single source beats the
        # offline templates. (The old code returned () whenever Wikipedia
        # couldn't pad a 1-source result set to 2, discarding live evidence
        # and reporting 'no usable web results' for a topic search HAD just
        # answered.)
        return (sources, ())

    async def _multi_platform_search(
        self, request: ExploreRequest, plan: ResearchPlan | None = None
    ) -> tuple[ResearchSource, ...]:
        topic = request.topic
        # Queries come from the model's plan when one was produced; the
        # deterministic builder is the fallback, not the default.
        if plan:
            queries = tuple((query, "planned") for query in plan.queries)
            # Relevance is judged against the PLAN, not just the question's
            # literal words: a model-chosen angle ("cat purr vocal folds") is
            # exactly the evidence the answer needs, and the keyword gate must
            # not throw it away for failing to repeat the user's phrasing.
            relevance_reference = " ".join(plan.queries)
        else:
            queries = build_search_queries(topic, request.max_sources)
            relevance_reference = topic
        limit_each = max(4, min(request.max_sources + 2, 6))
        # Collect a candidate POOL across queries, then keep the best-scoring
        # ones. First-pass-wins let a generic match (a Wikipedia page sharing
        # one keyword with the topic) take a slot a genuinely on-topic news
        # story earned later in the list.
        candidates: list[tuple[float, str, ResearchSource]] = []
        seen: set[str] = set()
        for query, source_type in queries:
            try:
                raw_results = await self.search_provider.search(query, limit=limit_each)
            except Exception:
                continue
            for source in raw_results:
                key = _source_key(source.url)
                if not key or key in seen:
                    continue
                if not is_relevant(source.title, source.snippet, source.url, relevance_reference):
                    continue
                seen.add(key)
                score = relevance_score(source.title, source.snippet, source.url, relevance_reference)
                candidates.append((score, source_type, source))
        candidates.sort(key=lambda item: item[0], reverse=True)
        # Relevance cutoff: keep sources scoring at least half of the best
        # candidate (absolute floor 1.0). A page that merely shares one keyword
        # with a well-covered topic scores far below the real coverage and is
        # NOT used as filler; when nothing strong exists, everything relevant
        # that passed still ships (weak-topic answers stay better than none).
        kept = [c for c in candidates if c[0] >= max(1.0, 0.5 * candidates[0][0])]
        if not kept:
            kept = candidates[:1]
        return tuple(
            replace(source, source_type=source_type)
            for _score, source_type, source in kept[: request.max_sources]
        )

    async def _read_sources(
        self, sources: tuple[ResearchSource, ...], request: ExploreRequest
    ) -> tuple[ResearchSource, ...]:
        """Attach the readable prose of each source page to the source.

        A search result's blurb is a 150-character meta description, which is
        why an answer built only from blurbs can never say more than "here are
        the pages that mention your keywords". The page itself holds the
        paragraph that answers the question; this is where it enters the run.
        Sources whose pages cannot be read keep working through their blurb, so
        this is strictly an upgrade.
        """
        if not self.read_pages or not sources:
            return sources
        await self._emit(
            "reading", f"Reading {len(sources)} source page(s)...", correlation_id=request.id
        )
        texts = await self.page_reader.read_many([source.url for source in sources], limit=len(sources))
        if not texts:
            await self._emit(
                "reading_failed",
                "Source pages could not be read; using search summaries instead.",
                correlation_id=request.id,
            )
            return sources
        read = tuple(
            replace(source, content=texts.get(source.url, "")) if texts.get(source.url) else source
            for source in sources
        )
        await self._emit(
            "read",
            f"Read {len(texts)} of {len(sources)} source page(s).",
            correlation_id=request.id,
            count=len(texts),
        )
        return read

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


    def _cache_key(self, request: ExploreRequest) -> str:
        """Request identity PLUS the synthesis brain: switching the chat brain
        (scratch <-> llm <-> cloud) must never serve a report synthesized by
        the OLD mode."""
        parts = [
            request.topic.strip().lower(),
            request.depth,
            str(request.include_videos),
            str(request.max_sources),
            str(request.max_videos),
        ]
        if request.prior_topics:
            parts.append("|".join(request.prior_topics))
        parts.append(str(getattr(getattr(self, "_cache_provider", None), "name", "templates")))
        return "|".join(parts)


def set_explore_cache_provider(service: ExploreService, provider: object | None) -> None:
    """Stamp the service's cache namespace with the synthesis provider.

    Called by the application whenever the brain mode changes; the provider
    name rides on every cache key (via ExploreService._cache_key) so reports
    synthesized by different brains never collide. ``templates`` = local
    no-LLM synthesis.
    """
    service._cache_provider = provider


def _source_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path}".rstrip("/")


