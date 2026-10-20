"""Wikipedia fallback search provider for Explore.

The general web search stack lives in providers.py (DuckDuckGo Lite -> Bing ->
Google); this module exists only because the service additionally falls back to
Wikipedia when web results are too few.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from novacontrol.explore.models import ResearchSource


# The Wikipedia fallback is a per-request HTTP call with a 5s socket timeout.
_WIKI_TIMEOUT_SECONDS = 5.0


async def _fetch_json(url: str) -> Any:
    """Fetch and decode a JSON body off the event loop (``urlopen`` blocks)."""

    def _blocking() -> Any:
        request = Request(url, headers={"User-Agent": "NovaControl/1.0"})
        with urlopen(request, timeout=_WIKI_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    return await asyncio.to_thread(_blocking)


class WikipediaSearchProvider:
    """Fallback: search Wikipedia for topic information.

    Uses the Wikipedia search API to find articles by content,
    not just by exact title match.
    """

    summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/"
    search_url = "https://en.wikipedia.org/w/api.php"

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        try:
            clean = query.strip().replace("_", " ")
            # Try the exact query via summary API
            result = await self._fetch_wiki(clean)
            if result:
                return (result,)

            # Try first few words via summary API
            words = clean.split()
            if len(words) > 2:
                for n in range(len(words), 1, -1):
                    candidate = " ".join(words[:n])
                    result = await self._fetch_wiki(candidate)
                    if result:
                        return (result,)

            # Fall back to Wikipedia search API (searches article content)
            results = await self._search_wiki(clean, limit=limit)
            if results:
                return tuple(results)

            return ()
        except Exception:
            return ()

    async def _fetch_wiki(self, title: str) -> ResearchSource | None:
        try:
            wiki_title = title.replace(" ", "_")
            url = f"{self.summary_url}{quote_plus(wiki_title)}"
            data = await _fetch_json(url)

            if data.get("type") == "standard":
                page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
                return ResearchSource(
                    title=data.get("title", title),
                    url=page_url or f"https://en.wikipedia.org/wiki/{quote_plus(title)}",
                    snippet=data.get("extract", ""),
                    source_type="wikipedia",
                )
        except Exception:
            pass
        return None

    async def _search_wiki(self, query: str, *, limit: int = 3) -> list[ResearchSource]:
        """Search Wikipedia articles by content using the search API."""
        try:
            url = f"{self.search_url}?action=query&list=search&srsearch={quote_plus(query)}&srlimit={limit}&format=json"
            data = await _fetch_json(url)

            results = []
            for item in data.get("query", {}).get("search", [])[:limit]:
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                # Clean HTML from snippet
                snippet = re.sub(r"<[^>]+>", "", snippet)
                if title:
                    page_url = f"https://en.wikipedia.org/wiki/{quote_plus(title)}"
                    results.append(ResearchSource(
                        title=title,
                        url=page_url,
                        snippet=snippet,
                        source_type="wikipedia",
                    ))
            return results
        except Exception:
            return []