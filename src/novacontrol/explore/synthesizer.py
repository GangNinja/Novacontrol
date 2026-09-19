"""Answer synthesis: source evidence → a written answer.

Owns: pulling facts out of source text, ranking them against the user's
question, and composing the answer — plus the connected-model path that writes
prose from the same evidence.

There is no per-topic rule table here any more. Answers used to be assembled by
keyword buckets ("cause", "step", "definition" word lists) filled from search
blurbs, which is how "why do cats purr" produced a re-listing of pages that
mentioned purring. The local path now ranks the sentences the sources actually
contain (see `evidence.py`) and composes the answer from the top ones; the
question's wording is never echoed back as a framing line.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
import re

from novacontrol.explore.evidence import (  # noqa: F401  (Sentence re-exported for callers)
    Evidence,
    Sentence,
    build_evidence,
    join_sentences,
    page_text_of,
)
from novacontrol.explore.planner import usable_provider
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
        # Meta-discourse: sentences about the PAGE instead of about the subject.
        # "In this article they will discuss the concept of Abetment under the
        # Indian Penal Code" was served as a research fact, and it tells the
        # reader nothing about abetment. Form patterns, not topic words: the
        # subject can be anything and this phrasing still means "no content".
        r"\b(this|the following|that) (article|guide|post|page|section|overview|piece|blog)\b",
        r"\bwe (will |'ll |can )?(discuss|cover|explain|look at|explore|talk about)\b",
        r"\byou (will|'ll|can) (learn|discover|find out)\b",
        r"\b(read on|keep reading|let's (dive|get started)|without further ado)\b",
        r"\b(table of contents|updated[: ]|published[: ]|min read)\b",
        r"\bin (this|the following) (video|episode|chapter|course)\b",
        # Standing disclaimers: true about the SITE, not about the subject.
        r"\binformation is (current|accurate|up[- ]?to[- ]?date)\b",
        r"\bnot a substitute for (professional|veterinary|medical|legal) advice\b",
        r"\bconsult (your|a|an) (doctor|vet|veterinarian|physician|lawyer|professional)\b",
        r"\b(we|i) (may )?(earn|receive) (a )?(commission|compensation)\b",
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

# A snippet that opens with a rhetorical teaser ("Ever wonder why cats purr?",
# "Have you ever wondered…?") has its answer in the NEXT sentence; the teaser
# itself is marketing copy, and keeping it is how a research answer ended up
# opening with the user's own question echoed back. This is a FORM pattern
# about search snippets, not a fact about any topic.
_TEASER_SENTENCE = re.compile(
    r"^\s*(?:ever|have you ever|did you ever|do you)\s+wonder(?:ed)?[^.!?]{0,120}[.!?]\s*"
    r"|^\s*wondering[^.!?]{0,120}\?\s*"
    r"|^\s*curious about[^.!?]{0,120}\?\s*",
    re.IGNORECASE,
)

# Tolerant on purpose: scraped links arrive mangled as "https:/." (one slash,
# trailing dot), which the strict `https?://\S+` form misses entirely.
_URL_IN_TEXT = re.compile(r"https?:\S*", re.IGNORECASE)


def clean_snippet(text: str) -> str:
    """Clean a raw search snippet into a readable fact.

    Returns "" when the snippet is nothing but site chrome, so callers skip it
    rather than presenting a consent banner or footer as a research fact.
    """
    cleaned = " ".join(text.split()).strip()
    # Raw links survive snippet scraping as "https:/." style fragments; they
    # are never part of a fact and they leaked into a live answer.
    cleaned = _URL_IN_TEXT.sub("", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    # Date prefixes
    cleaned = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s*\d{4}\s*[·•\-–]\s*", "", cleaned)
    cleaned = re.sub(r"^\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\s*[·•\-–]\s*", "", cleaned)
    cleaned = re.sub(r"^\d{4}\s*[·•\-–]\s*", "", cleaned)
    # Truncation markers. A snippet cut off mid-sentence KEEPS its ellipsis: a
    # search snippet that stops at "...purring is due to stress or" must read as
    # truncated, not as a finished sentence ending in a dangling "or."
    cleaned = re.sub(r"\.{3,}", "\u2026", cleaned)
    cleaned = re.sub(r"\u2026+", "\u2026", cleaned)
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
    # Drop a leading rhetorical teaser, but only when real content follows it.
    teased = _TEASER_SENTENCE.sub("", cleaned, count=1).strip()
    if len(teased) >= 30:
        cleaned = teased
    # Removing a teaser can orphan its closing quote/bracket ('Ever wonder …?'
    # leaves '" Cats can purr …'): quotes, not words, are what get cleaned.
    cleaned = cleaned.lstrip("\"'\u2018\u2019\u201c\u201d)]}:;,.-\u2013\u2014\u2026 ").strip()
    if not cleaned:
        return ""
    if cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned[-1] not in ".!?\u2026":
        cleaned += "."
    return cleaned.strip()


# ────────────────────────────────────────────────────────────
# Fact extraction
# ────────────────────────────────────────────────────────────

# Video pages are not prose sources: their "snippet" is a video description
# (title, channel blurb, promo line), which is how a Dodo clip's description
# became a research "fact". Videos keep their own section in the report.
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com")


def _is_video_page(url: str) -> bool:
    host = url.lower()
    return any(video_host in host for video_host in _VIDEO_HOSTS)


# Facts are SENTENCES. A fact used to be a whole search blurb, so a report's
# "findings" could be three truncated meta descriptions; the page's own
# sentences are what a finding actually looks like.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def extract_facts(sources: Sequence[ResearchSource]) -> list[str]:
    """Clean, ranked facts: one sentence each, from the best text per source.

    Reads the page we fetched when there is one and the search blurb otherwise,
    splits it into sentences, drops chrome, and ranks by how much each sentence
    reads like a statement (length, plus the informative-marker boost that has
    always been here). Video pages stay excluded: a video description is not
    evidence.
    """
    candidates: list[tuple[float, str]] = []
    seen: set[str] = set()

    for source in sources:
        if _is_video_page(source.url):
            continue
        text = page_text_of(source)
        if not text or len(text) < 15:
            continue
        for raw in _SENTENCE_BOUNDARY.split(text):
            cleaned = clean_snippet(raw)
            if not cleaned or len(cleaned) < 15:
                continue
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
# Composition: the local answer is built from the sources' sentences
# ────────────────────────────────────────────────────────────

def select_answer_sentences(evidence: Evidence) -> tuple[tuple[Sentence, ...], tuple[Sentence, ...]]:
    """The (lead, supporting) sentences the local answer spends.

    Returned separately so the report can record what was already shown: the
    highlights and the sections below the answer then draw from what is left,
    instead of repeating the same sentences in a second widget.
    """
    lead_count = 3 if evidence.has_page_text else 2
    lead = evidence.take(lead_count, complete_only=True) or evidence.take(1)
    spent = {sentence.text for sentence in lead}
    # The supporting bullets hold a floor: a sentence the ranking judged far
    # weaker than the leader is boilerplate, and padding the answer with it is
    # how an answer stops being one.
    support = (
        evidence.take(5, exclude=spent, complete_only=True, min_score_ratio=0.35)
        or evidence.take(5, exclude=spent, min_score_ratio=0.35)
    )
    return lead, support


def compose_answer(
    evidence: Evidence, sources: Sequence[ResearchSource], *, question: str = ""
) -> str:
    """Write the local answer out of ranked source sentences.

    It opens with the sentences that best answer the question, as a paragraph —
    because that is what an answer looks like — and then lists the remaining
    strong sentences as supporting detail. Every sentence is one a source
    wrote; nothing is invented.

    The question's own wording never comes back as a framing line ("here is
    what the sources say about X"): that line answers nothing and reads as a
    template. Attribution goes at the END, where it belongs, naming the domains
    the sentences came from.

    With page text the answer can carry a real paragraph; with only search
    blurbs (which end in "…") the lead stays the best COMPLETE sentence and the
    rest become bullets, so a digest never masquerades as flowing prose.
    """
    lead, support = select_answer_sentences(evidence)

    parts: list[str] = []
    if lead:
        parts.append(join_sentences(lead))
    if support:
        parts += ["", "**Supporting detail:**"]
        parts.extend(f"• {sentence.text}" for sentence in support)
    domains = [d for source in sources[:4] if (d := _domain(source.url))]
    if domains:
        parts += ["", f"Sources: {', '.join(dict.fromkeys(domains))}."]
    return "\n".join(parts).strip()


# ────────────────────────────────────────────────────────────
# Main synthesis entry point
# ────────────────────────────────────────────────────────────

def local_answer(
    evidence: Evidence,
    sources: Sequence[ResearchSource],
    *,
    topic: str,
    question: str = "",
) -> tuple[str, tuple[str, ...]]:
    """The local answer plus the sentences it spent on it.

    When nothing can be quoted at all, the answer names the pages it found
    instead of emitting a "here is what the sources say about X" shell with no
    content under it.
    """
    if evidence:
        lead, support = select_answer_sentences(evidence)
        return (
            compose_answer(evidence, sources, question=question),
            tuple(sentence.text for sentence in (*lead, *support)),
        )
    titles = [source.title for source in sources[:3] if source.title]
    if titles:
        return (
            f"Nothing in the sources could be quoted for {topic}, so here are the pages "
            "that were found: " + "; ".join(titles) + ". Open a source to read it directly.",
            (),
        )
    return "", ()


def synthesize_answer(
    topic: str,
    frame: QueryFrame,
    sources: Sequence[ResearchSource],
    *,
    question: str = "",
) -> str:
    """Build the local answer from the strongest evidence in the sources.

    `question` is the user's own phrasing (or the topic a vague follow-up was
    resolved to); relevance is judged against it, not against whatever the
    query parser reduced the topic to. Sentences come from the pages we read
    when reading worked, and from the search blurbs when it did not.
    """
    if not sources:
        return ""
    ask = " ".join(str(question or topic).split()) or topic
    evidence = build_evidence(ask, frame, sources)
    return local_answer(evidence, sources, topic=topic, question=ask)[0]


def _domain(url: str) -> str:
    """Extract domain from URL. Kept local to avoid circular import with source_helpers."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ────────────────────────────────────────────────────────────
# LLM synthesis
# ────────────────────────────────────────────────────────────

# How much of one page the model gets. Enough for the argument it makes, not so
# much that six sources swamp a small local model's context.
_MAX_PAGE_CHARS_IN_PROMPT = 2400


def _research_block(sources: Sequence[ResearchSource], *, per_source_chars: int = _MAX_PAGE_CHARS_IN_PROMPT) -> str:
    """The research handed to the model: page prose where we read it, else the blurb.

    The page is the whole point of reading it — a model given only meta
    descriptions can do no better than paraphrase them, which is exactly the
    keyword-shaped answer this path exists to replace.
    """
    lines: list[str] = []
    for source in sources[:5]:
        text = " ".join(page_text_of(source).split())
        if len(text) < 40:
            continue
        if len(text) > per_source_chars:
            text = text[:per_source_chars].rsplit(" ", 1)[0] + "…"
        lines.append(f"- [{source.title}]({source.url}): {text}")
    return "\n".join(lines)

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
    *,
    question: str = "",
    focus: str = "",
) -> str | None:
    """Attempt LLM-powered answer synthesis. Returns None if unavailable.

    QUESTION-FIRST by construction: the model is handed the user's own words
    and asked to ANSWER them from the research, not to describe the topic. It
    is forbidden from listing source titles or pasting snippets, because a
    model that echoes the evidence reproduces exactly the keyword-shaped
    non-answer this path exists to replace. When the research does not answer
    the question, the model is told to say so rather than pad — an honest gap
    is worth more than another blurb.
    """
    if provider is None or not usable_provider(provider):
        return None
    complete = getattr(provider, "complete", None)
    if complete is None:
        return None

    facts = extract_facts(sources)
    if not facts:
        return None

    source_list = _research_block(sources)
    facts_text = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))

    # Type-specific instructions
    type_instruction = _LLM_TYPE_INSTRUCTIONS.get(frame.kind, "")
    system_prompt = _LLM_SYSTEM_PROMPTS.get(
        frame.kind,
        "You synthesize research into clear, accurate answers. Be concise and factual.",
    )

    ask = " ".join(str(question or topic).split())
    prompt = f"Question: {ask}\n"
    if ask.lower() != topic.lower():
        prompt += f"Subject under discussion: {topic}\n"
    if focus:
        prompt += f"What the question is actually asking for: {focus}\n"
    prompt += (
        "\nAnswer that question from the research below. Write the answer a "
        "knowledgeable person would give out loud: directly, in your own words, "
        "in flowing prose.\n"
        "Do not list source titles, do not paste snippet text, do not restate the "
        "question, and do not pad with background the question did not ask for. "
        "Use only the research; if it does not answer part of the question, say "
        "plainly what is missing instead of guessing.\n"
    )
    if type_instruction:
        prompt += f"Shape of this answer: {type_instruction}\n"
    # Conversation context
    if prior_topics:
        prompt += f"\nEarlier exploration in this conversation: {', '.join(prior_topics)}\n"
        prompt += (
            "The user may be following up on one of those; if the question refers to "
            "a prior topic, connect it to the relevant one.\n"
        )
    prompt += f"\nResearch:\n{source_list}\n\nThe sentences that best match the question:\n{facts_text}\n"
    if frame.context:
        prompt += f"\nContext: {frame.context}"
    if frame.items:
        prompt += f"\nItems to compare: {', '.join(frame.items)}"

    try:
        messages: list[Mapping[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        result = str(await complete(messages)).strip()
    except Exception as exc:
        logger.debug("LLM synthesis failed: %s", exc)
        return None
    # A one-liner is a refusal or a stray token, not an answer; anything longer
    # is used verbatim. (The old gate also rejected answers whose first line
    # mentioned the word "topic" — a heuristic that discarded real answers.)
    return result if len(result) >= 40 else None
