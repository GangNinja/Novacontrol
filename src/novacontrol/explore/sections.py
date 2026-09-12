"""Section generation: facts + sources → structured report sections.

Owns: building sections, highlights, key points, learning paths,
next questions, overview text, offline fallbacks.
"""

from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any

from novacontrol.explore.query import QueryFrame, sentence_subject, display_subject
from novacontrol.explore.synthesizer import clean_snippet, extract_facts
from novacontrol.explore.source_helpers import clean_source_title, domain as _domain
from novacontrol.explore.models import ResearchSource, VideoResult


# ────────────────────────────────────────────────────────────
# Answer highlights
# ────────────────────────────────────────────────────────────

def answer_highlights(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[str, ...]:
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return _comparison_highlights(frame)
    if frame.kind == "ideas":
        return _ideas_highlights(frame, sources)
    highlights = _extract_highlights_from_sources(frame.subject, sources)
    return highlights or (
        f"Research {len(sources)} source(s) for information about {frame.subject}.",
        "Check the source links for detailed explanations and evidence.",
        "Compare information across multiple sources for the most reliable understanding.",
    )


def _extract_highlights_from_sources(topic: str, sources: Sequence[ResearchSource]) -> tuple[str, ...]:
    highlights, seen = [], set()
    for source in sources:
        snippet = (source.snippet or "").strip()
        if not snippet or len(snippet) < 20:
            continue
        for sentence in re.split(r'(?<=[.!?])\s+', snippet):
            cleaned = clean_snippet(sentence)
            if not cleaned or len(cleaned) < 20:
                continue
            lower = cleaned.lower()
            if any(s in lower for s in ("click here", "read more", "learn more", "visit ", "see more")):
                continue
            key = lower[:60]
            if key in seen:
                continue
            seen.add(key)
            highlights.append(cleaned)
            if len(highlights) >= 4:
                return tuple(highlights)
    return tuple(highlights)


def _comparison_highlights(frame: QueryFrame) -> tuple[str, ...]:
    highlights = tuple(_comparison_clause(item) for item in frame.items[:4])
    if frame.context:
        return (*highlights[:3], f"Judge all options against the same goal: {frame.context}.")
    return highlights


def _ideas_highlights(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[str, ...]:
    if sources:
        h = _extract_highlights_from_sources(frame.subject, sources)
        if h:
            return h[:4]
    return (
        "Start with one practical idea you can do immediately.",
        "Add one creative variation so the result does not feel generic.",
        "Keep cost, setup, and cleanup small for the first version.",
        "Save the best version as a repeatable template.",
    )


def _comparison_clause(item: str) -> str:
    return f"Choose {item} when its strengths match your highest-priority constraint"


# ────────────────────────────────────────────────────────────
# Sections
# ────────────────────────────────────────────────────────────

def build_sections(frame: QueryFrame, sources: Sequence[ResearchSource], videos: Sequence[VideoResult], depth: str) -> tuple[dict[str, Any], ...]:
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return _comparison_sections(frame, sources)
    if frame.kind == "ideas":
        return _ideas_sections(frame, sources)
    if not sources:
        return _offline_sections(frame)
    return _source_based_sections(frame, sources)


def _source_based_sections(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[dict[str, Any], ...]:
    topic = frame.subject
    sections: list[dict[str, Any]] = []

    if frame.kind in ("how_to", "ideas"):
        kw_groups = [
            ("How To Get Started", ("step", "first", "start", "begin", "need", "create", "plan", "make", "set up", "organize", "include", "prepare"), 5),
            ("Ideas And Tips", ("tip", "idea", "try", "use", "add", "include", "creative", "fun", "great", "best", "easy", "simple"), 4),
            ("What You Need", ("need", "require", "must have", "essential", "material", "supply", "tool", "equipment"), 3),
        ]
    elif frame.kind in ("explanation", "mechanism"):
        kw_groups = [
            ("What It Is", ("is", "are", "defined", "known as", "means", "refers to", "consists of", "involves", "type", "form"), 3),
            ("How It Works", ("works", "process", "step", "method", "technique", "uses", "operates", "functions", "happens"), 4),
        ]
    else:
        kw_groups = []

    for title, keywords, max_items in kw_groups:
        items = _extract_section_items(sources, keywords=keywords, max_items=max_items)
        if items:
            sections.append({"title": title, "items": items})

    if not sections:
        all_facts = extract_facts(sources)
        if all_facts:
            sections.append({"title": "Key Findings", "items": tuple({"text": f, "source_indices": ()} for f in all_facts[:5])})

    if sources:
        ref_items = tuple(
            {"text": f"{clean_source_title(s.title, s.url)}: {text}",
             "source_indices": (i,)}
            for i, s in enumerate(sources[:4], start=1)
            if (text := clean_snippet(s.snippet or "")) and len(text) > 10
        )
        if ref_items:
            sections.append({"title": "From The Sources", "items": ref_items})

    return tuple(sections) if sections else _offline_sections(frame)


def _extract_section_items(sources: Sequence[ResearchSource], *, keywords: tuple[str, ...], max_items: int = 3) -> tuple[dict[str, Any], ...]:
    items, seen = [], set()
    for index, source in enumerate(sources, start=1):
        snippet = (source.snippet or "").strip()
        if not snippet:
            continue
        for sentence in re.split(r'(?<=[.!?])\s+', snippet):
            lower = sentence.lower()
            if not any(kw in lower for kw in keywords):
                continue
            cleaned = clean_snippet(sentence)
            if not cleaned or len(cleaned) < 20:
                continue
            key = cleaned.lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            items.append({"text": cleaned, "source_indices": (index,)})
            if len(items) >= max_items:
                return tuple(items)
    return tuple(items)


def _matching_source_indices(sources: Sequence[ResearchSource], terms: tuple[str, ...]) -> tuple[int, ...]:
    """Find source indices whose title or snippet contain any of the terms."""
    matched: list[int] = []
    for i, s in enumerate(sources, start=1):
        hay = f"{s.title} {s.snippet or ''}".lower()
        if any(t.lower() in hay for t in terms):
            matched.append(i)
    return tuple(matched)


def _comparison_sections(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[dict[str, Any], ...]:
    quick_items = tuple(
        {"text": _comparison_clause(item), "source_indices": _matching_source_indices(sources, (item,))}
        for item in frame.items[:4]
    )
    return (
        {"title": "Quick Comparison", "items": quick_items},
        {"title": "How To Decide", "items": (
            {"text": f"Define what matters most for {frame.context or 'your goal'} before choosing.", "source_indices": ()},
            {"text": "Check effort, cost, learning curve, and long-term fit for each option.", "source_indices": ()},
            {"text": "If two options seem tied, run a small real-world trial to compare actual results.", "source_indices": ()},
        )},
    )


def _ideas_sections(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[dict[str, Any], ...]:
    topic = frame.subject
    if sources:
        items = tuple(
            {"text": text, "source_indices": (i,)}
            for i, s in enumerate(sources[:5], start=1)
            if (text := clean_snippet(s.snippet or s.title)) and len(text) > 15
        )
        if items:
            return (
                {"title": "Ideas From Research", "items": items[:4]},
                {"title": "Try These First", "items": (
                    {"text": "Pick the easiest idea from above and try it this week.", "source_indices": ()},
                    {"text": "Add one personal detail to make it feel intentional.", "source_indices": ()},
                    {"text": "Save what worked as a template for next time.", "source_indices": ()},
                )},
            )
    return (
        {"title": "Try These First", "items": (
            {"text": f"Make the smallest useful version of {topic} and test it once.", "source_indices": ()},
            {"text": "Create a low-cost version using what you already have.", "source_indices": ()},
            {"text": "Add one unusual constraint or personal detail for a creative version.", "source_indices": ()},
        )},
        {"title": "Choose The Best Idea", "items": (
            {"text": "Pick the idea with the clearest outcome and fewest blockers.", "source_indices": ()},
            {"text": "Avoid ideas that need too much setup before you know if they work.", "source_indices": ()},
            {"text": "Save the winning version as a repeatable template.", "source_indices": ()},
        )},
    )


def _offline_sections(frame: QueryFrame) -> tuple[dict[str, Any], ...]:
    topic = frame.subject
    return (
        {"title": "Understanding " + sentence_subject(topic), "items": (
            {"text": f"Identify the core purpose of {topic}: what problem does it solve?", "source_indices": ()},
            {"text": f"Examine the key components or principles that make {topic} work.", "source_indices": ()},
            {"text": f"Review real-world examples to see {topic} in action.", "source_indices": ()},
        )},
        {"title": "Next Steps", "items": (
            {"text": "Online research will provide specific data, expert opinions, and current information.", "source_indices": ()},
            {"text": "Compare information from multiple independent sources for reliability.", "source_indices": ()},
            {"text": "Check the sources below once they become available for detailed evidence.", "source_indices": ()},
        )},
    )


# ────────────────────────────────────────────────────────────
# Key points
# ────────────────────────────────────────────────────────────

def key_points(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[str, ...]:
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return _comparison_highlights(frame)
    if frame.kind == "ideas":
        return _ideas_highlights(frame, sources)
    points, seen = [], set()
    for source in sources[:5]:
        snippet = (source.snippet or "").strip()
        if not snippet or len(snippet) < 20:
            continue
        for sentence in re.split(r'(?<=[.!?])\s+', snippet):
            cleaned = clean_snippet(sentence)
            if not cleaned or len(cleaned) < 20:
                continue
            lower = cleaned.lower()
            if any(s in lower for s in ("click here", "read more", "visit")):
                continue
            key = lower[:60]
            if key in seen:
                continue
            seen.add(key)
            points.append(cleaned)
            break
    return tuple(points) if points else (
        f"Research {len(sources)} source(s) for information about {frame.subject}.",
        "Check the source links for detailed explanations.",
    )


# ────────────────────────────────────────────────────────────
# Detailed explanation
# ────────────────────────────────────────────────────────────

def detailed_explanation(frame: QueryFrame, sources: Sequence[ResearchSource], depth: str) -> str:
    if not sources:
        return _offline_detailed_explanation(frame, depth)
    sections = []
    for i, source in enumerate(sources[:6], start=1):
        snippet = (source.snippet or "").strip()
        title = source.title or f"Source {i}"
        cleaned = clean_snippet(snippet) if len(snippet) > 15 else ""
        if cleaned:
            sections.append(f"{i}. {title}: {cleaned}")
        else:
            sections.append(f"{i}. {title} (see source for details)")
    if depth == "deep":
        sections.append("Verification: compare agreement across sources, check whether snippets support each claim.")
    else:
        sections.append("For a quick understanding, focus on the main points above, then check the source links for deeper details.")
    return "\n".join(sections)


def _offline_detailed_explanation(frame: QueryFrame, depth: str) -> str:
    topic = frame.subject
    parts = [
        f"Here is a structural overview of {topic} based on available context:",
        f"To understand {topic}, start by identifying: what problem it solves, what its core components are, and what results it produces.",
        "Connect the concept to one concrete example to see how inputs become outputs.",
        "Check the limits: where it works well, where it struggles, and what assumptions it relies on.",
    ]
    if depth == "deep":
        parts.append("For deeper research, use the Explore feature to search multiple independent sources and compare their claims.")
    else:
        parts.append("Online research will add specific data, expert opinions, and real-world examples.")
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────
# Learning path, next questions, overview
# ────────────────────────────────────────────────────────────

def learning_path(frame: QueryFrame, depth: str) -> tuple[str, ...]:
    topic = frame.subject
    if frame.kind == "ideas":
        steps = [f"Pick one small version of {topic} to try this week.", "Add a personal detail so it feels intentional.",
                 "Keep setup and cost low for the first attempt.", "Save what worked as a reusable template."]
    elif frame.kind == "comparison" and len(frame.items) >= 2:
        steps = [f"Write the same evaluation criteria for all options: {', '.join(frame.items)}.",
                 "Decide which criterion matters most before declaring a winner.",
                 "Run a small real-world trial with the top option.", "Re-check the results after the trial."]
    else:
        steps = [f"Learn the basic definition and purpose of {topic}.",
                 "Identify the key terms and concepts that appear across sources.",
                 "Work through one real example or case study.", "Watch an introductory video for a visual explanation."]
    if depth == "deep":
        steps.append("Compare tradeoffs, limitations, and open questions across sources.")
    return tuple(steps)


def next_questions(frame: QueryFrame) -> tuple[str, ...]:
    topic = frame.subject
    if frame.kind == "ideas":
        return (f"Give me a simple checklist for {topic}.", f"What are low-budget options for {topic}?",
                f"How do I get started with {topic} today?")
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return (f"Which of {', '.join(frame.items)} is best for beginners?",
                f"What are the biggest tradeoffs between {', '.join(frame.items[:2])}?",
                f"How would I combine {', '.join(frame.items)} in a practical plan?")
    return (f"What are the most important things to know about {topic}?",
            f"What common mistakes should I avoid with {topic}?",
            f"How is {topic} used in real-world applications?")


def overview(topic: str, sources: Sequence[ResearchSource], *, provider_status: str) -> str:
    display = display_subject(topic)
    if not sources:
        return (f"Here is a structured overview of {display}. "
                "Online search returned no usable results, so I used NovaControl's local explanation workflow.")
    domains = list({d for s in sources if (d := _domain(s.url))})[:3]
    domain_text = ", ".join(domains) if domains else f"{len(sources)} sources"
    return (f"Research on {display} from {domain_text}: "
            f"{len(sources)} source(s) analyzed with key findings organized below.")

