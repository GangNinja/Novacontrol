"""Read the pages a search returned, so an answer can use the article itself.

Owns: fetching one source URL, pulling its readable prose out of the HTML, and
caching the result for the process lifetime.

Why this module exists. Search results carry a 150-300 character blurb — often
a meta description cut mid-sentence ("Introduction Among the many offences
which are delineated by the Code, there exists one which is known as …").
Those blurbs were the ONLY evidence synthesis ever had, so an Explore answer
could do nothing but re-list them: it never saw the paragraph that answers the
question. Fixing the evidence is the one fix that is not another keyword rule.

Standard library only (`urllib` + `html.parser`): the project has no HTML
dependency and this must not add one. No JavaScript is executed, so a
script-rendered page degrades to "no readable text" and the caller keeps the
blurb — reading is an upgrade, never a requirement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from html import unescape
from html.parser import HTMLParser
import logging
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from novacontrol.performance import TtlCache

logger = logging.getLogger(__name__)


# Tags whose contents are never prose. `head` is here with the script/style
# block so titles and meta tags cannot leak in as sentences.
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "head", "iframe",
    "nav", "header", "footer", "aside", "form", "button", "select", "option",
    "label", "figcaption", "code", "pre",
})

# Tags that end a run of text. A paragraph boundary is the only structural
# signal an answer needs: within one block the sentences belong together.
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "li", "br", "tr", "td", "th", "dd", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "table", "hr",
})

# A block shorter than this is a label, a button, or a nav crumb — never the
# sentence that answers a question.
_MIN_BLOCK_CHARS = 45

# One page of prose is plenty of evidence; the rest is footer and related links.
_MAX_BLOCK_CHARS = 1500

# A hard ceiling on what one page may contribute, so six sources cannot push a
# small model's context or the local ranker out of proportion.
_DEFAULT_MAX_CHARS = 12_000

# Reading is done once per URL per run of the app. Five minutes matches the
# report cache, so a re-asked question is served from the same evidence.
_DEFAULT_CACHE_TTL_SECONDS = 300.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_NON_HTML_TYPES = ("application/pdf", "image/", "audio/", "video/", "application/zip")


class _ProseExtractor(HTMLParser):
    """Collect the text of prose blocks, skipping chrome and non-content tags.

    State is a single skip depth: everything inside a skipped tag is ignored,
    including nested blocks, so a `<nav>` full of links contributes nothing while
    the article next to it is read normally.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._buffer: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_depth and tag.lower() in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self._buffer.append(text)

    def _flush(self) -> None:
        if self._buffer:
            self.blocks.append(" ".join(self._buffer))
            self._buffer = []

    def close(self) -> None:
        super().close()
        self._flush()


# Artifacts of reading HTML as text: citation markers ("[ 3 ]"), a space left
# before punctuation, and a space pushed inside a bracket. They are noise in a
# quoted sentence and they made a live answer read like a scraped document.
_CITATION = re.compile(r"\s*\[\s*\d+\s*\]\s*")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?%])")
_SPACE_INSIDE_BRACKET = re.compile(r"\(\s+|\s+\)")
_ENTITY = re.compile(r"&[a-zA-Z#][a-zA-Z0-9]{1,8};")


def tidy_text(text: str) -> str:
    """Turn extracted HTML text into the sentences a reader would quote."""
    cleaned = unescape(text)
    # Some pages double-encode (&amp;quot;), so resolve a second layer when the
    # first pass left entities behind — but only while that is what is left.
    if _ENTITY.search(cleaned):
        cleaned = unescape(cleaned)
    cleaned = _CITATION.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
    cleaned = _SPACE_INSIDE_BRACKET.sub(lambda match: "(" if match.group(0).startswith("(") else ")", cleaned)
    return " ".join(cleaned.split())


def _looks_like_prose(text: str) -> bool:
    """True when a block reads as a sentence, not as a menu or a byline.

    Shape, not vocabulary: enough letters, several words, and at least one
    sentence mark. A block of nav labels ("Home Shop About Contact") has the
    first two and fails the third.
    """
    letters = sum(character.isalpha() for character in text)
    if letters < len(text) * 0.55:
        return False
    if len(text.split()) < 8:
        return False
    return any(mark in text for mark in ".!?")


def extract_prose(html: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """The readable prose of one HTML document, as blank-line separated blocks."""
    parser = _ProseExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # malformed markup must not break a research run
        logger.debug("HTML parse failed: %s", exc)

    blocks: list[str] = []
    seen: set[str] = set()
    total = 0
    for block in parser.blocks:
        if not (_MIN_BLOCK_CHARS <= len(block) <= _MAX_BLOCK_CHARS):
            continue
        if not _looks_like_prose(block):
            continue
        key = block.lower()[:120]
        if key in seen:
            continue
        seen.add(key)
        cleaned = tidy_text(block)
        if len(cleaned) < _MIN_BLOCK_CHARS:
            continue
        blocks.append(cleaned)
        total += len(cleaned)
        if total >= max_chars:
            break
    return "\n\n".join(blocks)[:max_chars]


def _decode(raw: bytes, charset: str | None) -> str:
    """Decode a page body, preferring the charset the server declared."""
    for candidate in (charset, "utf-8", "cp1252"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


class PageReader:
    """Fetches source URLs and returns their readable prose.

    Never raises: a blocked, slow, script-only, or 404 page yields "" so the
    caller keeps the search blurb it already had.
    """

    def __init__(
        self,
        *,
        timeout: float = 6.0,
        max_chars: int = _DEFAULT_MAX_CHARS,
        max_pages: int = 6,
        concurrency: int = 4,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        cache: TtlCache[str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_chars = max_chars
        self.max_pages = max_pages
        self.concurrency = max(1, concurrency)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache = cache or TtlCache()

    def read(self, url: str) -> str:
        """Read one page's prose, from cache when possible. Never raises."""
        if not url or not url.lower().startswith(("http://", "https://")):
            return ""
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        text = self._fetch(url)
        if not text and "+" in urlparse(url).path:
            # Search engines hand back wiki titles with "+" for the spaces, which
            # is not the canonical URL and 404s — so a whole research run lost
            # its page text and fell back to meta descriptions. Retried once with
            # the separator the wiki actually uses.
            retry = _wiki_style_url(url)
            if retry != url:
                text = self._fetch(retry)
        self.cache.set(url, text, ttl_seconds=self.cache_ttl_seconds)
        return text

    def _fetch(self, url: str) -> str:
        request = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if any(kind in content_type for kind in _NON_HTML_TYPES):
                    return ""
                # Cap the download: a runaway response is not evidence.
                raw = response.read(3_000_000)
                charset = response.headers.get_content_charset()
        except Exception as exc:
            logger.debug("Page read failed for %s: %s", url, exc)
            return ""
        return extract_prose(_decode(raw, charset), max_chars=self.max_chars)

    async def read_many(self, urls: Sequence[str], *, limit: int | None = None) -> dict[str, str]:
        """Read several pages concurrently; returns {url: prose} for the reads
        that produced text. Unreadable pages are simply absent from the result.

        Fetching is blocking I/O, so each page is read on a worker thread and
        the whole batch is capped by one deadline — a single slow host must not
        hold the research run open.
        """
        unique: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            unique.append(url)
        cap = min(len(unique), self.max_pages, limit if limit is not None else self.max_pages)
        targets = unique[:cap]
        if not targets:
            return {}

        gate = asyncio.Semaphore(self.concurrency)

        async def _one(url: str) -> tuple[str, str]:
            async with gate:
                return url, await asyncio.to_thread(self.read, url)

        try:
            results = await asyncio.gather(*(_one(url) for url in targets))
        except Exception as exc:  # pragma: no cover - gather only raises on cancellation
            logger.debug("Concurrent page read failed: %s", exc)
            return {}
        return {url: text for url, text in results if text.strip()}


def _wiki_style_url(url: str) -> str:
    """Swap "+" for "_" in a URL path (the wiki separator)."""
    parsed = urlparse(url)
    return parsed._replace(path=parsed.path.replace("+", "_")).geturl()


def readable_host(url: str) -> str:
    """The host of a source URL (empty when the URL is unusable)."""
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""
