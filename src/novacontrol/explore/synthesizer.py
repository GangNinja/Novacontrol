"""Answer synthesis: source facts → structured text answer.

Owns: extracting facts from snippets, categorizing them,
ranking by relevance, building type-specific answers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
import re

from novacontrol.explore.query import QueryFrame
from novacontrol.explore.models import ResearchSource

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Site-chrome detection
# ────────────────────────────────────────────────────────────
# Search snippets routinely glue site chrome onto real content: consent
# banners ("by using this site, you agree to the Terms of Use"), Wikipedia
# footers ("this page was last edited on …"), trademark lines, newsletter and
# sign-in frames. Synthesis used to treat those as facts, so a research answer
# could open with a Terms-of-Use sentence instead of an explanation. Chrome is
# detected SENTENCE BY SENTENCE and stripped, so a snippet that mixes chrome
# with real content keeps the content.

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_CHROME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^by (using|continuing to use|browsing|accessing) (this|our) (site|website|service)",
        r"\bterms of (use|service)\b[^.]*\bprivacy policy\b",
        r"\bprivacy policy\b[^.]*\bterms of (use|service)\b",
        r"\bis a registered trademark of\b",
        r"\bthis page was last edited\b",
        r"\blast (updated|edited|modified|reviewed) on\b",
        r"\b(we|this (site|website)|our (site|website)) use[s]? cookies\b",
        r"\b(accept|allow) (all )?cookies\b",
        r"\bcookie (preferences|settings|consent)\b",
        r"\ball rights reserved\b",
        r"(^|\s)(©|\(c\))\s*\d{2,4}\b",
        r"\benable javascript\b",
        r"\bjavascript is (disabled|required)\b",
        r"\bsubscribe (now|today)\b",
        r"\b(sign in|log in) to (your|continue|access)\b",
        r"\bskip to (main )?content\b",
        r"\bfollow us on (facebook|twitter|x|instagram|linkedin|youtube)\b",
        r"\bshare (this|on) (article|page|post)\b",
        r"\bread (more|next|the full (article|story))\s*$",
        r"^\s*(menu|search|home|sign up|log in|share|advertisement)\s*$",
    )
)


def _is_chrome_sentence(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _CHROME_PATTERNS)


def strip_boilerplate(text: str) -> str:
    """Drop site-chrome sentences, keeping the real content around them."""
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    return " ".join(s for s in sentences if not _is_chrome_sentence(s)).strip()


def is_boilerplate(text: str) -> bool:
    """True when nothing usable remains after the chrome sentences are gone."""
    return len(strip_boilerplate(text)) < 15


# ────────────────────────────────────────────────────────────
# Snippet cleaning
# ────────────────────────────────────────────────────────────

def clean_snippet(text: str) -> str:
    """Clean a raw search snippet into a readable fact.

    Returns "" when the snippet is nothing but site chrome, so callers skip it
    rather than presenting a consent banner or footer as a research fact.
    """
    cleaned = " ".join(text.split()).strip()
    # Date prefixes
    cleaned = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4}\s*[·•\-–]\s*", "", cleaned)
    cleaned = re.sub(r"^\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\s*[·•\-–]\s*", "", cleaned)
    cleaned = re.sub(r"^\d{4}\s*[·•\-–]\s*", "", cleaned)
    # Truncation markers
    cleaned = re.sub(r"\.{3,}", ".", cleaned)
    cleaned = re.sub(r"\u2026+", ".", cleaned)
    cleaned = re.sub(r"\s*\.\s*$", ".", cleaned)
    # Filler prefixes
    for pattern in (
        r"^(learn|discover|find out|explore|read|see|check|visit|go to|click)\s+(how|what|why|when|where|about|more)\s+",
        r"^(a guide to|an introduction to|everything you need to know about|the complete guide to)\s+",
        r"^(best|top|leading|popular|recommended)\s+",
    ):
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    # Trailing navigation
    cleaned = re.sub(r"\s*(read more|learn more|click here|see more)\s*\.?\s*$", "", cleaned, flags=re.IGNORECASE)
    # HTML artifacts
    cleaned = re.sub(r"[›»>]+", " ", cleaned)
    cleaned = cleaned.replace("&amp;", "&")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Site chrome is never a fact — strip it and bail out if nothing is left.
    cleaned = strip_boilerplate(cleaned)
    if not cleaned:
        return ""
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned.strip()


# ────────────────────────────────────────────────────────────
# Fact categorization
# ────────────────────────────────────────────────────────────
# Buckets are *advisory*: they decide which section a fact ideally lands in
# and are used with word boundaries so generic words ("is a", "are") never
# swallow facts that actually answer a why/how question. Keyword groups with
# rarer, stronger signals are checked first; weak definition words last.

CAUSE_KW = ("because", "due to", "caused by", "results from", "reason",
            "why", "effect", "impact", "leads to", "trigger")
STEP_KW = ("step", "first", "start by", "begin by", "next", "then", "finally",
           "create", "build", "set up", "make sure", "organize", "prepare")
NEED_KW = ("need", "require", "must have", "essential", "supply",
           "material", "equipment", "tool")
TIP_KW = ("tip", "pro tip", "best", "recommend", "try", "add", "include")
DEF_KW = ("is a", "is the", "are", "defined as", "known as", "means",
          "refers to", "involves", "consists of")

_CATEGORY_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cause", CAUSE_KW),
    ("step", STEP_KW),
    ("need", NEED_KW),
    ("tip", TIP_KW),
    ("definition", DEF_KW),
)


def categorize(fact: str) -> str:
    lower = fact.lower()
    for cat, keywords in _CATEGORY_ORDER:
        if any(re.search(rf"\b{re.escape(kw)}\b", lower) for kw in keywords):
            return cat
    return "info"


def score_fact(fact: str, query_type: str) -> float:
    lower = fact.lower()
    score = float(len(fact))
    boosts = {
        "how_to": (STEP_KW, 2.0),
        "explanation": (DEF_KW, 2.0),
        "cause": (CAUSE_KW, 2.0),
        "recommendation": (("best", "top", "recommend", "choose", "pick"), 1.5),
    }
    if query_type in boosts:
        kw, mult = boosts[query_type]
        if any(re.search(rf"\b{re.escape(k)}\b", lower) for k in kw):
            score *= mult
    if any(w in lower for w in ("how to", "step", "tip", "idea", "create", "plan")):
        score *= 1.3
    if len(fact) < 30:
        score *= 0.5
    return score


# ────────────────────────────────────────────────────────────
# Fact extraction
# ────────────────────────────────────────────────────────────

def extract_facts(sources: Sequence[ResearchSource]) -> list[str]:
    """Extract clean, ranked facts from source snippets."""
    candidates: list[tuple[float, str]] = []
    seen: set[str] = set()

    for source in sources:
        snippet = (source.snippet or "").strip()
        if not snippet or len(snippet) < 15:
            continue
        cleaned = clean_snippet(snippet)
        if not cleaned or len(cleaned) < 15:
            continue
        # Strip truncation artifacts
        last_period = cleaned.rfind(".")
        last_question = cleaned.rfind("?")
        last_excl = cleaned.rfind("!")
        end_pos = max(last_period, last_question, last_excl)
        if end_pos > 0 and end_pos < len(cleaned) - 1:
            after = cleaned[end_pos + 1:].strip()
            if len(after) < 5:
                cleaned = cleaned[:end_pos + 1]
        cleaned = re.sub(r",\s*\.\s*$", ".", cleaned).strip()
        key = cleaned.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        score = float(len(cleaned))
        lower = cleaned.lower()
        if any(w in lower for w in ("how to", "step", "tip", "idea", "create", "plan", "need")):
            score *= 1.5
        if any(w in lower for w in ("include", "contain", "example", "such as", "like")):
            score *= 1.3
        if len(cleaned) < 30:
            score *= 0.5
        candidates.append((score, cleaned))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [fact for _, fact in candidates[:6]]


# ────────────────────────────────────────────────────────────
# Template answer builders
#
# Fact buckets only *advise* placement. Each builder takes its topic facts
# from the matching bucket, and when that bucket is empty it falls back to
# the best remaining facts from any bucket — so a non-empty answer body is
# guaranteed whenever at least one usable fact exists, instead of emitting
# just an intro line (the old "why do cats purr" empty-shell failure).
# ────────────────────────────────────────────────────────────

_BUCKETS = ("cause", "step", "need", "tip", "definition", "info")

Cats = dict[str, list[tuple[float, str]]]


def _attribution(domains: list[str], n: int) -> str:
    if not domains:
        return f"Based on {n} research findings"
    if len(domains) == 1:
        return f"Based on research from {domains[0]}"
    if len(domains) == 2:
        return f"Based on research from {domains[0]} and {domains[1]}"
    return f"Based on research from {', '.join(domains[:3])}"


def _bucket_facts(cats: Cats, key: str, n: int, used: set[str]) -> list[str]:
    """Top facts from one bucket only (no cross-bucket borrowing)."""
    picked: list[str] = []
    for _score, fact in cats[key]:
        if fact in used:
            continue
        used.add(fact)
        picked.append(fact)
        if len(picked) >= n:
            break
    return picked


def _any_facts(ranked: list[str], n: int, used: set[str]) -> list[str]:
    """Best remaining facts from any bucket, in global relevance order."""
    picked: list[str] = []
    for fact in ranked:
        if fact in used:
            continue
        used.add(fact)
        picked.append(fact)
        if len(picked) >= n:
            break
    return picked


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"• {i}" for i in items]


def _numbered(items: Sequence[str]) -> list[str]:
    return [f"{i}. {item.rstrip('.')}." for i, item in enumerate(items, 1)]


def _build_how_to(topic: str, cats: Cats, ranked: list[str], attr: str) -> str:
    used: set[str] = set()
    steps = _bucket_facts(cats, "step", 5, used)
    needs = _bucket_facts(cats, "need", 3, used)
    tips = _bucket_facts(cats, "tip", 3, used)
    parts = [f"{attr}, here is how to {topic}:", ""]
    if steps:
        parts.append("**Steps:**")
        parts.extend(_numbered(steps))
        parts.append("")
    if needs:
        parts.append("**What you'll need:**")
        parts.extend(_bullets(needs))
        parts.append("")
    if tips:
        parts.append("**Tips:**")
        parts.extend(_bullets(tips))
        parts.append("")
    if not steps:
        points = _any_facts(ranked, 4, used)
        if points:
            parts.append("**Key points:**")
            parts.extend(_bullets(points))
            parts.append("")
    return "\n".join(parts).strip()


def _build_explanation(topic: str, cats: Cats, ranked: list[str], attr: str) -> str:
    used: set[str] = set()
    defs = _bucket_facts(cats, "definition", 3, used)
    info = _bucket_facts(cats, "info", 4, used)
    tips = _bucket_facts(cats, "tip", 2, used)
    parts = [f"{attr}, here is what you need to know about {topic}:", ""]
    if defs:
        parts.append("**What it is:**")
        parts.extend(_bullets(defs))
        parts.append("")
    if info:
        parts.append("**Key points:**")
        parts.extend(_bullets(info))
        parts.append("")
    if tips:
        parts.append("**Notable details:**")
        parts.extend(_bullets(tips))
        parts.append("")
    if not defs and not info:
        points = _any_facts(ranked, 5, used)
        if points:
            parts.append("**Key points:**")
            parts.extend(_bullets(points))
            parts.append("")
    return "\n".join(parts).strip()


def _build_cause(topic: str, cats: Cats, ranked: list[str], attr: str) -> str:
    used: set[str] = set()
    causes = _bucket_facts(cats, "cause", 4, used)
    context = _bucket_facts(cats, "info", 2, used) + _bucket_facts(cats, "definition", 2, used)
    parts = [f"{attr}, here is why {topic}:", ""]
    if causes:
        parts.extend(_bullets(causes))
        parts.append("")
    if context:
        parts.append("**Additional context:**")
        parts.extend(_bullets(context))
        parts.append("")
    if not causes and not context:
        points = _any_facts(ranked, 5, used)
        if points:
            parts.append("**Key points:**")
            parts.extend(_bullets(points))
            parts.append("")
    return "\n".join(parts).strip()


def _build_recommendation(
    topic: str, cats: Cats, ranked: list[str], attr: str, context: str | None = None
) -> str:
    used: set[str] = set()
    ctx = f" for {context}" if context else ""
    info = _bucket_facts(cats, "info", 5, used)
    tips = _bucket_facts(cats, "tip", 3, used)
    parts = [f"{attr}, here are the key considerations for {topic}{ctx}:", ""]
    if info:
        parts.extend(_numbered(info))
        parts.append("")
    if tips:
        parts.append("**Recommendations:**")
        parts.extend(_bullets(tips))
        parts.append("")
    if not info and not tips:
        points = _any_facts(ranked, 5, used)
        if points:
            parts.extend(_numbered(points))
            parts.append("")
    return "\n".join(parts).strip()


def _build_comparison(
    topic: str,
    cats: Cats,
    ranked: list[str],
    attr: str,
    context: str | None = None,
    items: tuple[str, ...] = (),
) -> str:
    used: set[str] = set()
    items_str = ", ".join(items[:3]) if items else topic
    facts = _bucket_facts(cats, "info", 5, used)
    if not facts:
        facts = _bucket_facts(cats, "definition", 3, used)
    if not facts:
        facts = _any_facts(ranked, 5, used)
    parts = [f"{attr}, here is a comparison of {items_str}:"]
    if context:
        parts += ["", f"For {context}:"]
    parts.append("")
    if facts:
        parts.extend(_bullets(facts))
        parts.append("")
    return "\n".join(parts).strip()


def _build_ideas(topic: str, cats: Cats, ranked: list[str], attr: str) -> str:
    used: set[str] = set()
    tips = _bucket_facts(cats, "tip", 5, used)
    info = _bucket_facts(cats, "info", 3, used)
    parts = [f"{attr}, here are ideas for {topic}:", ""]
    if tips:
        parts.append("**Ideas:**")
        parts.extend(_numbered(tips))
        parts.append("")
    if info:
        parts.append("**More inspiration:**")
        parts.extend(_bullets(info))
        parts.append("")
    if not tips and not info:
        points = _any_facts(ranked, 5, used)
        if points:
            parts.append("**Ideas:**")
            parts.extend(_numbered(points))
            parts.append("")
    return "\n".join(parts).strip()


_BUILDERS = {
    "how_to": _build_how_to,
    "explanation": _build_explanation,
    "cause": _build_cause,
    "recommendation": _build_recommendation,
    "comparison": _build_comparison,
    "ideas": _build_ideas,
}


# ────────────────────────────────────────────────────────────
# Main synthesis entry point
# ────────────────────────────────────────────────────────────

def synthesize_answer(topic: str, frame: QueryFrame, sources: Sequence[ResearchSource]) -> str:
    """Build a template answer from source facts.

    Facts are ranked globally and bucketed only to choose section placement;
    every builder falls back to the best remaining facts when its preferred
    bucket is empty, so the answer never degenerates to a bare intro line as
    long as at least one usable fact exists.
    """
    facts = extract_facts(sources)
    if not facts:
        titles = [s.title for s in sources[:3] if s.title]
        if titles:
            return (
                f"Based on {len(sources)} source(s), here is what the research found about {topic}: "
                + "; ".join(titles) + ". Check the source links below for the full details."
            )
        return ""

    source_domains = list({d for s in sources if (d := _domain(s.url))})[:3]
    query_type = frame.kind if frame.kind in _BUILDERS else "explanation"

    cats: Cats = {key: [] for key in _BUCKETS}
    for fact in facts:
        cats[categorize(fact)].append((score_fact(fact, query_type), fact))
    for bucket in cats.values():
        bucket.sort(key=lambda item: item[0], reverse=True)
    ranked = [
        fact for _score, fact in sorted(
            (item for bucket in cats.values() for item in bucket),
            key=lambda item: item[0], reverse=True,
        )
    ]

    attr = _attribution(source_domains, len(facts))
    builder = _BUILDERS.get(query_type, _build_explanation)
    if query_type == "recommendation":
        return _build_recommendation(topic, cats, ranked, attr, frame.context)
    if query_type == "comparison":
        return _build_comparison(topic, cats, ranked, attr, frame.context, frame.items)
    return builder(topic, cats, ranked, attr)


def _domain(url: str) -> str:
    """Extract domain from URL. Kept local to avoid circular import with source_helpers."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ────────────────────────────────────────────────────────────
# LLM synthesis
# ────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────
# LLM prompt instructions by query type
# ────────────────────────────────────────────────────────────

_LLM_TYPE_INSTRUCTIONS: dict[str, str] = {
    "how_to": (
        "Write a step-by-step guide with numbered steps. Start with prerequisites, "
        "then give clear actions in order. End with tips or common pitfalls."
    ),
    "explanation": (
        "Write a clear explanation that builds from basic to detailed. "
        "Define the concept first, then expand with key details and implications."
    ),
    "cause": (
        "Explain the causal mechanism. Identify the primary cause, contributing factors, "
        "and any downstream effects. Use cause-and-effect language (because, leads to, results in)."
    ),
    "recommendation": (
        "Provide a clear recommendation. State the top choice and why, then mention "
        "alternatives and when each makes sense. Base reasoning on the source data."
    ),
    "comparison": (
        "Write a structured comparison. For each option, state its strengths and weaknesses. "
        "End with a decision framework: which to choose depending on the user's priorities."
    ),
    "ideas": (
        "List concrete, actionable ideas. For each idea, state what it is, why it works, "
        "and how to start. Prioritize ideas with low setup cost and high impact."
    ),
    "mechanism": (
        "Explain how something works mechanically. Break it into components or stages, "
        "describe what happens at each stage, and note the inputs and outputs."
    ),
}

_LLM_SYSTEM_PROMPTS: dict[str, str] = {
    "how_to": "You write clear, actionable step-by-step guides. Be direct and practical.",
    "explanation": "You write clear explanations that build from simple to detailed. Be accurate.",
    "cause": "You explain causes and effects with precision. Use causal reasoning.",
    "recommendation": "You give well-reasoned recommendations backed by evidence. Be decisive.",
    "comparison": "You write fair, structured comparisons. Present both sides honestly.",
    "ideas": "You generate creative, practical ideas. Be specific and actionable.",
    "mechanism": "You explain how things work mechanically. Be precise about components and stages.",
}


async def try_llm_synthesis(
    provider: object | None,
    topic: str,
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    prior_topics: tuple[str, ...] = (),
) -> str | None:
    """Attempt LLM-powered answer synthesis. Returns None if unavailable."""
    if provider is None:
        return None
    name = str(getattr(provider, "name", "")).lower()
    if not name or "echo" in name:
        return None
    complete = getattr(provider, "complete", None)
    if complete is None:
        return None

    facts = extract_facts(sources)
    if not facts:
        return None

    source_list = "\n".join(
        f"- [{s.title}]({s.url}): {text}"
        for s in sources[:5]
        if (text := clean_snippet(s.snippet or "")) and len(text) > 10
    )
    facts_text = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))

    # Type-specific instructions
    type_instruction = _LLM_TYPE_INSTRUCTIONS.get(frame.kind, "")
    system_prompt = _LLM_SYSTEM_PROMPTS.get(
        frame.kind,
        "You synthesize research into clear, accurate answers. Be concise and factual.",
    )

    prompt = f"Synthesize a clear answer about '{topic}' from the research below.\n\n"
    if type_instruction:
        prompt += f"How to structure this answer: {type_instruction}\n\n"
    # Conversation context
    if prior_topics:
        prompt += f"Previous exploration in this conversation: {', '.join(prior_topics)}\n"
        prompt += f"The user may be following up on one of these topics. If the current question "
        prompt += f"refers to a prior topic (e.g. 'tell me more about that'), connect it to "
        prompt += f"the relevant prior topic in your answer.\n\n"
    prompt += f"Source snippets:\n{source_list}\n\n"
    prompt += f"Key facts:\n{facts_text}\n\n"
    prompt += f"Question kind: {frame.kind}"
    if frame.context:
        prompt += f"\nContext: {frame.context}"
    if frame.items:
        prompt += f"\nItems to compare: {', '.join(frame.items)}"

    try:
        messages: list[Mapping[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        result = await complete(messages)
        result = str(result).strip()
        if "research assistant" in result.lower()[:80]:
            return None
        if len(result) > 50 and "topic" not in result.lower().split("\n")[0][:20]:
            return result
    except Exception as exc:
        logger.debug("LLM synthesis failed: %s", exc)
    return None
