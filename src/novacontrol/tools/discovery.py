"""Tool discovery: the few tools that could carry THIS request, with scores.

Handing every tool definition to a model is the failure this module exists to
prevent. It is not only that a long list costs context: a list of twenty
tools invites a model to pick the one whose NAME reads best, and the names
(``system_monitor``, ``explore_service``) say almost nothing about what the user
meant. Retrieval replaces the list with an answer to a smaller question:

    given *"which programs are consuming most of my memory?"*, which three of
    these could possibly be what is being asked for?

The ranking is deliberately the lightweight machinery the NLU layer already
uses, not a new one:

* **lexical**: the query's content words, inverse-document-frequency weighted
  across the tool corpus, scored as the fraction of the query's meaning a tool
  covers. Rare words decide, which is why "ram" outweighs "programs" — and why
  a tool that matches nothing but a common word cannot win.
* **semantic** (optional, on by default): cosine similarity over hashed
  word/bigram/character-ngram features — the same deterministic, offline vector
  space the intent layer uses. It closes the gap when the words differ
  ("chewing up my ram" vs "memory use") and needs no model; a caller that HAS an
  embedding model passes it as ``embedder`` and this layer uses it instead.

Both components are reported on every match, and the combined score is a fixed
weighted sum rather than a black box, so a surprising ranking can be explained
after the fact by reading two numbers. A result below ``floor`` is not returned
at all: "no tool fits this" is a real answer, and it is the one that keeps the
planner from reaching for an unrelated tool just to have one.

Nothing here executes anything, and nothing here can widen a permission.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.normalize import normalize
from novacontrol.intelligence.semantic import Embedder, HashingEmbedder, content_words
from novacontrol.tools.catalog import ToolCatalog
from novacontrol.tools.metadata import ToolCategory, ToolMetadata

#: Below this combined score, a tool is not a candidate at all — measured, not
#: guessed. On the shipped corpus the requests the system can genuinely serve
#: score far above it ("which programs are consuming most of my memory?" 0.73,
#: "send a text to alex" 0.77, "take a screenshot and tell me what button is
#: broken" 0.45), while requests nothing can serve land just under it ("book me
#: a flight to mars" 0.23 because a browser can search but not book, "write a
#: poem about the sea" 0.19 from a tag that happened to overlap). At an earlier
#: floor of 0.18 both of those were returned as candidates, which is how a
#: planner ends up reaching for a tool that has nothing to do with the request.
DEFAULT_FLOOR = 0.25

#: How many tools a caller gets when it does not say.
DEFAULT_LIMIT = 5

#: How the two components combine. The lexical side leads because an exact word
#: match is evidence a person used the tool's own vocabulary; the semantic side
#: is what rescues a paraphrase.
_LEXICAL_WEIGHT = 0.65
_SEMANTIC_WEIGHT = 0.35

#: A registered tool is a slightly better answer than an unregistered one with
#: the same description — it can actually be run. Small on purpose: it breaks
#: ties, it does not decide.
_REGISTERED_BONUS = 0.02

#: Words are matched after a cheap pluralfold ("programs" -> "program"), because
#: requests are plural and declared tags usually are not.
_MIN_FOLD_LENGTH = 4


@dataclass(frozen=True, slots=True)
class ToolMatch:
    """One candidate tool, with the evidence for its score."""

    tool: ToolMetadata
    score: float
    lexical: float = 0.0
    semantic: float = 0.0
    matched_terms: tuple[str, ...] = ()
    reason: str = ""

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def available(self) -> bool:
        return self.tool.registered

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool.name,
            "description": self.tool.description,
            "category": self.tool.category.value,
            "risk": self.tool.risk.value,
            "requires_approval": self.tool.requires_approval(),
            "available": self.available,
            "score": round(self.score, 4),
            "lexical": round(self.lexical, 4),
            "semantic": round(self.semantic, 4),
            "matched_terms": list(self.matched_terms),
            "reason": self.reason,
        }


class ToolRetriever:
    """Ranks catalogued tools for a piece of text. Deterministic and cached."""

    def __init__(
        self,
        catalog: ToolCatalog | Sequence[ToolMetadata],
        *,
        embedder: Embedder | None = None,
        floor: float = DEFAULT_FLOOR,
        limit: int = DEFAULT_LIMIT,
        use_semantic: bool = True,
    ) -> None:
        self.catalog = catalog if isinstance(catalog, ToolCatalog) else ToolCatalog(catalog)
        self.floor = max(0.0, floor)
        self.limit = max(1, limit)
        self.use_semantic = use_semantic
        self.embedder: Embedder = embedder or HashingEmbedder()
        self._tools: tuple[ToolMetadata, ...] = self.catalog.tools()
        self._terms: tuple[tuple[str, ...], ...] = tuple(
            _terms(tool.searchable_text()) for tool in self._tools
        )
        self._sparse: tuple[dict[int, float], ...] = tuple(
            _sparse(tool.searchable_text(), self.embedder) for tool in self._tools
        )
        self._norms: tuple[float, ...] = tuple(_norm(vector) for vector in self._sparse)
        self._idf: dict[str, float] = _document_frequencies(self._terms)
        self._cache: dict[tuple[Any, ...], tuple[ToolMatch, ...]] = {}
        self.hits = 0
        self.misses = 0

    # -- queries ----------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        category: ToolCategory | str | None = None,
        tags: Sequence[str] = (),
        max_risk: Any | None = None,
        require_registered: bool = False,
    ) -> tuple[ToolMatch, ...]:
        """The tools that could carry ``query``, best first, above the floor.

        Filters are applied BEFORE ranking, so a limit of three means three
        candidates from the requested family rather than whatever survived a
        global cut. The result is cached per (query, filters), because the same
        request asked twice should be a dictionary lookup.
        """
        wanted_limit = max(1, limit if limit is not None else self.limit)
        key = (
            normalize(query),
            wanted_limit,
            str(category or ""),
            tuple(sorted(str(tag).lower() for tag in tags)),
            str(getattr(max_risk, "value", max_risk) or ""),
            require_registered,
        )
        cached = self._cache.get(key)
        if cached is None:
            self.misses += 1
            cached = self._search_uncached(
                query,
                limit=wanted_limit,
                category=category,
                tags=tags,
                max_risk=max_risk,
                require_registered=require_registered,
            )
            self._cache[key] = cached
        else:
            self.hits += 1
        return cached

    def best(self, query: str, **kwargs: Any) -> ToolMatch | None:
        ranked = self.search(query, limit=1, **kwargs)
        return ranked[0] if ranked else None

    def prompt_tools(self, query: str, *, limit: int | None = None) -> tuple[str, ...]:
        """Just the names — what a planner prompt should be given."""
        return tuple(match.name for match in self.search(query, limit=limit))

    # -- ranking ----------------------------------------------------------------

    def _search_uncached(
        self,
        query: str,
        *,
        limit: int,
        category: ToolCategory | str | None,
        tags: Sequence[str],
        max_risk: Any | None,
        require_registered: bool,
    ) -> tuple[ToolMatch, ...]:
        normalized = normalize(query)
        query_terms = _terms(normalized)
        if not normalized.strip():
            return ()
        # A request can be made entirely of function words — "what can you do?"
        # has no content words at all, so both the lexical and the semantic layer
        # see an empty query and every tool scores zero. That request has a real
        # answer (it is the capabilities tool's own example), so when there is
        # nothing to weigh, the phrases a tool declares are compared directly.
        phrase_only = not query_terms
        query_sparse = (
            {}
            if phrase_only or not self.use_semantic
            else _sparse(normalized, self.embedder)
        )
        query_norm = _norm(query_sparse)
        wanted_category = (
            category.value if isinstance(category, ToolCategory) else str(category or "")
        )
        wanted_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
        risk_ceiling = _risk_rank(max_risk)

        matches: list[ToolMatch] = []
        for index, tool in enumerate(self._tools):
            if wanted_category and tool.category.value != wanted_category:
                continue
            if wanted_tags and not wanted_tags.issubset({tag.lower() for tag in tool.tags}):
                continue
            rank = _risk_rank(tool.risk)
            if risk_ceiling is not None and rank is not None and rank > risk_ceiling:
                continue
            if require_registered and not tool.registered:
                continue
            matched: tuple[str, ...] = ()
            if phrase_only:
                lexical, matched = _phrase_match(normalized, tool), ()
                semantic = 0.0
            else:
                lexical, matched = _coverage(query_terms, self._terms[index], self._idf)
                semantic = 0.0
                if query_norm > 0.0 and self._norms[index] > 0.0:
                    semantic = max(
                        0.0,
                        _dot(query_sparse, self._sparse[index])
                        / (query_norm * self._norms[index]),
                    )
            score = _LEXICAL_WEIGHT * lexical + _SEMANTIC_WEIGHT * semantic
            if tool.registered:
                score += _REGISTERED_BONUS
            if score < self.floor:
                continue
            matches.append(
                ToolMatch(
                    tool=tool,
                    score=min(1.0, score),
                    lexical=lexical,
                    semantic=semantic,
                    matched_terms=matched,
                    reason=_reason(tool, lexical, semantic, matched),
                )
            )
        matches.sort(key=lambda match: (-match.score, match.tool.name))
        return tuple(matches[:limit])

    # -- reporting ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools_indexed": len(self._tools),
            "registered_tools": len(self.catalog.registered()),
            "embedder": self.embedder.name,
            "semantic_enabled": self.use_semantic,
            "floor": self.floor,
            "limit": self.limit,
            "cached_queries": len(self._cache),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
        }


def _terms(text: str) -> tuple[str, ...]:
    """Content words of ``text``, plural-folded, order preserved."""
    return tuple(_fold(word) for word in content_words(normalize(text)))


def _fold(word: str) -> str:
    if len(word) > _MIN_FOLD_LENGTH and word.endswith("s"):
        return word[:-1]
    return word


def _document_frequencies(documents: Sequence[Sequence[str]]) -> dict[str, float]:
    """IDF per term across the tool corpus; a rare term is what identifies."""
    counts: dict[str, int] = {}
    for document in documents:
        for term in set(document):
            counts[term] = counts.get(term, 0) + 1
    total = max(1, len(documents))
    return {term: math.log((total + 1) / (count + 1)) + 1.0 for term, count in counts.items()}


def _coverage(
    query_terms: Sequence[str],
    document_terms: Sequence[str],
    idf: Mapping[str, float],
) -> tuple[float, tuple[str, ...]]:
    """How much of the query's meaning the document covers, as a fraction.

    Terms are weighted by the SQUARE of their rarity, which is what makes the
    identifying word win over the incidental one. Measured on the shipped
    corpus: with plain IDF, *"what is chewing up my ram"* ranked the browser
    above the system monitor, because "up" happens to appear in more documents
    than "ram" does and nothing said which of the two the request was ABOUT.
    Squaring the rarity means a term almost no tool uses ("ram") outweighs one
    several tools share ("up"), which is the same reasoning that makes
    inverse-document-frequency work at all — just pushed further where the
    corpus is small and vocabulary is narrow.
    """
    if not query_terms:
        return 0.0, ()
    seen = set(document_terms)
    matched = tuple(term for term in dict.fromkeys(query_terms) if term in seen)
    if not matched:
        return 0.0, ()
    default = max(idf.values(), default=1.0)
    total = sum(_weight(term, idf, default) for term in dict.fromkeys(query_terms))
    if total <= 0.0:
        return 0.0, matched
    return min(1.0, sum(_weight(term, idf, default) for term in matched) / total), matched


def _weight(term: str, idf: Mapping[str, float], default: float) -> float:
    rarity = idf.get(term, default)
    return rarity * rarity


def _sparse(text: str, embedder: Embedder) -> dict[int, float]:
    """Text as sparse dimensions, whichever kind of backend supplied it."""
    producer = getattr(embedder, "sparse", None)
    if callable(producer):
        return {int(key): float(value) for key, value in producer(text).items() if value}
    vector = embedder.embed([text])[0]
    return {index: float(value) for index, value in enumerate(vector) if value}


def _norm(vector: Mapping[int, float]) -> float:
    return math.sqrt(sum(value * value for value in vector.values()))


def _dot(left: Mapping[int, float], right: Mapping[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(dimension, 0.0) for dimension, value in left.items())


def _phrase_match(query: str, tool: ToolMetadata) -> float:
    """How close a content-word-less request is to a phrase the tool declares.

    Character-trigram overlap against the examples (and the name), which is the
    same kind of evidence the NLU's lexical layer uses for spelling and
    morphology — enough to recognise a tool's own example phrasing, and not
    enough to match an unrelated tool by accident: all three of the floor, the
    weighted score and the caller's limit still apply.
    """
    candidates = [tool.name.replace("_", " "), *tool.examples, tool.description]
    grams = _trigrams(query)
    if not grams:
        return 0.0
    best = 0.0
    for candidate in candidates:
        other = _trigrams(normalize(candidate))
        if not other:
            continue
        best = max(best, len(grams & other) / len(grams | other))
    return best


def _trigrams(text: str) -> set[str]:
    padded = f"  {text.strip()}  "
    return {padded[start:start + 3] for start in range(max(0, len(padded) - 2))}


def _risk_rank(level: Any) -> int | None:
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if level is None or level == "":
        return None
    return order.get(str(getattr(level, "value", level)).strip().lower())


def _reason(tool: ToolMetadata, lexical: float, semantic: float, matched: Sequence[str]) -> str:
    """One sentence a person can check the ranking against."""
    if lexical > 0.0:
        terms = ", ".join(matched[:5]) or "no terms"
        return f"shares {terms} with {tool.name} (lexical {lexical:.2f})"
    return f"reads like {tool.name}'s wording (semantic {semantic:.2f})"
