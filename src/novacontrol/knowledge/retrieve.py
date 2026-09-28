"""Reranking and the context budget (Phase 11.4).

Retrieval answers "what matched?". This module answers the two questions that
come after it, and they are different questions:

* **Which of those matches deserves the space?** A score from the index is a
  statement about the TEXT; a useful ranking is a statement about the text plus
  the source it came from — a project document beats a vendored copy of the same
  words, code beats a changelog when the question is where something lives, and
  something indexed five minutes ago beats something from last quarter. Recency
  and priority MULTIPLY the score (they never replace it), and every hit keeps
  the reasons it was ranked where it was.
* **What fits?** A budget is a promise not to dump a project into a model. The
  budget is spent best-first, with a per-source cap so one verbose file cannot
  take every slot, and everything left out is COUNTED — ``truncated`` and
  ``omitted`` are how a caller can tell "nothing else matched" from "there was
  more and it did not fit".

Deduplication happens here rather than in the index because it is about the
result set: two chunks of one paragraph (a cut boundary) or a file copied into
two directories are one answer, and the second copy is not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from novacontrol.knowledge.index import ScoredChunk, terms
from novacontrol.knowledge.models import (
    KnowledgeChunk,
    KnowledgeContext,
    KnowledgeHit,
    KnowledgeSource,
    SourceType,
    estimate_tokens,
)

#: How much each kind of source is trusted when scores are close. Code and
#: documentation are what a project question is usually about; a scan-quality
#: PDF is the least reliable text in an index.
SOURCE_PRIORITY: dict[SourceType, float] = {
    SourceType.CODE: 1.10,
    SourceType.DOCUMENTATION: 1.15,
    SourceType.MARKDOWN: 1.05,
    SourceType.TEXT: 1.00,
    SourceType.NOTE: 1.00,
    SourceType.PDF: 0.90,
}

#: A source is "fresh" for the first weeks and decays by half every half-life.
#: The floor keeps an old but perfect match from being buried by a recent bad
#: one: recency is a tie-breaker with teeth, not the ranking itself.
RECENCY_HALF_LIFE_DAYS = 30.0
RECENCY_FLOOR = 0.6
RECENCY_WEIGHT = 0.25

#: The same project as the active one is what a project question is about.
ACTIVE_PROJECT_BOOST = 1.20

#: Two chunks this alike are one answer (token-set Jaccard).
DUPLICATE_THRESHOLD = 0.75

#: How many chunks one source may contribute to a context, so a single large
#: file cannot fill the window.
DEFAULT_MAX_PER_SOURCE = 3

#: What the block costs before a single excerpt: the "Relevant local knowledge
#: for: …" line, the "(N excerpts)" line and the blank line between them.
HEADER_TOKENS = 20

#: What one excerpt costs BEYOND its text: the `--- [n] path (kind, lines a-b)`
#: label and its blank line. A budget that ignored the labels would let a "2000
#: token" context arrive at the model as a 2200 token one, and the label is not
#: optional — it is the citation.
LABEL_TOKENS = 14

#: What the cut marker costs, so a shortened excerpt pays for saying so.
MARKER_TOKENS = 6

#: Appended to text that did not fit, so the reader can see the cut.
TRUNCATION_MARKER = "… [truncated]"


@dataclass(frozen=True, slots=True)
class RankedHit:
    """A hit after priority and recency, before the budget."""

    hit: KnowledgeHit
    duplicate_of: str = ""


def rank(
    candidates: tuple[ScoredChunk, ...],
    *,
    index_sources: dict[str, KnowledgeSource] | None = None,
    project: str = "",
    now: datetime | None = None,
) -> tuple[RankedHit, ...]:
    """Fuse index scores with source priority and recency, and drop duplicates.

    Order matters twice here, and getting it backwards cost the active-project
    preference (see §32): the candidates are weighted and SORTED first, and only
    then deduplicated. Deduplicating first meant that of two identical
    paragraphs — the same text vendored into another project — whichever the
    index happened to list first survived, so the copy in the project you are
    actually standing in could be the one dropped. Now the ranked-best copy is
    the one kept, and the loser is reported as the duplicate it is.
    """
    moment = now or datetime.now(UTC)
    weighted: list[KnowledgeHit] = []
    for candidate in candidates:
        source = (index_sources or {}).get(candidate.chunk.source_id)
        priority, recency = _signals(source, project, moment)
        # The index's reasons (matched terms, bm25, embedding) travel with the
        # hit: reranking adds to the evidence, it does not replace it.
        weighted.append(_as_hit(candidate, source, priority, recency, candidate.reasons))
    weighted.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
    ranked: list[RankedHit] = []
    seen: list[tuple[KnowledgeChunk, frozenset[str]]] = []
    for hit in weighted:
        candidate_terms = frozenset(terms(hit.chunk.text))
        duplicate = _duplicate_of(candidate_terms, seen)
        if duplicate is not None:
            ranked.append(RankedHit(hit=hit, duplicate_of=duplicate))
            continue
        seen.append((hit.chunk, candidate_terms))
        ranked.append(RankedHit(hit=hit))
    return tuple(ranked)


def _as_hit(
    candidate: ScoredChunk,
    source: KnowledgeSource | None,
    priority: float,
    recency: float,
    reasons: tuple[str, ...],
) -> KnowledgeHit:
    weighted = candidate.score * priority * (1 - RECENCY_WEIGHT + RECENCY_WEIGHT * recency)
    if priority != 1.0:
        reasons = (*reasons, f"priority x{priority:.2f} ({source.source_type.value if source else 'unknown'})")
    if recency < 1.0:
        reasons = (*reasons, f"recency x{recency:.2f}")
    return KnowledgeHit(
        chunk=candidate.chunk,
        score=weighted,
        lexical_score=candidate.lexical,
        embedding_score=candidate.embedding,
        priority=priority,
        recency=recency,
        reasons=reasons,
        source=source,
    )


def _signals(
    source: KnowledgeSource | None, project: str, now: datetime
) -> tuple[float, float]:
    """This source's priority and recency multipliers, both measured from facts."""
    if source is None:
        return 1.0, 1.0
    priority = SOURCE_PRIORITY.get(source.source_type, 1.0)
    if project and source.project == project:
        priority *= ACTIVE_PROJECT_BOOST
    age_days = max(0.0, (now - source.indexed_at).total_seconds() / 86400.0)
    recency = max(RECENCY_FLOOR, 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))
    return priority, recency


def _duplicate_of(
    candidate_terms: frozenset[str], seen: list[tuple[KnowledgeChunk, frozenset[str]]]
) -> str | None:
    if not candidate_terms:
        return None
    for chunk, known in seen:
        union = candidate_terms | known
        if not union:
            continue
        overlap = len(candidate_terms & known) / len(union)
        if overlap >= DUPLICATE_THRESHOLD:
            return chunk.chunk_id
    return None


def assemble(
    query: str,
    ranked: tuple[RankedHit, ...],
    *,
    budget_tokens: int,
    max_per_source: int = DEFAULT_MAX_PER_SOURCE,
    embedding_backend: str = "",
) -> KnowledgeContext:
    """Spend the budget best-first and report exactly what was left out.

    The first hit is included even when it alone exceeds the budget — its text is
    cut to fit and the context says so — because returning nothing for a query
    that matched is a worse answer than a shortened one.

    A hit costs its own tokens PLUS its label (see :data:`LABEL_TOKENS`), and the
    reported ``tokens`` is the size of the block that was actually rendered, not
    the sum of the pieces: a budget is a promise about what the model receives,
    so it has to be measured on what is sent.
    """
    budget = max(1, int(budget_tokens))
    kept: list[KnowledgeHit] = []
    # The budget is spent on what is SENT: the header and every label are real
    # tokens in the request, so they are charged before the first excerpt.
    used = HEADER_TOKENS
    per_source: dict[str, int] = {}
    omitted = 0
    duplicates = 0
    truncated = False
    for entry in ranked:
        if entry.duplicate_of:
            duplicates += 1
            continue
        source_id = entry.hit.chunk.source_id
        if per_source.get(source_id, 0) >= max(1, max_per_source):
            omitted += 1
            continue
        remaining = budget - used
        if remaining <= 0:
            omitted += 1
            truncated = True
            continue
        cost = entry.hit.chunk.tokens + LABEL_TOKENS
        if cost > remaining:
            if kept:
                omitted += 1
                truncated = True
                continue
            cut = _cut_to_budget(
                entry.hit.chunk, max(1, remaining - LABEL_TOKENS - MARKER_TOKENS)
            )
            kept.append(replace(entry.hit, chunk=cut))
            used += cut.tokens + LABEL_TOKENS
            truncated = True
            per_source[source_id] = per_source.get(source_id, 0) + 1
            continue
        kept.append(entry.hit)
        used += cost
        per_source[source_id] = per_source.get(source_id, 0) + 1
    text = render_context(query, tuple(kept))
    spent = estimate_tokens(text)
    return KnowledgeContext(
        query=query,
        text=text,
        hits=tuple(kept),
        tokens=spent,
        budget=budget,
        considered=len(ranked),
        omitted=omitted + duplicates,
        truncated=truncated or omitted > 0 or spent > budget,
        embedding_backend=embedding_backend,
    )


def _cut_to_budget(chunk: KnowledgeChunk, tokens: int) -> KnowledgeChunk:
    """Cut a chunk's text to about ``tokens``, and re-count what is left.

    The count is recomputed rather than carried over, because the budget is
    spent from ``chunk.tokens``: leaving the original length on a shortened
    chunk would charge the budget for text that was never sent. The marker is
    not free — the caller is told the chunk was cut, and that costs a few
    tokens — so the result can exceed the budget by the marker's own size, which
    is the honest direction for a rounding error.
    """
    cut = cut_text(chunk.text, tokens)
    return replace(chunk, text=cut, tokens=estimate_tokens(cut))


def cut_text(text: str, tokens: int, *, marker: str = TRUNCATION_MARKER) -> str:
    """Cut plain text to about ``tokens``, on a line boundary, and say it was cut.

    Used for text that is not a chunk — the project header of a context, for
    instance — so a header that grows with a project's recent errors cannot push
    a budgeted context past its budget unnoticed.
    """
    if estimate_tokens(text) <= tokens:
        return text
    characters = max(80, tokens * 4)
    head = text[:characters]
    newline = head.rfind("\n")
    if newline > characters // 2:
        head = head[:newline]
    return f"{head.rstrip()}\n{marker}"


def render_context(query: str, hits: tuple[KnowledgeHit, ...]) -> str:
    """The blocks a model is given: labelled, cited, and nothing else.

    Every block names its source, its kind and its line range, so an answer can
    point at where it came from — and so a reader can tell a code hit from a
    README hit at a glance.
    """
    if not hits:
        return ""
    lines = [
        f"Relevant local knowledge for: {query}",
        f"({len(hits)} excerpt{'' if len(hits) == 1 else 's'})",
        "",
    ]
    for hit in hits:
        chunk = hit.chunk
        source = hit.source
        kind = source.source_type.value if source is not None else "local"
        # The path comes from the HIT (which knows its source), not from the
        # chunk (which knows only which source it belongs to).
        location = hit.path or chunk.source_id
        lines.append(f"--- [{len(lines) - 2}] {location} ({kind}, lines {chunk.start_line}-{chunk.end_line})")
        if chunk.heading:
            lines.append(f"    under: {chunk.heading}")
        lines.append(chunk.text)
        lines.append("")
    return "\n".join(lines).strip()


def total_tokens(hits: tuple[KnowledgeHit, ...]) -> int:
    """The token cost of a set of hits, by the same estimate the budget uses."""
    return sum(hit.chunk.tokens for hit in hits)


__all__ = [
    "ACTIVE_PROJECT_BOOST",
    "DEFAULT_MAX_PER_SOURCE",
    "DUPLICATE_THRESHOLD",
    "RECENCY_HALF_LIFE_DAYS",
    "HEADER_TOKENS",
    "LABEL_TOKENS",
    "MARKER_TOKENS",
    "SOURCE_PRIORITY",
    "TRUNCATION_MARKER",
    "RankedHit",
    "assemble",
    "cut_text",
    "rank",
    "render_context",
    "total_tokens",
]
