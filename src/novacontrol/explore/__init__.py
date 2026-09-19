"""Explore research subsystem."""

from novacontrol.explore.explainer import ResearchExplainer
from novacontrol.explore.models import ExploreReport, ExploreRequest, ResearchSource, VideoResult
from novacontrol.explore.planner import ResearchPlan, plan_research
from novacontrol.explore.providers import (
    DuckDuckGoLiteSearchProvider,
    SearchProvider,
    VideoProvider,
    YouTubeSearchVideoProvider,
)
from novacontrol.explore.runtime import ExploreModule
from novacontrol.explore.service import ExploreService, set_explore_cache_provider

__all__ = [
    "DuckDuckGoLiteSearchProvider",
    "ExploreModule",
    "set_explore_cache_provider",
    "ExploreReport",
    "ExploreRequest",
    "ExploreService",
    "plan_research",
    "ResearchExplainer",
    "ResearchPlan",
    "ResearchSource",
    "SearchProvider",
    "VideoProvider",
    "VideoResult",
    "YouTubeSearchVideoProvider",
]
