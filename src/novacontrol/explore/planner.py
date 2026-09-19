"""Question-first research planning: the model decides what to search.

Owns: turning the user's question (plus the conversation so far) into the
search queries that would actually ANSWER it, and a one-line statement of what
is being asked.

Why this module exists. Searching a topic's keywords returns pages that mention
those keywords, so the "answer" a report can build from them explains something
ADJACENT to the question ("why do cats purr" came back as pages titled *Why Do
Cats Purr?* whose own blurbs were re-listed as the answer). The fix is not
another keyword rule with another word list — it is letting the connected model
read the question and pick the queries itself, which is a capability no
hand-written rule table has.

`query.build_search_queries` is untouched and remains the deterministic
fallback for when no model is connected (scratch brain, no Ollama, no cloud
key). When a model IS connected but answers with something that is not a plan,
this returns None and the caller falls back — a malformed plan is worse than
the rules, and silently searching the model's prose would be worse still.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import re

logger = logging.getLogger(__name__)


_PLAN_SYSTEM_PROMPT = (
    "You plan web searches for a research assistant. You receive a user's "
    "question, and possibly the topics discussed earlier in the conversation.\n"
    "Reply with ONLY a JSON object and nothing else — no prose, no markdown "
    "fence:\n"
    '{"focus": "<one sentence: what the user is actually asking for>", '
    '"queries": ["<search query>", ...]}\n'
    "Rules for the queries:\n"
    "- 2 to 4 queries, each one a phrase a search engine answers well.\n"
    "- Keep the question's specific nouns and qualifiers; drop only conversational "
    "filler.\n"
    "- Cover the question from the angles that actually exist for it (the direct "
    "answer, the authoritative or official source, the current state of things, the "
    "main counter-argument or caveat).\n"
    "- Never answer the question, never invent facts, never add a query about "
    "something the user did not ask about.\n"
    "- If the question is a vague follow-up, use the earlier conversation topics "
    "to make the queries concrete."
)


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """The model's reading of the question: what is asked, and what to search."""

    focus: str
    queries: tuple[str, ...]
    planned_by: str = ""

    def __bool__(self) -> bool:
        return bool(self.queries)


def usable_provider(provider: object | None) -> bool:
    """True when `provider` is a real completion model (not None / not Echo).

    The single eligibility rule shared with `synthesizer.try_llm_synthesis`:
    an unnamed or echo provider is the offline baseline, and asking it to plan
    would just hand back the prompt.
    """
    if provider is None:
        return False
    name = str(getattr(provider, "name", "")).lower()
    if not name or "echo" in name:
        return False
    return callable(getattr(provider, "complete", None))


async def plan_research(
    provider: object | None,
    question: str,
    *,
    prior_topics: Sequence[str] = (),
    max_queries: int = 4,
) -> ResearchPlan | None:
    """Ask the model to turn `question` into search queries.

    Returns None (never raises) when no model is connected, when the call
    fails, or when the reply is not a usable plan — every one of those means
    "use the deterministic query builder instead".
    """
    question = " ".join(str(question or "").split())
    if not question or not usable_provider(provider):
        return None

    prompt = f"Question: {question}"
    if prior_topics:
        recent = [t for t in prior_topics if str(t).strip()][-3:]
        if recent:
            prompt += "\nEarlier in this conversation: " + "; ".join(str(t) for t in recent)
    messages: list[Mapping[str, str]] = [
        {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    complete = getattr(provider, "complete", None)
    if complete is None:  # pragma: no cover - usable_provider already checked
        return None
    try:
        raw = await complete(messages, max_tokens=400, temperature=0.2)
    except Exception as exc:  # a planning failure must never break the research
        logger.debug("Research planning failed: %s", exc)
        return None
    return parse_plan(str(raw), provider, max_queries=max_queries)


def parse_plan(raw: str, provider: object | None = None, *, max_queries: int = 4) -> ResearchPlan | None:
    """Parse a planning reply into a ResearchPlan, or None when unusable.

    Only JSON is accepted. Local models wrap JSON in prose or a fence often
    enough that the first balanced `{...}` block is extracted and parsed; a
    reply with no JSON object at all is rejected rather than mined for lines,
    because treating model prose as search queries searches nonsense.
    """
    payload = _first_json_object(raw)
    if not isinstance(payload, dict):
        return None
    queries = payload.get("queries")
    if isinstance(queries, str):
        queries = [queries]
    if not isinstance(queries, (list, tuple)):
        return None
    cleaned = _clean_queries([q for q in queries if isinstance(q, (str, int, float))], max_queries)
    if not cleaned:
        return None
    focus = " ".join(str(payload.get("focus", "")).split())[:400]
    return ResearchPlan(focus=focus, queries=cleaned, planned_by=str(getattr(provider, "name", "") or "model"))


def _first_json_object(raw: str) -> object | None:
    """The first decodable JSON object in `raw`, fences and prose stripped."""
    text = raw.strip()
    if not text:
        return None
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            decoded: object = json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
        return decoded
    return None


def _clean_queries(queries: Sequence[object], max_queries: int) -> tuple[str, ...]:
    """Normalize queries: strip decoration, drop empties/dupes/runaways."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in queries:
        text = " ".join(str(item).split())
        text = re.sub(r"^[-*\u2022\d.)\s]+", "", text)          # "- q", "1. q", "1) q"
        text = text.strip("\"' \t")
        if not text or len(text) < 2 or len(text) > 160:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= max(1, max_queries):
            break
    return tuple(cleaned)
