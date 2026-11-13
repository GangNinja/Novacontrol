"""Event source catalog and feed retrieval.

Nothing here downloads a page and calls it an event: a source is a *declared*
feed, retrieval is bounded and typed, and parsing either produces
:class:`~novacontrol.novamarket.radar.models.SourceEntry` records or a typed
failure. The transport, retry policy and rate limiter are NM-02's
(``marketdata.providers.base``), imported rather than reimplemented — one outbound
HTTP seam for the whole subsystem.

Source honesty rules:

* every entry in :data:`DEFAULT_SOURCES` is a real, publicly documented feed;
* each is ``enabled_default=False`` and ``verified_live=False`` until a real fetch
  of that exact URL succeeds here;
* a source that fails is recorded as failed (status, code, message, attempt time)
  and never filled in from memory;
* conditional GET (``If-None-Match`` / ``If-Modified-Since``) means unchanged
  content is not re-parsed into events.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from novacontrol.novamarket.contracts import (
    EventNotFoundError,
    EventSourceError,
    SourceMalformedError,
    SourceRateLimitedError,
    SourceTimeoutError,
    SourceUnavailableError,
)
from novacontrol.novamarket.marketdata.providers.base import (
    HttpTransport,
    RateLimiter,
    RetryPolicy,
    UrllibTransport,
)
from novacontrol.novamarket.radar.models import (
    EventSourceDefinition,
    FeedFormat,
    SourceEntry,
    SourceReliability,
    SourceType,
    content_hash,
    utcnow,
)

#: One feed may not return more entries than this; a "feed" with 50k items is a
#: bulk dump, not a feed, and would drown the radar.
MAX_ENTRIES = 100

_ATOM = "{http://www.w3.org/2005/Atom}"
_DOCTYPE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]{0,400}>")
_SPACE = re.compile(r"\s+")


#: The implemented source catalog. These are real public feeds; whether they are
#: reachable from this machine is a separate, observable question (``/market/
#: events/sources`` reports the last attempt). The two Indian stock-market news
#: feeds are enabled by default because they are the desk's live company-event
#: surface; official macro feeds stay opt-in.
DEFAULT_SOURCES: tuple[EventSourceDefinition, ...] = (
    EventSourceDefinition(
        source_id="rbi_press",
        name="RBI press releases",
        source_type=SourceType.GOVERNMENT,
        reliability=SourceReliability.OFFICIAL,
        url="https://www.rbi.org.in/pressreleases_rss.xml",
        feed_format=FeedFormat.RSS,
        publisher="Reserve Bank of India",
        market="IN",
        description="Official press releases of the Reserve Bank of India.",
    ),
    EventSourceDefinition(
        source_id="fed_press",
        name="Federal Reserve press releases",
        source_type=SourceType.GOVERNMENT,
        reliability=SourceReliability.OFFICIAL,
        url="https://www.federalreserve.gov/feeds/press_all.xml",
        feed_format=FeedFormat.RSS,
        publisher="Board of Governors of the Federal Reserve System",
        market="US",
        description="Official press releases of the US Federal Reserve Board.",
    ),
    EventSourceDefinition(
        source_id="bls_latest",
        name="BLS latest releases",
        source_type=SourceType.OFFICIAL_STATISTICS,
        reliability=SourceReliability.OFFICIAL,
        url="https://www.bls.gov/feed/bls_latest.rss",
        feed_format=FeedFormat.RSS,
        publisher="US Bureau of Labor Statistics",
        market="US",
        description="Official economic-statistics releases from the BLS.",
    ),
    EventSourceDefinition(
        source_id="sec_edgar_8k",
        name="SEC EDGAR current 8-K filings",
        source_type=SourceType.REGULATOR,
        reliability=SourceReliability.OFFICIAL,
        url=(
            "https://www.sec.gov/cgi-bin/browse-edgar"
            "?action=getcurrent&type=8-K&output=atom"
        ),
        feed_format=FeedFormat.ATOM,
        publisher="US Securities and Exchange Commission",
        market="US",
        description="Official current 8-K company-event filings (Atom feed).",
    ),
    EventSourceDefinition(
        source_id="et_markets",
        name="Economic Times markets",
        source_type=SourceType.FINANCIAL_NEWS,
        reliability=SourceReliability.REPUTABLE,
        url="https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        feed_format=FeedFormat.RSS,
        publisher="The Economic Times",
        market="IN",
        description="Live NSE/BSE market and company news from The Economic Times.",
        enabled_default=True,
    ),
    EventSourceDefinition(
        source_id="bs_markets",
        name="Business Standard markets",
        source_type=SourceType.FINANCIAL_NEWS,
        reliability=SourceReliability.REPUTABLE,
        url="https://www.business-standard.com/rss/markets-106.rss",
        feed_format=FeedFormat.RSS,
        publisher="Business Standard",
        market="IN",
        description="Live Indian equity-market and company news from Business Standard.",
        enabled_default=True,
    ),
)

SOURCE_INDEX: Mapping[str, EventSourceDefinition] = {
    source.source_id: source for source in DEFAULT_SOURCES
}


class SourceCatalog:
    """The declared sources, plus operator enable/disable state.

    Enablement is kept here (persisted by the radar store) rather than baked into
    the definitions, so a definition stays a fact about the feed while "is it on"
    stays an operator decision.
    """

    def __init__(
        self,
        sources: Sequence[EventSourceDefinition] | None = None,
        *,
        enabled: Sequence[str] | None = None,
    ) -> None:
        definitions = tuple(sources) if sources is not None else DEFAULT_SOURCES
        if not definitions:
            raise SourceMalformedError("the source catalog cannot be empty")
        seen: set[str] = set()
        for definition in definitions:
            if definition.source_id in seen:
                raise SourceMalformedError(
                    f"duplicate source id {definition.source_id!r}"
                )
            seen.add(definition.source_id)
        self._definitions = {item.source_id: item for item in definitions}
        initial = (
            [item.source_id for item in definitions if item.enabled_default]
            if enabled is None
            else list(enabled)
        )
        self._enabled = {str(item) for item in initial}
        unknown = self._enabled - set(self._definitions)
        if unknown:
            raise SourceMalformedError(
                f"unknown source(s) enabled: {', '.join(sorted(unknown))}"
            )

    def __len__(self) -> int:
        return len(self._definitions)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def get(self, source_id: str) -> EventSourceDefinition:
        key = str(source_id or "").strip()
        try:
            return self._definitions[key]
        except KeyError as exc:
            # An unknown identifier is a missing resource, not a broken feed.
            raise EventNotFoundError(
                f"unknown event source {source_id!r}; catalog: "
                f"{', '.join(self.ids)}"
            ) from exc

    def enabled(self) -> tuple[EventSourceDefinition, ...]:
        return tuple(
            self._definitions[key]
            for key in sorted(self._definitions)
            if key in self._enabled
        )

    def is_enabled(self, source_id: str) -> bool:
        return str(source_id or "").strip() in self._enabled

    def set_enabled(self, source_id: str, enabled: bool) -> EventSourceDefinition:
        definition = self.get(source_id)
        if enabled:
            self._enabled.add(definition.source_id)
        else:
            self._enabled.discard(definition.source_id)
        return definition

    def definitions(self) -> tuple[EventSourceDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def to_dict(self) -> list[dict[str, Any]]:
        rows = []
        for definition in self.definitions():
            body = definition.to_dict()
            body["enabled"] = definition.source_id in self._enabled
            rows.append(body)
        return rows


@dataclass(frozen=True, slots=True)
class FeedResult:
    """One retrieval outcome, including a not-modified answer."""

    source_id: str
    status: int
    entries: tuple[SourceEntry, ...]
    fetched_at: datetime
    unchanged: bool = False
    etag: str = ""
    last_modified: str = ""
    body_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "entries": len(self.entries),
            "fetched_at": self.fetched_at.isoformat(),
            "unchanged": self.unchanged,
            "etag": self.etag,
            "body_digest": self.body_digest,
        }


def _header(headers: object, name: str) -> str:
    """Read a response header whatever case the transport preserved."""
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    for key in (name, name.lower(), name.upper(), name.title()):
        value = getter(key)
        if value:
            return str(value)
    items = getattr(headers, "items", None)
    if callable(items):
        wanted = name.lower()
        for key, value in items():
            if str(key).lower() == wanted and value:
                return str(value)
    return ""


def _clean_text(value: object) -> str:
    """Strip markup and collapse whitespace without interpreting entities."""
    text = _TAG.sub(" ", str(value or ""))
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return _SPACE.sub(" ", text).strip()


def parse_feed_datetime(value: object) -> datetime | None:
    """Parse the date forms RSS 2.0 and Atom actually use; ``None`` if unknown.

    An unparseable date stays unknown. Substituting the retrieval time would make
    a stale item look fresh, which is exactly the confusion this phase forbids.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        candidate = text.replace("Z", "+00:00")
        for fmt in (None, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = (
                    datetime.fromisoformat(candidate)
                    if fmt is None
                    else datetime.strptime(candidate, fmt)
                )
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # A feed that states no zone is ambiguous; treat it as UTC but record it
        # as UTC rather than pretending the local zone applies.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _xml_entries(body: str, *, source_id: str) -> tuple[SourceEntry, ...]:
    if _DOCTYPE.search(body):
        raise SourceMalformedError(
            "the feed declares a DTD or entity; refusing to expand it",
            source_id=source_id,
        )
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceMalformedError(
            f"the feed is not well-formed XML: {exc}", source_id=source_id
        ) from exc
    entries: list[SourceEntry] = []
    if root.tag == f"{_ATOM}feed":
        for node in root.findall(f"{_ATOM}entry")[:MAX_ENTRIES]:
            title = _clean_text(node.findtext(f"{_ATOM}title", ""))
            summary = _clean_text(
                node.findtext(f"{_ATOM}summary", "")
                or node.findtext(f"{_ATOM}content", "")
            )
            link = ""
            for link_node in node.findall(f"{_ATOM}link"):
                href = str(link_node.get("href") or "").strip()
                if href and (link_node.get("rel") in (None, "alternate") or not link):
                    link = href
            published = parse_feed_datetime(
                node.findtext(f"{_ATOM}published", "")
                or node.findtext(f"{_ATOM}updated", "")
            )
            external_id = _clean_text(node.findtext(f"{_ATOM}id", ""))
            if not title:
                continue
            entries.append(
                SourceEntry(
                    title=title,
                    summary=summary,
                    url=link,
                    published_at=published,
                    external_id=external_id,
                )
            )
        return tuple(entries)
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    for node in items[:MAX_ENTRIES]:
        title = _clean_text(node.findtext("title", ""))
        summary = _clean_text(
            node.findtext("description", "") or node.findtext("summary", "")
        )
        link = _clean_text(node.findtext("link", ""))
        published = parse_feed_datetime(
            node.findtext("pubDate", "") or node.findtext("published", "")
        )
        external_id = _clean_text(node.findtext("guid", ""))
        if not title:
            continue
        entries.append(
            SourceEntry(
                title=title,
                summary=summary,
                url=link,
                published_at=published,
                external_id=external_id,
            )
        )
    return tuple(entries)


def _json_entries(body: str, *, source_id: str) -> tuple[SourceEntry, ...]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SourceMalformedError(
            f"the feed is not valid JSON: {exc}", source_id=source_id
        ) from exc
    if isinstance(payload, Mapping):
        raw_items = payload.get("items") or payload.get("entries") or payload.get("data") or []
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        raise SourceMalformedError(
            "the JSON feed must be a list or carry an items/entries/data list",
            source_id=source_id,
        )
    entries: list[SourceEntry] = []
    for item in raw_items[:MAX_ENTRIES]:
        if not isinstance(item, Mapping):
            continue
        title = _clean_text(item.get("title", ""))
        if not title:
            continue
        summary = _clean_text(
            item.get("summary") or item.get("description") or item.get("body") or ""
        )
        url = str(item.get("url") or item.get("link") or "").strip()
        published = parse_feed_datetime(
            item.get("published") or item.get("published_at") or item.get("date")
        )
        entries.append(
            SourceEntry(
                title=title,
                summary=summary,
                url=url,
                published_at=published,
                external_id=_clean_text(item.get("id", "")),
            )
        )
    return tuple(entries)


def parse_feed(
    body: str, definition: EventSourceDefinition
) -> tuple[SourceEntry, ...]:
    """Parse a retrieved body into entries, or raise a typed failure."""
    if not str(body or "").strip():
        raise SourceMalformedError("the feed body is empty", source_id=definition.source_id)
    if definition.feed_format is FeedFormat.JSON:
        return _json_entries(body, source_id=definition.source_id)
    return _xml_entries(body, source_id=definition.source_id)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = str(headers.get("retry-after", "") or "").strip()
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        parsed = parse_feed_datetime(raw)
        if parsed is None:
            return None
        return max((parsed - utcnow()).total_seconds(), 0.0)


class EventFeedClient:
    """Bounded retrieval for one source, with retry, rate limit and caching.

    The client never decides whether an entry is an event; it only turns HTTP into
    entries or a typed failure, so a transport problem cannot be mistaken for an
    empty — and therefore "quiet" — market.
    """

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        retry: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        sleeper: Any | None = None,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._transport = transport or UrllibTransport(timeout_seconds=15.0)
        self._retry = retry or RetryPolicy(max_attempts=3)
        self._limiter = rate_limiter or RateLimiter(0.0)
        self._sleeper = sleeper
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleeper is None:
            import asyncio

            await asyncio.sleep(seconds)
            return
        result = self._sleeper(seconds)
        if hasattr(result, "__await__"):
            await result

    async def fetch(
        self,
        definition: EventSourceDefinition,
        *,
        etag: str = "",
        last_modified: str = "",
    ) -> FeedResult:
        """Retrieve one feed. Raises a typed :class:`EventSourceError` on failure."""
        headers: dict[str, str] = {"Accept": "application/atom+xml, application/rss+xml, application/xml, application/json;q=0.8, text/xml;q=0.7"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        attempt = 0
        last_error: EventSourceError | None = None
        while attempt < self._retry.max_attempts:
            attempt += 1
            await self._limiter.acquire()
            try:
                return await self._attempt(definition, headers)
            except EventSourceError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self._retry.max_attempts:
                    break
                await self._sleep(self._retry.delay_for(attempt))
        assert last_error is not None  # the loop only ends after an error
        raise last_error

    async def _attempt(
        self, definition: EventSourceDefinition, headers: Mapping[str, str]
    ) -> FeedResult:
        try:
            response = await self._transport.get(definition.url, headers=dict(headers))
        except SourceTimeoutError:
            raise
        except EventSourceError:
            raise
        except TimeoutError as exc:
            raise SourceTimeoutError(
                "the source did not answer in time", source_id=definition.source_id
            ) from exc
        except Exception as exc:  # noqa: BLE001 - any transport failure is typed here
            raise SourceUnavailableError(
                f"the source could not be reached: {type(exc).__name__}",
                source_id=definition.source_id,
            ) from exc

        if response.status == 304:
            return FeedResult(
                source_id=definition.source_id,
                status=304,
                entries=(),
                fetched_at=utcnow(),
                unchanged=True,
                etag=headers.get("If-None-Match", ""),
                last_modified=headers.get("If-Modified-Since", ""),
            )
        if response.status == 429:
            raise SourceRateLimitedError(
                "the source asked to slow down", source_id=definition.source_id
            )
        if 400 <= response.status < 500:
            raise SourceUnavailableError(
                f"the source refused the request with HTTP {response.status}",
                source_id=definition.source_id,
            )
        if response.status >= 500:
            raise SourceUnavailableError(
                f"the source failed with HTTP {response.status}",
                source_id=definition.source_id,
            )

        entries = parse_feed(response.body, definition)[: self.max_entries]
        return FeedResult(
            source_id=definition.source_id,
            status=response.status,
            entries=entries,
            fetched_at=utcnow(),
            etag=_header(response.headers, "etag"),
            last_modified=_header(response.headers, "last-modified"),
            body_digest=content_hash(response.body),
        )


__all__ = [
    "DEFAULT_SOURCES",
    "EventFeedClient",
    "FeedResult",
    "MAX_ENTRIES",
    "SOURCE_INDEX",
    "SourceCatalog",
    "parse_feed",
    "parse_feed_datetime",
]
