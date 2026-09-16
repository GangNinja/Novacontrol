"""Research explanation orchestrator.

Coordinates query parsing, answer synthesis, section generation,
and source presentation into an ExploreReport.
"""

from __future__ import annotations

from collections.abc import Sequence

from novacontrol.explore.models import ExploreReport, ExploreRequest, ResearchSource, VideoResult
from novacontrol.explore.query import parse_query
from novacontrol.explore.synthesizer import (
    clean_snippet,
    synthesize_answer,
    try_llm_synthesis,
)
from novacontrol.explore.sections import (
    answer_highlights,
    build_sections,
    detailed_explanation,
    key_points,
    learning_path,
    next_questions,
    overview,
)
from novacontrol.explore.source_helpers import source_chips, verification


# Re-export for backward compatibility
from novacontrol.explore.query import QueryFrame  # noqa: F401


class ResearchExplainer:
    """Creates a clear, source-aware explanation from research results.

    When a completion_provider (LLM) is available, uses it to synthesize
    coherent answers from source data. Falls back to template-based
    synthesis when no LLM is configured.
    """

    def __init__(self, *, completion_provider: object | None = None) -> None:
        self._completion_provider = completion_provider

    def set_completion_provider(self, provider: object) -> None:
        """Swap the synthesis provider after construction (lazy Ollama upgrade)."""
        self._completion_provider = provider

    async def explain(
        self,
        request: ExploreRequest,
        sources: Sequence[ResearchSource],
        videos: Sequence[VideoResult],
        *,
        warnings: Sequence[str] = (),
        provider_status: str = "online",
    ) -> ExploreReport:
        frame = parse_query(request.topic)
        topic = frame.subject
        clean_sources = tuple(s for s in sources if s.source_type != "error")

        llm_answer = await try_llm_synthesis(
            self._completion_provider, topic, frame, clean_sources,
            prior_topics=request.prior_topics,
        )
        answer = llm_answer or synthesize_answer(topic, frame, clean_sources)
        if not answer:
            answer = _offline_fallback(frame)

        return ExploreReport(
            topic=topic,
            overview=overview(topic, clean_sources),
            key_points=key_points(frame, clean_sources),
            detailed_explanation=detailed_explanation(frame, clean_sources, request.depth),
            sources=clean_sources,
            videos=tuple(videos),
            answer=answer,
            answer_highlights=answer_highlights(frame, clean_sources),
            sections=build_sections(frame, clean_sources),
            source_chips=source_chips(clean_sources),
            learning_path=learning_path(frame, request.depth),
            follow_up_questions=next_questions(frame),
            warnings=tuple(warnings),
            provider_status=provider_status,
            verification=verification(clean_sources, provider_status=provider_status),
        )


def _offline_fallback(frame: QueryFrame) -> str:
    """Minimal offline answer when no sources or LLM available."""
    from novacontrol.explore.query import sentence_subject
    topic, display = frame.subject, sentence_subject(frame.subject)
    templates = {
        "how_to": f"To {topic}, start by defining your goal and breaking it into steps.",
        "recommendation": f"When choosing {topic}, define your requirements and evaluate options against them.",
        "cause": f"To understand why {topic}, identify the primary mechanism and contributing factors.",
        "ideas": f"For {topic}, consider practical, creative, low-cost, and scalable approaches.",
        "comparison": f"Evaluate each option across performance, cost, and ease of use.",
    }
    if frame.kind in templates:
        return templates[frame.kind]
    return f"{display} is a topic with multiple dimensions. Try a more specific Explore question for live sources."
