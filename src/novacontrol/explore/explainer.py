"""Research explanation orchestrator.

Coordinates query parsing, answer synthesis, section generation,
and source presentation into an ExploreReport.
"""

from __future__ import annotations

from collections.abc import Sequence

from novacontrol.explore.evidence import build_evidence
from novacontrol.explore.models import ExploreReport, ExploreRequest, ResearchSource, VideoResult
from novacontrol.explore.planner import ResearchPlan, usable_provider
from novacontrol.explore.query import parse_query
from novacontrol.explore.synthesizer import (
    local_answer,
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

    @property
    def completion_provider(self) -> object | None:
        """The live synthesis model (None = local templates).

        ExploreService reads this to plan its searches with the SAME brain that
        will write the answer, so the evidence gathered and the answer that
        uses it come from one view of the question.
        """
        return self._completion_provider

    def set_completion_provider(self, provider: object | None) -> None:
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
        question: str = "",
        plan: ResearchPlan | None = None,
    ) -> ExploreReport:
        frame = parse_query(request.topic)
        topic = frame.subject
        clean_sources = tuple(s for s in sources if s.source_type != "error")

        # The question the answer must answer: the user's own phrasing, plus the
        # planner's reading of it when the model produced one.
        ask = " ".join(str(question or request.topic).split())
        # ONE ranked pool of source sentences, built once: the answer takes the
        # best of it, and the highlights and key points take the next best, so
        # the same sentence never appears twice on one report page.
        evidence = build_evidence(ask, frame, clean_sources)
        llm_answer = await try_llm_synthesis(
            self._completion_provider, topic, frame, clean_sources,
            prior_topics=request.prior_topics,
            question=ask,
            focus=plan.focus if plan else "",
        )
        spent: tuple[str, ...]
        if llm_answer:
            # A written answer is prose, not quoted sentences, so nothing in the
            # pool has been shown yet and no sentence needs excluding.
            answer, spent = llm_answer, ()
        else:
            answer, spent = local_answer(evidence, clean_sources, topic=topic, question=ask)
        if not answer:
            answer = _offline_fallback(frame)
        warnings = (*warnings, *self._synthesis_notes(bool(llm_answer), clean_sources, bool(evidence.has_page_text)))

        # Every surface below the answer takes a DISJOINT slice of the same
        # ranked pool, in the order the reader meets them: the answer first, then
        # the highlight cards, then the key points, then the sections. Without
        # this the same sentences appeared once in the answer and again as
        # highlight cards underneath it.
        highlights = answer_highlights(frame, clean_sources, evidence=evidence, used=spent)
        points = key_points(frame, clean_sources, evidence=evidence, used=(*spent, *highlights))
        sections = build_sections(
            frame, clean_sources, evidence=evidence, used=(*spent, *highlights, *points)
        )

        return ExploreReport(
            topic=topic,
            overview=overview(topic, clean_sources),
            key_points=points,
            detailed_explanation=detailed_explanation(frame, clean_sources, request.depth),
            sources=clean_sources,
            videos=tuple(videos),
            answer=answer,
            answer_highlights=highlights,
            sections=sections,
            source_chips=source_chips(clean_sources),
            learning_path=learning_path(frame, request.depth),
            follow_up_questions=next_questions(frame),
            warnings=tuple(warnings),
            provider_status=provider_status,
            verification=verification(clean_sources, provider_status=provider_status),
        )

    def _synthesis_notes(
        self, wrote_answer: bool, sources: Sequence[ResearchSource], read_pages: bool = False
    ) -> tuple[str, ...]:
        """Say who wrote the answer — and say it plainly when nobody could.

        Without this, a research run whose brain is unreachable (a rejected
        cloud key, a stopped Ollama) looks exactly like a healthy one: the report
        is full of matched source text and nothing explains why no one answered
        the question. The failure mode must be visible, and so must the reason
        the local answer is assembled rather than written.
        """
        if wrote_answer or not sources:
            return ()
        provider = self._completion_provider
        if usable_provider(provider):
            name = str(getattr(provider, "name", "") or "the model")
            detail = str(getattr(provider, "last_error", "") or "").strip()
            if detail:
                return (
                    f"The connected brain ({name}) could not write this answer: {detail}. "
                    "What follows is assembled from the sources instead.",
                )
            return (
                f"The connected brain ({name}) returned nothing usable, so what follows "
                "is assembled from the sources instead.",
            )
        if read_pages:
            return (
                "No model is connected, so this answer is assembled from the sources' own "
                "sentences. Connect Ollama or a cloud model in Settings for a written "
                "synthesis.",
            )
        return (
            "No model is connected and the source pages could not be read, so this is a "
            "digest of search summaries rather than an answer. Connect Ollama or a cloud "
            "model in Settings, or try again.",
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
