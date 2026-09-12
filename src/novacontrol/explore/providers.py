"""Online research providers for Explore."""

from __future__ import annotations

import base64
from dataclasses import replace
from html import unescape
import json
from html.parser import HTMLParser
import re
from typing import Protocol, runtime_checkable
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
from urllib.request import Request, urlopen

from novacontrol.explore.models import ResearchSource, VideoResult


@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        """Search online sources."""


@runtime_checkable
class VideoProvider(Protocol):
    async def search_videos(self, query: str, *, limit: int = 5) -> tuple[VideoResult, ...]:
        """Search related videos."""


class DuckDuckGoLiteSearchProvider:
    """Standard-library web search provider using DuckDuckGo Lite HTML."""

    endpoint = "https://lite.duckduckgo.com/lite/"

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        url = f"{self.endpoint}?q={quote_plus(query)}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _DuckDuckGoLiteParser(limit=limit)
        parser.feed(html)
        return parser.results()


class BingSearchProvider:
    """Fallback standard-library web search provider using Bing HTML."""

    endpoint = "https://www.bing.com/search"

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        url = f"{self.endpoint}?q={quote_plus(query)}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _BingSearchParser(limit=limit)
        parser.feed(html)
        return parser.results()


class GoogleSearchProvider:
    """Web search provider using Google HTML scraping."""

    endpoint = "https://www.google.com/search"

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        url = f"{self.endpoint}?q={quote_plus(query)}&num={limit}"
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _GoogleSearchParser(limit=limit)
        parser.feed(html)
        return parser.results()


class ResilientSearchProvider:
    """Try multiple web search providers and return the first usable result set."""

    def __init__(self, providers: tuple[SearchProvider, ...] | None = None) -> None:
        self.providers = providers or (
            DuckDuckGoLiteSearchProvider(),
            BingSearchProvider(),
            GoogleSearchProvider(),
        )

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                results = await provider.search(query, limit=limit)
            except Exception as exc:
                last_error = exc
                continue
            if results:
                return results
        if last_error is not None:
            raise last_error
        return ()


class YouTubeSearchVideoProvider:
    """Video provider that returns YouTube search result links for a topic."""

    async def search_videos(self, query: str, *, limit: int = 5) -> tuple[VideoResult, ...]:
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        request = Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=10) as response:
                html = response.read().decode("utf-8", errors="replace")
            videos = _extract_youtube_videos(html, limit=limit)
            if videos:
                return videos
        except Exception:
            pass
        return (
            VideoResult(
                title=f"Search YouTube for {query}",
                url=search_url,
                channel="YouTube",
                reason="Open this result to choose current videos related to the topic.",
            ),
        )[:limit]


class _DuckDuckGoLiteParser(HTMLParser):
    def __init__(self, *, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self._in_link = False
        self._in_snippet = False
        self._current_href = ""
        self._current_text: list[str] = []
        self._snippet_text: list[str] = []
        self._results: list[ResearchSource] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        if tag == "td" and "result-snippet" in attrs_dict.get("class", ""):
            self._in_snippet = True
            self._snippet_text = []
            return
        if tag != "a":
            return
        href = attrs_dict.get("href", "")
        if "uddg=" in href or href.startswith("http"):
            self._in_link = True
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._current_text.append(data)
        if self._in_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_snippet:
            snippet = " ".join(" ".join(self._snippet_text).split())
            if snippet and self._results:
                self._results[-1] = replace(self._results[-1], snippet=snippet)
            self._in_snippet = False
            self._snippet_text = []
            return
        if tag != "a" or not self._in_link:
            return
        title = " ".join(" ".join(self._current_text).split())
        url = _clean_url(self._current_href)
        if title and url and _is_usable_result(title, url) and not _is_duplicate(url, self._results):
            self._results.append(ResearchSource(title=title, url=url))
        self._in_link = False
        self._current_href = ""
        self._current_text = []

    def results(self) -> tuple[ResearchSource, ...]:
        return tuple(self._results[: self.limit])


class _BingSearchParser(HTMLParser):
    def __init__(self, *, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self._in_result = False
        self._result_depth = 0
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title_text: list[str] = []
        self._snippet_text: list[str] = []
        self._results: list[ResearchSource] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        classes = attrs_dict.get("class", "")
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
            self._result_depth = 1
            self._href = ""
            self._title_text = []
            self._snippet_text = []
            return
        if not self._in_result:
            return
        self._result_depth += 1
        if tag == "a" and not self._href:
            href = attrs_dict.get("href", "")
            if href:
                self._href = href
                self._in_title = True
        elif tag == "p":
            self._in_snippet = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_text.append(data)
        elif self._in_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_result:
            return
        if tag == "a":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        self._result_depth -= 1
        if tag == "li" and self._result_depth <= 0:
            self._finish_result()

    def _finish_result(self) -> None:
        title = _clean_html_text(" ".join(self._title_text))
        url = _clean_url(self._href)
        snippet = _clean_html_text(" ".join(self._snippet_text))
        if title and url and _is_usable_result(title, url) and not _is_duplicate(url, self._results):
            self._results.append(ResearchSource(title=title, url=url, snippet=snippet))
        self._in_result = False
        self._result_depth = 0
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title_text = []
        self._snippet_text = []

    def results(self) -> tuple[ResearchSource, ...]:
        return tuple(self._results[: self.limit])


class _GoogleSearchParser(HTMLParser):
    """Parser for Google search results HTML."""

    def __init__(self, *, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self._in_link = False
        self._in_snippet_div = False
        self._href = ""
        self._title_text: list[str] = []
        self._snippet_text: list[str] = []
        self._results: list[ResearchSource] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        classes = attrs_dict.get("class", "")

        # Detect result links - Google wraps titles in <a> tags with /url?q= format
        if tag == "a":
            href = attrs_dict.get("href", "")
            if "/url?q=" in href or (href.startswith("http") and "google" not in href.lower()):
                if not self._in_link:
                    self._in_link = True
                    self._href = href
                    self._title_text = []
                    self._snippet_text = []

        # Detect snippet containers
        if tag == "div" and any(cls in classes for cls in ("VwiC3b", "IsZvec", "s3v9rd")):
            self._in_snippet_div = True
            self._snippet_text = []

        if self._in_link or self._in_snippet_div:
            self._depth += 1

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_link:
            self._title_text.append(text)
        if self._in_snippet_div:
            self._snippet_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if self._depth > 0:
            self._depth -= 1

        if tag == "a" and self._in_link and self._depth <= 0:
            self._finish_link()

        if tag == "div" and self._in_snippet_div and self._depth <= 0:
            self._finish_snippet()

    def _finish_link(self) -> None:
        title = _clean_html_text(" ".join(self._title_text))
        url = _clean_url(self._href)
        if title and url and _is_usable_result(title, url) and not _is_duplicate(url, self._results):
            self._results.append(ResearchSource(title=title, url=url))
        self._in_link = False
        self._href = ""
        self._title_text = []

    def _finish_snippet(self) -> None:
        snippet = _clean_html_text(" ".join(self._snippet_text))
        if snippet and self._results:
            last = self._results[-1]
            if not last.snippet:
                self._results[-1] = replace(last, snippet=snippet)
        self._in_snippet_div = False
        self._snippet_text = []

    def results(self) -> tuple[ResearchSource, ...]:
        return tuple(self._results[: self.limit])


def _clean_url(value: str) -> str:
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])
    if "q" in query and parsed.netloc.endswith("google.com"):
        return unquote(query["q"][0])
    if parsed.netloc.endswith("bing.com") and parsed.path == "/ck/a":
        # Bing HTML-encodes ampersands, so keys become 'amp;u' instead of 'u'
        u_param = query.get("u", query.get("amp;u", []))
        if u_param:
            decoded = _decode_bing_url(u_param[0])
            if decoded:
                return decoded
    return value


def _decode_bing_url(value: str) -> str:
    payload = value[2:] if value.startswith("a1") else value
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}").decode("utf-8", errors="replace")
    except Exception:
        return ""
    return decoded if decoded.startswith(("http://", "https://")) else ""


def _clean_html_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _is_duplicate(url: str, results: list[ResearchSource]) -> bool:
    return any(result.url == url for result in results)


def _is_usable_result(title: str, url: str) -> bool:
    parsed = urlparse(url)
    lower_title = title.strip().lower()
    if lower_title in {"more info", "ad", "ads"}:
        return False
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path in {"/y.js", "/duckduckgo-help-pages/company/ads-by-microsoft-on-duckduckgo-private-search/"}:
        return False
    return True


def _youtube_thumbnail_from_url(url: str) -> str:
    parsed = urlparse(url)
    video_id = ""
    if parsed.netloc.endswith("youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.netloc.endswith("youtu.be"):
        video_id = parsed.path.strip("/")
    if not video_id:
        return ""
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def _extract_youtube_videos(html: str, *, limit: int) -> tuple[VideoResult, ...]:
    marker = "var ytInitialData ="
    start = html.find(marker)
    if start == -1:
        start = html.find("ytInitialData")
    if start == -1:
        return ()
    json_text = _extract_json_object(html, html.find("{", start))
    if not json_text:
        return ()
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return ()
    results: list[VideoResult] = []
    for renderer in _walk_video_renderers(payload):
        video_id = str(renderer.get("videoId", ""))
        title = _runs_text(renderer.get("title", {}))
        if not video_id or not title:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        thumbnail = _thumbnail(renderer) or _youtube_thumbnail_from_url(url)
        video = VideoResult(
            title=title,
            url=url,
            channel=_runs_text(renderer.get("ownerText", {})),
            duration=_duration(renderer),
            reason="Current YouTube result for this topic.",
            thumbnail_url=thumbnail,
        )
        if all(existing.url != video.url for existing in results):
            results.append(video)
        if len(results) >= limit:
            break
    return tuple(results)


def _extract_json_object(text: str, start: int) -> str:
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _walk_video_renderers(value: object) -> tuple[dict[str, object], ...]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        renderer = value.get("videoRenderer")
        if isinstance(renderer, dict):
            found.append(renderer)
        for child in value.values():
            found.extend(_walk_video_renderers(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_video_renderers(child))
    return tuple(found)


def _runs_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("simpleText"):
        return str(value["simpleText"])
    runs = value.get("runs")
    if isinstance(runs, list):
        return " ".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
    return ""


def _thumbnail(renderer: dict[str, object]) -> str:
    thumbnail = renderer.get("thumbnail", {})
    if not isinstance(thumbnail, dict):
        return ""
    thumbnails = thumbnail.get("thumbnails", [])
    if not isinstance(thumbnails, list) or not thumbnails:
        return ""
    candidate = thumbnails[-1]
    if not isinstance(candidate, dict):
        return ""
    return re.sub(r"&amp;", "&", str(candidate.get("url", "")))


def _duration(renderer: dict[str, object]) -> str:
    length_text = renderer.get("lengthText", {})
    return _runs_text(length_text)


# ────────────────────────────────────────────────────────────
# Trending research topics (daily world updates)
# ────────────────────────────────────────────────────────────

class _RssHeadlineParser(HTMLParser):
    """Minimal RSS reader: <item><title>/<pubDate> pairs from a news feed."""

    def __init__(self) -> None:
        super().__init__()
        self._in_item = False
        self._in_title = False
        self._in_date = False
        self._text: list[str] = []
        self.headlines: list[tuple[str, str]] = []  # (title, pubDate)
        self._title = ""
        self._date = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "item":
            self._in_item = True
            self._title = ""
        elif self._in_item and tag == "title":
            self._in_title = True
            self._text = []
        elif self._in_item and tag == "pubdate":
            self._in_date = True
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_title or self._in_date:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._title = " ".join("".join(self._text).split())
            self._in_title = False
        elif tag == "pubdate" and self._in_date:
            self._in_date = False
        elif tag == "item" and self._in_item:
            self._in_item = False
            if self._title:
                self.headlines.append((self._title, self._date))
            self._date = ""


def _news_feed_url(edition: str) -> str:
    """Google News RSS top-stories URL for a home-country edition.

    ``edition`` is an ISO-ish country code ("us", "in", "gb"); unknown codes
    fall back to the US edition rather than erroring.
    """
    code = (edition or "us").strip().lower() or "us"
    supported = {"us": ("en-US", "US", "US:en"), "in": ("en-IN", "IN", "IN:en"), "gb": ("en-GB", "GB", "GB:en")}
    hl, gl, ceid = supported.get(code, supported["us"])
    return f"https://news.google.com/rss?hl={hl}&gl={gl}&ceid={ceid}"


def fetch_trending_headlines(*, edition: str = "us", timeout: int = 8) -> tuple[str, ...]:
    """Live top-story headlines from Google News RSS (standard library only).

    Returns clean headline strings (publisher suffix stripped). Empty tuple on
    any failure — callers own the fallback, network problems must never raise
    into the API layer.
    """
    url = _news_feed_url(edition)
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=timeout) as response:
            xml = response.read().decode("utf-8", errors="replace")
    except Exception:
        return ()
    parser = _RssHeadlineParser()
    try:
        parser.feed(xml)
    except Exception:
        return ()
    titles: list[str] = []
    for title, _date in parser.headlines:
        # Google News appends " - Publisher"; the research topic is the story.
        cleaned = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
        cleaned = unescape(cleaned)
        if len(cleaned) >= 8:
            titles.append(cleaned)
    return tuple(titles)
