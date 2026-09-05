"""Query parsing: raw text → structured QueryFrame.

Owns: parsing user questions into kind/subject/items/context.
No other module needs to parse queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Sequence


@dataclass(frozen=True)
class QueryFrame:
    original: str
    kind: str
    subject: str
    items: tuple[str, ...] = ()
    context: str = ""


def parse_query(query: str) -> QueryFrame:
    """Parse a user query into a structured frame."""
    original = " ".join(query.strip().split())
    text = re.sub(r"^(please\s+)?(can|could|would)\s+you\s+", "", original, flags=re.IGNORECASE)
    text = re.sub(r"^(please\s+)?", "", text, flags=re.IGNORECASE).strip()

    comparison = _comparison_frame(original, text)
    if comparison is not None:
        return comparison

    kind = "explanation"
    if re.match(r"^(give\s+me\s+some\s+ideas|give\s+me\s+ideas|ideas\s+for|brainstorm|suggest\s+ideas)\b", text, re.IGNORECASE):
        kind = "ideas"
    elif re.match(r"^(recommend|suggest|find|pick|choose|which|what\s+is\s+the\s+best)\b", text, re.IGNORECASE):
        kind = "recommendation"
    elif re.match(r"^why\b", text, re.IGNORECASE):
        kind = "cause"
    elif re.match(r"^(how\s+to|how\s+(do|can|should)\s+i)\b", text, re.IGNORECASE):
        kind = "how_to"
    elif re.match(r"^(plan|create|build|make|set\s+up|start|organize|find|get|prepare|design|develop|implement|write|code)\b", text, re.IGNORECASE):
        kind = "how_to"
    elif re.match(r"^(explain\s+)?how\b", text, re.IGNORECASE):
        kind = "mechanism"

    return QueryFrame(original=original, kind=kind, subject=_clean_topic(text))


def _comparison_frame(original: str, text: str) -> QueryFrame | None:
    match = re.match(r"^(compare|compare\s+and\s+contrast)\s+(.+)$", text, re.IGNORECASE)
    remainder = match.group(2).strip() if match else None
    if remainder is None and re.search(r"\b(vs\.?|versus)\b", text, re.IGNORECASE):
        remainder = text.strip()
    if remainder is None:
        return None
    context_match = re.search(
        r"\s+(for|as|when choosing|to choose|if you want|for choosing)\s+(.+)$",
        remainder, re.IGNORECASE,
    )
    if context_match:
        core = remainder[:context_match.start()].strip(" ,.")
        context = context_match.group(2).strip(" ?.")
    else:
        core, context = remainder.strip(" ?."), ""
    items = _split_comparison_items(core)
    return (
        QueryFrame(original=original, kind="comparison", subject=_join_items(items), items=items, context=context)
        if len(items) >= 2
        else None
    )


def _split_comparison_items(text: str) -> tuple[str, ...]:
    normalized = re.sub(r"\b(vs\.?|versus)\b", ",", text, flags=re.IGNORECASE)
    normalized = re.sub(r",?\s+and\s+", ",", normalized, flags=re.IGNORECASE)
    parts = [p.strip(" ,.") for p in normalized.split(",")]
    cleaned, seen = [], set()
    for part in parts:
        if not part or part.lower() in seen:
            continue
        seen.add(part.lower())
        cleaned.append(part[:1].upper() + part[1:] if not part.isupper() or len(part) > 8 else part)
    return tuple(cleaned)


def _join_items(items: Sequence[str]) -> str:
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


# ────────────────────────────────────────────────────────────
# Topic cleaning
# ────────────────────────────────────────────────────────────

def _clean_topic(topic: str) -> str:
    text = " ".join(topic.strip().split())
    text = re.sub(r"^(please\s+)?(can|could|would)\s+you\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(please\s+)?", "", text, flags=re.IGNORECASE)
    how_question = text.lower().startswith(("explain how ", "how "))
    personal_how = re.match(r"^how\s+(do|can|should)\s+i\s+(.+)$", text, flags=re.IGNORECASE)
    if personal_how:
        text = personal_how.group(2)
    else:
        work_question = re.match(
            r"^(?:explain\s+)?how\s+(?:exactly\s+)?(?:do|does|did|can|should)\s+(.+?)\s+(?:actually\s+)?work\b.*$",
            text, flags=re.IGNORECASE,
        )
        if work_question:
            text = work_question.group(1)
        else:
            for pattern, replacement in (
                (r"^(please\s+)?explain\s+how\s+", ""),
                (r"^(please\s+)?explain\s+", ""),
                (r"^(please\s+)?research\s+", ""),
                (r"^(please\s+)?recommend\s+", ""),
                (r"^(please\s+)?suggest\s+", ""),
                (r"^(please\s+)?give\s+me\s+some\s+ideas\s+for\s+", ""),
                (r"^(please\s+)?give\s+me\s+ideas\s+for\s+", ""),
                (r"^(please\s+)?ideas\s+for\s+", ""),
                (r"^(please\s+)?brainstorm\s+", ""),
                (r"^(please\s+)?suggest\s+ideas\s+for\s+", ""),
                (r"^(please\s+)?find\s+", ""),
                (r"^(please\s+)?pick\s+", ""),
                (r"^(please\s+)?choose\s+", ""),
                (r"^(please\s+)?teach\s+me\s+(about\s+)?", ""),
                (r"^(please\s+)?tell\s+me\s+about\s+", ""),
                (r"^what\s+(is|are)\s+(the\s+)?", ""),
                (r"^why\s+(do|does|did|is|are|can|should)\s+", ""),
                (r"^pros\s+and\s+cons\s+of\s+", ""),
                (r"^how\s+to\s+", ""),
                (r"^how\s+(do|does|did|can|should)\s+", ""),
            ):
                text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    if how_question:
        text = re.sub(r"\s+actually\s+work\??$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+work\??$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,?\s*(in simple terms|in plain english|for beginners|step by step|with examples)\??$", "", text, flags=re.IGNORECASE)
    text = text.strip(" ?.")
    for prefix in ("set up ", "create ", "build ", "make ", "start "):
        text = re.sub(r"^" + re.escape(prefix), "", text, flags=re.IGNORECASE)
    text = _strip_leading_article(text.strip())
    return text.strip() or topic.strip()


def display_subject(topic: str) -> str:
    return _strip_leading_article(topic)


def sentence_subject(topic: str) -> str:
    text = _strip_leading_article(topic)
    return text[:1].upper() + text[1:] if text else topic


def _strip_leading_article(topic: str) -> str:
    return re.sub(r"^(a|an|the)\s+", "", topic.strip(), flags=re.IGNORECASE)


# ────────────────────────────────────────────────────────────
# Commerce / shopping context
# ────────────────────────────────────────────────────────────

# Words that describe price or quality rather than the product itself.
# They are too generic to carry relevance alone (a page about the federal
# budget mentions "budget"), so shopping queries require a product-token match.
_SHOPPING_QUALIFIERS = frozenset({
    "budget", "budget-friendly", "cheap", "cheaper", "cheapest",
    "affordable", "inexpensive", "low-cost", "best", "top", "top-rated",
    "great", "good", "price", "priced", "prices", "pricing", "cost",
    "value", "worth", "deal", "deals", "discount", "sale", "under",
    "below", "around", "about", "less", "up", "to", "for",
})

# Strong commerce words: two or more signal buying intent even without an amount.
_COMMERCE_WORDS = (
    "buy", "purchase", "cheap", "affordable", "inexpensive", "low-cost",
    "budget", "price", "priced", "prices", "cost", "deal", "deals",
    "discount", "sale", "best", "top", "recommend", "recommended",
    "pick", "choose", "worth", "value",
)

_AMOUNT_RE = re.compile(
    r"(?:[$\u20ac\u00a3\u20b9]\s?\d[\d,]*"
    r"|\d[\d,]*\s*(?:usd|dollars?|rupees?|euros?|pounds?|inr|bucks?)\b"
    r"|(?:usd|dollars?|rupees?|euros?|pounds?|inr|bucks?)\s?\d[\d,]*"
    r"|(?:under|below|less\s+than|around|about|up\s+to)\s+[$\u20ac\u00a3\u20b9]?\s?\d[\d,]*)",
    re.IGNORECASE,
)


def detect_shopping_context(topic: str) -> bool:
    """Detect buying intent: an explicit price/amount, or commerce wording.

    'best budget laptops under 500' -> True (amount)
    'recommend a laptop for video editing under 1000' -> True (amount)
    'best budget laptops' -> True (best + budget)
    'how does the federal budget work' -> False (only the word 'budget')
    """
    text = topic.strip().lower()
    if _AMOUNT_RE.search(text):
        return True
    hits = sum(1 for word in _COMMERCE_WORDS if re.search(rf"\b{word}\b", text))
    return hits >= 2


def _find_price_phrase(topic: str) -> str:
    """Return the explicit price constraint, e.g. 'under $500' or '50000 rupees'."""
    match = _AMOUNT_RE.search(topic)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(0)).strip()


# ────────────────────────────────────────────────────────────
# Search query generation
# ────────────────────────────────────────────────────────────

def extract_search_topic(topic: str) -> str:
    """Extract the core searchable topic from a user question.

    Removes question prefixes, action verbs, and filler words.
    'Plan a fun scavenger hunt in my area' -> 'scavenger hunt'
    'How does quantum computing work?' -> 'quantum computing'
    """
    text = topic.lower().strip(" ?.")
    for prefix in (
        r"^(please\s+)?(can|could|would)\s+you\s+",
        r"^(please\s+)?explain\s+(how\s+)?",
        r"^(please\s+)?give\s+me\s+(some\s+)?(ideas\s+for\s+)?",
        r"^(please\s+)?find\s+(me\s+)?",
        r"^(please\s+)?suggest\s+",
        r"^(please\s+)?",
    ):
        text = re.sub(prefix, "", text, flags=re.IGNORECASE)
    text = text.strip(" ?.")
    for starter in (
        r"^(how\s+to|how\s+(do|can|should)\s+i)\s+",
        r"^(how\s+(does|do|did|can|should)\s+)\s*",
        r"^(what\s+(is|are)\s+(the\s+)?(best|most|main|key|important)\s+)?",
        r"^(what\s+(is|are)\s+(the\s+)?)",
        r"^(why\s+(do|does|did|is|are|can|should)\s+)\s*",
        r"^(compare\s+(and\s+contrast\s+)?)\s*",
        r"^(best\s+)\s*",
        r"^(give\s+me\s+)?",
        r"^(find\s+me\s+)?",
        r"^(suggest\s+)?",
    ):
        text = re.sub(starter, "", text, flags=re.IGNORECASE)
    text = text.strip(" ?.")
    for verb in ("plan", "create", "build", "make", "set up", "start",
                 "organize", "find", "get", "choose", "pick", "learn"):
        if text.startswith(verb):
            text = text[len(verb):].strip()
            break
    text = re.sub(
        r"\s+(in|at|on|for|of|to|from|with|under|over|about)\s+"
        r"(my|the|a|an|your|this|that|some|any|1000|2024|2025).*",
        "", text,
    )
    text = re.sub(r"\s+(work|happening|going|mean|means|called)\??$", "", text)
    text = re.sub(
        r"\b(a|an|the|fun|good|best|great|new|simple|easy|quick|cool|really|very|some|ideas)\b",
        " ", text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 3:
        words = topic.split()
        content = [w for w in words if w.lower() not in {
            'a', 'an', 'the', 'in', 'my', 'your', 'area', 'for', 'with',
            'and', 'or', 'is', 'are', 'how', 'what', 'why', 'can', 'do',
            'does', 'should', 'would', 'could', 'please', 'me', 'i'}]
        text = " ".join(content[-3:]) if len(content) > 3 else " ".join(content)
    return text.strip() or topic


def build_search_queries(topic: str, max_sources: int) -> tuple[tuple[str, str], ...]:
    """Generate topic-aware search queries from cleaned topic words."""
    lower = topic.lower()
    core = extract_search_topic(topic)
    if detect_shopping_context(topic):
        # Re-anchor the price constraint: extract_search_topic can clip amounts
        # like 'under 1000', and for shopping queries the ceiling is essential.
        phrase = _find_price_phrase(topic)
        if phrase and phrase.lower() not in core:
            core = f"{core} {phrase}".strip()
    queries: list[tuple[str, str]] = [(core, "web")]
    if any(lower.startswith(w) for w in ("how to", "how do", "how can", "plan", "create", "build", "start", "set up", "make", "organize")):
        queries.append((f"{core} step by step guide", "guide"))
    elif any(lower.startswith(w) for w in ("compare", "vs", "versus")):
        queries.append((f"{core} pros cons comparison", "comparison"))
    elif any(lower.startswith(w) for w in ("what is", "what are", "explain")):
        queries.append((f"{core} explained simply", "definition"))
    elif any(lower.startswith(w) for w in ("why", "cause", "reason")):
        queries.append((f"{core} reasons explanation", "explanation"))
    elif any(lower.startswith(w) for w in ("best", "recommend", "suggest", "top")):
        queries.append((f"{core} best options review", "review"))
    else:
        queries.append((f"{core} explained simply", "explanation"))
    return tuple(queries[:max(2, min(max_sources, 4))])


def wiki_search_variations(topic: str) -> list[str]:
    """Generate Wikipedia search variations to avoid disambiguation traps."""
    variations: list[str] = []
    core = extract_search_topic(topic)
    variations.append(core.title())
    variations.append(core)
    words = core.split()
    if len(words) >= 2:
        variations.append("_".join(w.capitalize() for w in words))
    context_words = {
        "scavenger hunt": ["Scavenger hunt (game)"],
        "northern lights": ["Aurora", "Aurora borealis"],
        "aurora borealis": ["Aurora", "Northern lights"],
    }
    text = topic.lower().strip(" ?.")
    for key, extras in context_words.items():
        if key in text:
            variations.extend(extras)
    seen: set[str] = set()
    result: list[str] = []
    for v in variations:
        v_lower = v.lower().strip()
        if v_lower and v_lower not in seen:
            seen.add(v_lower)
            result.append(v)
    return result


def is_wiki_film_result(source_title: str, source_snippet: str) -> bool:
    """Check if a Wikipedia result is about a film/TV show instead of the topic."""
    combined = f"{source_title.lower()} {(source_snippet or '').lower()}"
    return any(ind in combined for ind in (
        "american comedy film", "american film", "directed by",
        "starring", "television series", "feature film",
        "release date", "box office",
    ))


def is_relevant(title: str, snippet: str, url: str, topic: str) -> bool:
    """Check if a search result is relevant to the topic."""
    combined = f"{title.lower()} {snippet.lower()}"
    url_lower = url.lower()
    blocked = ("airtel.in", "jio.com", "flipkart.com", "facebook.com", "twitter.com")
    if any(d in url_lower for d in blocked):
        return False
    if any(p in combined for p in ("dictionary definition", "define ", "meaning of ")):
        if any(d in url_lower for d in ("dictionary", "definition")):
            return False
    wiki_topic = (extract_search_topic(topic).title() or topic).lower()
    topic_words = {w for w in (_token(w) for w in wiki_topic.split()) if len(w) > 2}
    if not topic_words:
        return True
    if detect_shopping_context(topic):
        # Qualifier words ('budget', 'under', digits) must not carry relevance
        # alone: a page about the federal budget matches 'budget' but is not a
        # product result. Require one real product token.
        product_words = {
            w for w in topic_words
            if w not in _SHOPPING_QUALIFIERS and not _is_amount_token(w)
        }
        if product_words:
            return sum(1 for w in product_words if w in combined) >= 1
    # Function words ('and', 'the', 'for') are too common to carry relevance
    # alone; only fall back to them when the topic has no real content tokens.
    content_words = topic_words - _FUNCTION_WORDS
    if content_words:
        return sum(1 for w in content_words if w in combined) >= 1
    return sum(1 for w in topic_words if w in combined) >= 1


def _token(word: str) -> str:
    """Normalize a topic token: strip surrounding punctuation, keep inner hyphens."""
    return word.strip(".,;:!?'\"()[]{}$\u20ac\u00a3\u20b9&+=%#@*-_")


def _is_amount_token(word: str) -> bool:
    if word.isdigit():
        return True
    stripped = word.lstrip("$\u20ac\u00a3\u20b9")
    return bool(stripped) and stripped.isdigit()


# High-frequency function words that add no topical signal.
_FUNCTION_WORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "how", "why", "what",
    "are", "was", "were", "but", "you", "your", "this", "that", "its",
    "not", "all", "can", "has", "have", "who", "when", "where", "which",
    "should", "would", "could", "does", "did", "than", "them", "they",
})


# ────────────────────────────────────────────────────────────
# Follow-up reference resolution
# ────────────────────────────────────────────────────────────

_VAGUE_FOLLOWUP_PATTERNS: list[tuple[str, str]] = [
    (r"^tell me more (about|on)\s+(that|this|it)\s*$", "more_details"),
    (r"^(give me )?(more|additional|further)\s+(detail|info|information|explanation)\s*(about|on|for)?\s*(that|this|it)?\s*$", "more_details"),
    (r"^(go )?(deeper|further|into detail|in detail)\s*(on|about|into)?\s*(that|this|it)?\s*$", "deeper"),
    (r"^(what about|how about|and|also)\s+(that|this|it)\s*$", "more_details"),
    (r"^(can you )?(explain|elaborate|expand)\s*(on|about)?\s*(that|this|it)?\s*$", "more_details"),
    (r"^(i want|give me|show me)\s+(more|examples|options|alternatives)\s*(about|on|for|of)?\s*(that|this|it)?\s*$", "more_details"),
    (r"^(continue|keep going|go on)\s*$", "more_details"),
    (r"^(what )?(else|else can|else do|else is)\s*$", "more_details"),
    (r"^(tell me )?(about|more about|something about)\s+(that|this|it)\s*$", "more_details"),
    (r"^(compare|versus|vs|compared to)\s+(that|this|it)\s*$", "comparison"),
    (r"^(why|how|when|where)\s+(does|do|did|is|are|was|were|can|could|should)\s+(that|this|it)\s*$", "deeper"),
    (r"^(and )?(the )?(other|next|alternative|second)\s+(one|option|thing)?\s*$", "other_options"),
    (r"^(any|what)\s+(other|more|additional)\s+(questions?|topics?|ideas?)\s*$", "more_details"),
]

_VAGUE_KEYWORDS = (
    "tell me more", "more detail", "more info", "go deeper",
    "elaborate", "expand", "continue", "keep going",
    "what else", "and that", "about that", "about it",
    "about this", "the other one", "the next one",
    "give me more", "show me more",
)


def resolve_followup_topic(topic: str, last_topic: str | None = None) -> str:
    """Resolve a vague follow-up query to a concrete topic.

    If the user says 'tell me more about that' and the last topic was
    'photosynthesis', this returns 'photosynthesis deeper details'.

    If no prior topic exists, returns the original topic unchanged.
    """
    if not last_topic:
        return topic

    lower = topic.strip().lower()

    # Check regex patterns
    for pattern, intent in _VAGUE_FOLLOWUP_PATTERNS:
        if re.match(pattern, lower, re.IGNORECASE):
            return _build_resolved_topic(last_topic, intent)

    # Check keyword containment (less strict match)
    for keyword in _VAGUE_KEYWORDS:
        if keyword in lower:
            # Make sure it's mostly a vague reference, not a real topic
            # e.g. "tell me more about quantum computing" is NOT vague
            remaining = lower.replace(keyword, "").strip()
            # If what's left is just 'about that/it/this' or empty → vague
            remaining_clean = re.sub(r"^(about|on|for|of|to)?\s*(that|this|it)?\s*$", "", remaining).strip()
            if not remaining_clean or remaining_clean in ("that", "this", "it"):
                return _build_resolved_topic(last_topic, "more_details")

    return topic


def _build_resolved_topic(last_topic: str, intent: str) -> str:
    """Build a concrete search topic from the last topic and an intent."""
    clean = extract_search_topic(last_topic)
    suffixes = {
        "more_details": "detailed explanation examples",
        "deeper": "in depth advanced detailed",
        "comparison": "compared to alternatives pros cons",
        "other_options": "alternatives other options similar",
    }
    suffix = suffixes.get(intent, "detailed explanation")
    return f"{clean} {suffix}"
