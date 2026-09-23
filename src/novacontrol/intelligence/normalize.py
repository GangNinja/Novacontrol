"""Context-aware normalization and typo tolerance.

Punctuation is NOT blindly stripped: URLs, code, paths, decimals, and times
keep theirs. Only conversational noise (terminal punctuation, polite fillers,
shouty caps, padding whitespace) is removed, with guards that know what must
survive.

Typo tolerance is layered: token-level dictionary/alias correction first
(cheap, deterministic), then fuzzy matching against the live vocabulary with
context-aware thresholds.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# Fillers that carry no executable meaning when they END a request.
_POLITE_TAIL = re.compile(
    r"\s*(?:please|thanks|thank you|if you (?:would|don't mind)|for me|now|"
    r"can you|could you|would you|will you)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_LEADING_POLITE = re.compile(
    r"^\s*(?:please|can you|could you|would you|will you|i want you to|i need you to)\s+",
    re.IGNORECASE,
)

# Tokens whose punctuation is semantic and must never be "cleaned".
_URL_LIKE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_PATH_LIKE = re.compile(r"(?:[A-Za-z]:\\|~/|/home/|/Users/|\S+\.(?:exe|py|json|md|txt|csv|png|jpg|zip|dll|lnk)\b)")
_TIME_LIKE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_DECIMAL_LIKE = re.compile(r"\b\d+\.\d+\b")
_VERSION_LIKE = re.compile(r"\bv?\d+(?:\.\d+)+\b")

_WHITESPACE = re.compile(r"\s+")

# Conversational openers that carry no request. Stripped only when something
# survives: "hi" is a whole conversational turn (the scratch brain answers it),
# while "hey can you open chrome" is a command wearing a greeting.
#
# A word only belongs here if it is never the verb: "look" was in this list and
# turned "look at this screenshot" into "at this screenshot", losing a vision
# request to a clarifying question. Interjections are safe; anything that can
# start a command is not.
_LEADING_FILLER = re.compile(
    r"^\s*(?:hey|hi|hello|yo|hiya|ok|okay|so|um|uh|well|now then)\b[\s,]*",
    re.IGNORECASE,
)

# Contractions, expanded for MATCHING only (see ``expand_contractions``). The
# rule table holds some phrasings in one form and some in the other; expanding
# in place would break whichever form is missing, so this is applied as an
# additional lookup pass rather than a rewrite.
_CONTRACTIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{pattern}\b", re.IGNORECASE), replacement)
    for pattern, replacement in (
        ("can't", "cannot"), ("won't", "will not"), ("don't", "do not"),
        ("doesn't", "does not"), ("didn't", "did not"), ("isn't", "is not"),
        ("aren't", "are not"), ("couldn't", "could not"), ("shouldn't", "should not"),
        ("wouldn't", "would not"), ("hasn't", "has not"), ("haven't", "have not"),
        ("i'm", "i am"), ("i'll", "i will"), ("i've", "i have"), ("i'd", "i would"),
        ("it's", "it is"), ("that's", "that is"), ("there's", "there is"),
        ("what's", "what is"), ("where's", "where is"), ("who's", "who is"),
        ("how's", "how is"), ("let's", "let us"), ("we're", "we are"),
        ("you're", "you are"), ("they're", "they are"),
    )
)

# Brand-to-launch-name aliases. Only pairs that are the SAME program — the
# desktop runner resolves by name, so a wrong rewrite would launch the wrong
# application or nothing at all.
_APPLICATION_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{pattern}\b", re.IGNORECASE), canonical)
    for pattern, canonical in (
        ("google chrome", "chrome"),
        ("microsoft edge", "edge"),
        ("ms edge", "edge"),
        ("mozilla firefox", "firefox"),
        (r"windows explorer", "explorer"),
        ("file explorer", "explorer"),
    )
)

# Domain synonyms. ONE canonical form per group, and the groups are the ones
# where two words really are interchangeable in a command ("show my ram" and
# "show my memory" are the same request). This table is data: the exemplar
# corpus consumes it to widen paraphrase coverage, and nothing rewrites user
# text with it, so a synonym can never rename a file or an application.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("memory", "ram"),
    ("cpu", "processor"),
    ("gpu", "graphics card", "video card"),
    ("battery", "charge level", "power level"),
    ("network", "connection", "internet"),
    ("volume", "sound level", "loudness"),
    ("brightness", "screen brightness", "display brightness"),
    ("screenshot", "screen capture", "screen grab", "printscreen"),
    ("folder", "directory", "dir"),
    ("application", "app", "program"),
    ("website", "web site", "site"),
    ("delete", "remove", "erase"),
    ("launch", "open", "start", "bring up", "fire up"),
    ("close", "quit", "exit", "shut down"),
    ("find", "locate", "search for", "look for"),
)

_SYNONYM_INDEX: dict[str, tuple[str, ...]] = {
    term: group for group in SYNONYM_GROUPS for term in group
}


def contains_technical_token(text: str) -> bool:
    """True when the text holds tokens whose punctuation/spelling is semantic
    (URLs, paths, code-ish names, times, decimals, versions)."""
    return bool(
        _URL_LIKE.search(text)
        or _PATH_LIKE.search(text)
        or _TIME_LIKE.search(text)
        or _DECIMAL_LIKE.search(text)
        or _VERSION_LIKE.search(text)
    )


def normalize(text: str) -> str:
    """Canonical conversational form of user input.

    Unicode NFKC, whitespace collapse, capitalization fold, terminal
    punctuation and politeness filler removal — with technical tokens
    protected (their punctuation survives; the text around them is normalized).
    """
    if text is None:
        return ""
    # Unicode normalization + zero-width noise.
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("\u200b", "").replace("\u00ad", "")
    value = _WHITESPACE.sub(" ", value).strip()
    if not value:
        return ""

    if contains_technical_token(value):
        # Light touch: strip only trailing conversational punctuation.
        return value.rstrip(" .!?").strip()

    value = value.rstrip(" .!?;,:").strip()
    # Politeness stacks: "can you please open chrome" needs both removed, so
    # strip until the front stops changing.
    previous = ""
    while previous != value:
        previous = value
        value = _POLITE_TAIL.sub("", value).strip()
        value = _LEADING_POLITE.sub("", value).strip()
        value = _LEADING_FILLER.sub("", value).strip()
        if not value:
            value = previous  # a greeting on its own IS the request
            break
    value = value.rstrip(" .!?;,:").strip()
    for pattern, canonical in _APPLICATION_ALIASES:
        value = pattern.sub(canonical, value)
    return _WHITESPACE.sub(" ", value).lower().strip()


def expand_contractions(text: str) -> str:
    """Contraction-expanded form of ``text``, for an extra matching pass.

    Not applied by ``normalize``: the rule table holds some phrasings in one
    form and some in the other, so rewriting up front would break whichever
    form is missing. Callers try the normal form first and this one second.
    """
    value = text or ""
    for pattern, replacement in _CONTRACTIONS:
        value = pattern.sub(replacement, value)
    return _WHITESPACE.sub(" ", value).strip()


def synonym_variants(phrase: str, *, limit: int = 3) -> tuple[str, ...]:
    """Phrasings of ``phrase`` with one synonym group substituted.

    Used to widen the exemplar corpus, never to rewrite a live request: a
    user's words are matched as they are, and these variants only add
    paraphrase coverage for the lexical matcher.
    """
    normalized = normalize(phrase)
    if not normalized:
        return ()
    variants: list[str] = []
    for group in SYNONYM_GROUPS:
        for term in group:
            if not re.search(rf"\b{re.escape(term)}\b", normalized):
                continue
            for alternative in group:
                if alternative == term:
                    continue
                candidate = re.sub(rf"\b{re.escape(term)}\b", alternative, normalized)
                if candidate != normalized and candidate not in variants:
                    variants.append(candidate)
            break
        if len(variants) >= limit:
            break
    return tuple(variants[:limit])


def normalized_tokens(text: str) -> list[str]:
    """Token list of normalized input, technical tokens kept whole."""
    normalized = normalize(text)
    if not normalized:
        return []
    tokens: list[str] = []
    for match in re.finditer(r"\S+", normalized):
        token = match.group(0)
        # Split clean words from their punctuation; keep URL/path tokens whole.
        if _URL_LIKE.search(token) or _PATH_LIKE.search(token):
            tokens.append(token)
            continue
        tokens.extend(part for part in re.split(r"[^\w+#.-]+", token) if part)
    return tokens


def correct_token(token: str, vocabulary: tuple[str, ...] | frozenset[str], *, threshold: float = 0.78) -> str:
    """Correct one token against the vocabulary.

    Exact hits pass through. Otherwise the best vocabulary match above the
    threshold wins. Tokens that look technical (contain digits, dots beyond a
    version shape, or are very short) are never rewritten — a typo in 'chrome'
    is safe to fix, a mangled URL is not.
    """
    if not token or token.lower() in vocabulary:
        return token
    if len(token) <= 2 or any(ch.isdigit() for ch in token):
        return token
    lowered = token.lower()
    best: tuple[str, float] = ("", 0.0)
    for candidate in vocabulary:
        ratio = SequenceMatcher(None, lowered, candidate.lower()).ratio()
        if ratio > best[1]:
            best = (candidate, ratio)
    return best[0] if best[1] >= threshold else token


@lru_cache(maxsize=4096)
def _fuzzy_contains_cached(haystack_lower: str, needle_lower: str) -> bool:
    ratio = SequenceMatcher(None, haystack_lower, needle_lower).ratio()
    return ratio >= 0.72


def fuzzy_contains(haystack: str, needle: str) -> bool:
    """True when the needle appears in the haystack allowing small typos —
    used for word-inside-phrase checks ('opn chrme' vs 'open chrome')."""
    hay = " ".join(normalized_tokens(haystack))
    nick = " ".join(normalized_tokens(needle))
    if not hay or not nick:
        return False
    if nick in hay:
        return True
    return _fuzzy_contains_cached(hay, nick)


def fuzzy_token_in(text: str, token: str) -> bool:
    """True when any token of `text` is a near match of `token`."""
    lowered = token.lower()
    for candidate in normalized_tokens(text):
        if candidate.lower() == lowered:
            return True
        if len(lowered) >= 4 and SequenceMatcher(None, candidate.lower(), lowered).ratio() >= 0.8:
            return True
    return False
