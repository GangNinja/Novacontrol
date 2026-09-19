"""Section generation: ranked evidence → the report's sections and highlights.

Owns: highlights, sections, key points, learning paths, next questions,
overview text, and the offline fallbacks.

Rule-free by construction. Sections used to be assembled by matching keyword
word lists ("step", "tip", "defined as") separately per question kind, which is
why the same handful of facts reappeared under "What It Is", "How It Works",
and "Key Points". They are now slices of ONE ranked pool (see `evidence.py`):
the answer takes the best sentences, the highlights the next, the sections the
next after that — so every surface tells the reader something new, and nothing
here knows anything about the topic. Comparison keeps its question-form
structure (the options came out of the question itself, not out of a word
list), and the offline fallbacks are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import re
from typing import Any

from novacontrol.explore.evidence import Evidence, Sentence, build_evidence, page_text_of
from novacontrol.explore.query import QueryFrame, sentence_subject, display_subject
from novacontrol.explore.synthesizer import clean_snippet, extract_facts
from novacontrol.explore.source_helpers import clean_source_title, domain as _domain
from novacontrol.explore.models import ResearchSource, VideoResult  # noqa: F401  (re-exported type)

# Sentence boundary used when a source's text has to be split for display.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


# ────────────────────────────────────────────────────────────
# Ranked picks: the one pool every surface draws from
# ────────────────────────────────────────────────────────────

def _ranked_picks(
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    evidence: Evidence | None,
    count: int,
    used: Iterable[str] = (),
) -> tuple[Sentence, ...]:
    """The next best `count` sentences the report has not shown yet.

    `used` carries the sentences already spent by the answer and by any earlier
    section, which is what keeps a page from repeating itself. Complete
    sentences are preferred; truncated blurbs are used only to fill the quota.
    """
    if not sources:
        return ()
    pool = evidence if evidence is not None else build_evidence(frame.subject, frame, sources)
    if not pool:
        return ()
    picked = pool.take(count, exclude=used, complete_only=True)
    if len(picked) < count:
        already = (*used, *(pick.text for pick in picked))
        picked = (*picked, *pool.take(count, exclude=already))
    return picked[:count]


def _ranked_texts(
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    evidence: Evidence | None,
    count: int,
    used: Iterable[str] = (),
) -> tuple[str, ...]:
    return tuple(pick.text for pick in _ranked_picks(frame, sources, evidence, count, used))


def _pool_exhausted(evidence: Evidence | None, used: Iterable[str]) -> bool:
    """True when the answer already spent every sentence the pool held.

    Only THEN may a surface go empty: an empty card beats showing the reader the
    same sentence twice. With no ranked pool at all (search failed, or sources
    too thin to quote) the snippet digests are the only content there is, and
    hiding them would leave the report blank.
    """
    return bool(tuple(used)) and evidence is not None and bool(len(evidence))


# ────────────────────────────────────────────────────────────
# Answer highlights
# ────────────────────────────────────────────────────────────

def answer_highlights(
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    *,
    evidence: Evidence | None = None,
    used: Iterable[str] = (),
) -> tuple[str, ...]:
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return _comparison_highlights(frame)
    picks = _ranked_texts(frame, sources, evidence, 4, used)
    if picks:
        return picks
    # Nothing is left that the answer has not already shown. The snippet digest
    # would put those same sentences back on the page in a second widget.
    if _pool_exhausted(evidence, used):
        return ()
    return _snippet_highlights(sources) or (
        f"Research {len(sources)} source(s) for information about {frame.subject}.",
        "Check the source links for detailed explanations and evidence.",
        "Compare information across multiple sources for the most reliable understanding.",
    )


def _snippet_highlights(sources: Sequence[ResearchSource]) -> tuple[str, ...]:
    """Last resort when a source offers no sentence worth ranking."""
    highlights, seen = [], set()
    for source in sources:
        snippet = (source.snippet or "").strip()
        if not snippet or len(snippet) < 20:
            continue
        for sentence in _SENTENCE_BOUNDARY.split(snippet):
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


def _comparison_clause(item: str) -> str:
    return f"Choose {item} when its strengths match your highest-priority constraint"


# ────────────────────────────────────────────────────────────
# Sections
# ────────────────────────────────────────────────────────────

def build_sections(
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    *,
    evidence: Evidence | None = None,
    used: Iterable[str] = (),
) -> tuple[dict[str, Any], ...]:
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return _comparison_sections(frame, sources)
    if not sources:
        return _offline_sections(frame)
    return _evidence_sections(frame, sources, evidence, used)


def _evidence_sections(
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    evidence: Evidence | None,
    used: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    """Findings ranked against the question, then the per-source references."""
    sections: list[dict[str, Any]] = []

    picks = _ranked_picks(frame, sources, evidence, 6, used)
    if picks:
        sections.append({
            "title": "Key Findings",
            "items": tuple({"text": pick.text, "source_indices": (pick.source_index,)} for pick in picks),
        })
    elif not _pool_exhausted(evidence, used):
        facts = extract_facts(sources)
        if facts:
            sections.append({
                "title": "Key Findings",
                "items": tuple({"text": fact, "source_indices": ()} for fact in facts[:5]),
            })

    ref_items = tuple(
        {
            "text": f"{clean_source_title(source.title, source.url)}: {text}",
            "source_indices": (index,),
        }
        for index, source in enumerate(sources[:4], start=1)
        if (text := clean_snippet(source.snippet or "")) and len(text) > 10
    )
    if ref_items:
        sections.append({"title": "From The Sources", "items": ref_items})

    return tuple(sections) if sections else _offline_sections(frame)


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

def key_points(
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    *,
    evidence: Evidence | None = None,
    used: Iterable[str] = (),
) -> tuple[str, ...]:
    if frame.kind == "comparison" and len(frame.items) >= 2:
        return _comparison_highlights(frame)
    picks = _ranked_texts(frame, sources, evidence, 5, used)
    if picks:
        return picks
    # Same contract as the highlights: never re-show what the answer spent.
    if _pool_exhausted(evidence, used):
        return ()
    return _snippet_key_points(frame, sources) or (
        f"Research {len(sources)} source(s) for information about {frame.subject}.",
        "Check the source links for detailed explanations.",
    )


def _snippet_key_points(frame: QueryFrame, sources: Sequence[ResearchSource]) -> tuple[str, ...]:
    points, seen = [], set()
    for source in sources[:5]:
        snippet = (source.snippet or "").strip()
        if not snippet or len(snippet) < 20:
            continue
        for sentence in _SENTENCE_BOUNDARY.split(snippet):
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
    return tuple(points)


# ────────────────────────────────────────────────────────────
# Detailed explanation
# ────────────────────────────────────────────────────────────

def detailed_explanation(frame: QueryFrame, sources: Sequence[ResearchSource], depth: str) -> str:
    """Per-source detail: what THIS source says, in its own sentences."""
    if not sources:
        return _offline_detailed_explanation(frame, depth)
    sections = []
    for i, source in enumerate(sources[:6], start=1):
        title = source.title or f"Source {i}"
        body = _source_summary(source)
        if body:
            sections.append(f"{i}. {title}: {body}")
        else:
            sections.append(f"{i}. {title} (see source for details)")
    if depth == "deep":
        sections.append("Verification: compare agreement across sources, check whether the quoted sentences support each claim.")
    else:
        sections.append("For a quick understanding, focus on the main points above, then check the source links for deeper details.")
    return "\n".join(sections)


def _source_summary(source: ResearchSource) -> str:
    """The first readable sentences a source offers, page text over blurb."""
    text = page_text_of(source)
    if text:
        cleaned = [clean_snippet(part) for part in _SENTENCE_BOUNDARY.split(text)]
        usable = [part for part in cleaned if part and len(part) > 15][:2]
        if usable:
            return " ".join(usable)[:420]
    snippet = (source.snippet or "").strip()
    return clean_snippet(snippet) if len(snippet) > 15 else ""


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


def overview(topic: str, sources: Sequence[ResearchSource]) -> str:
    display = display_subject(topic)
    if not sources:
        return (f"Here is a structured overview of {display}. "
                "Online search returned no usable results, so I used NovaControl's local explanation workflow.")
    domains = list({d for s in sources if (d := _domain(s.url))})[:3]
    domain_text = ", ".join(domains) if domains else f"{len(sources)} sources"
    return (f"Research on {display} from {domain_text}: "
            f"{len(sources)} source(s) analyzed with key findings organized below.")
