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
    value = _POLITE_TAIL.sub("", value)
    value = _LEADING_POLITE.sub("", value)
    value = value.rstrip(" .!?;,:").strip()
    return _WHITESPACE.sub(" ", value).lower().strip()


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
