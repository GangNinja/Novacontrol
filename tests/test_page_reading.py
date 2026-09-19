"""Page reading: the evidence an answer is actually built from.

Pins two things:

1. **Extraction** — script/style/nav/footer text never becomes prose, short
   blocks (labels, crumbs) are dropped, and repeated blocks collapse. This is
   what keeps a page's chrome out of an answer.
2. **Failure behavior** — an unreachable, non-HTML, or malformed page yields ""
   instead of raising, so reading is an upgrade over the search blurb and never
   a way for research to fail.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from novacontrol.explore import page_reader
from novacontrol.explore.page_reader import PageReader, extract_prose

_ARTICLE = """<!doctype html>
<html><head>
  <title>Purring explained</title>
  <style>.ad { color: red; }</style>
  <script>var tracking = "cats purr because javascript";</script>
</head>
<body>
  <nav><a href="/">Home</a><a href="/cats">Cats</a><a href="/contact">Contact us</a></nav>
  <header><p>Site header strapline goes here and is long enough to look like prose.</p></header>
  <main>
    <h1>Why cats purr</h1>
    <p>Cats purr by vibrating their vocal folds about 25 times a second, which is
       why a purr is a low rumble rather than a meow.</p>
    <p>Cats purr by vibrating their vocal folds about 25 times a second, which is
       why a purr is a low rumble rather than a meow.</p>
    <p>Purring also happens when a cat is in pain or frightened, so it is not
       always a sign of contentment.</p>
    <div>Share</div>
  </main>
  <aside><p>Related articles you might also want to read today about cats and dogs.</p></aside>
  <footer><p>Copyright 2026 Example Media. All rights reserved worldwide, so please do not copy.</p></footer>
  <p>A note at the end of the page that also reads like a normal sentence about cats.</p>
</body></html>
"""


class ProseExtractionTests(unittest.TestCase):
    """Chrome is dropped, prose is kept, duplicates collapse."""

    def setUp(self) -> None:
        self.prose = extract_prose(_ARTICLE)

    def test_keeps_the_article_sentences(self) -> None:
        self.assertIn("Cats purr by vibrating their vocal folds", self.prose)
        self.assertIn("Purring also happens when a cat is in pain", self.prose)

    def test_drops_script_and_style_text(self) -> None:
        self.assertNotIn("tracking", self.prose)
        self.assertNotIn("color: red", self.prose)
        self.assertNotIn("javascript", self.prose)

    def test_drops_navigation_header_aside_and_footer(self) -> None:
        for chrome in ("Contact us", "Site header strapline", "Related articles", "Copyright 2026"):
            with self.subTest(chrome=chrome):
                self.assertNotIn(chrome, self.prose)

    def test_drops_short_label_blocks(self) -> None:
        self.assertNotIn("Share", self.prose)

    def test_collapses_repeated_blocks(self) -> None:
        self.assertEqual(self.prose.count("Cats purr by vibrating their vocal folds"), 1)

    def test_empty_and_malformed_html_do_not_raise(self) -> None:
        self.assertEqual(extract_prose(""), "")
        self.assertEqual(extract_prose("<p>unclosed <div><span>"), "")

    def test_length_is_capped(self) -> None:
        long_page = "".join(f"<p>Sentence number {i} about a topic that runs on.</p>" for i in range(400))
        self.assertLessEqual(len(extract_prose(long_page, max_chars=500)), 500)


class _FakeResponse:
    """Minimal urlopen() stand-in: read() plus a headers mapping."""

    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = body
        self.headers = _FakeHeaders(content_type)

    def read(self, *_args: object) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get(self, name: str, default: str = "") -> str:
        return self._content_type if name.lower() == "content-type" else default

    def get_content_charset(self) -> str | None:
        if "charset=" in self._content_type:
            return self._content_type.split("charset=", 1)[1].split(";")[0].strip()
        return None


class PageReaderTests(unittest.TestCase):
    """A reader that never raises, and never re-fetches the same page."""

    def test_reads_and_extracts_a_page(self) -> None:
        reader = PageReader()
        with patch.object(page_reader, "urlopen", return_value=_FakeResponse(_ARTICLE.encode())):
            text = reader.read("https://example.com/purring")
        self.assertIn("Cats purr by vibrating", text)

    def test_non_http_urls_are_skipped_without_fetching(self) -> None:
        reader = PageReader()
        with patch.object(page_reader, "urlopen") as fake:
            self.assertEqual(reader.read("file:///etc/passwd"), "")
            self.assertEqual(reader.read(""), "")
        fake.assert_not_called()

    def test_fetch_failure_yields_empty_text(self) -> None:
        reader = PageReader()
        with patch.object(page_reader, "urlopen", side_effect=OSError("blocked")):
            self.assertEqual(reader.read("https://example.com/x"), "")

    def test_non_html_content_is_not_read_as_prose(self) -> None:
        reader = PageReader()
        with patch.object(
            page_reader, "urlopen", return_value=_FakeResponse(b"%PDF-1.4", "application/pdf")
        ):
            self.assertEqual(reader.read("https://example.com/paper.pdf"), "")

    def test_second_read_comes_from_cache(self) -> None:
        reader = PageReader()
        with patch.object(page_reader, "urlopen", return_value=_FakeResponse(_ARTICLE.encode())) as fake:
            first = reader.read("https://example.com/purring")
            second = reader.read("https://example.com/purring")
        self.assertEqual(first, second)
        self.assertEqual(fake.call_count, 1)

    def test_plus_encoded_wiki_path_is_retried_with_underscores(self) -> None:
        """Search engines hand back /wiki/Noise-cancelling+headphones, which 404s."""
        reader = PageReader()
        asked: list[str] = []

        def _fake(request: object, timeout: float = 0) -> _FakeResponse:
            target = getattr(request, "full_url", "")
            asked.append(target)
            if "+" in target:
                raise OSError("404")
            return _FakeResponse(_ARTICLE.encode())

        with patch.object(page_reader, "urlopen", side_effect=_fake):
            text = reader.read("https://en.wikipedia.org/wiki/Noise-cancelling+headphones")
        self.assertIn("Cats purr by vibrating", text)
        self.assertEqual(asked[0], "https://en.wikipedia.org/wiki/Noise-cancelling+headphones")
        self.assertEqual(asked[1], "https://en.wikipedia.org/wiki/Noise-cancelling_headphones")

    def test_declared_charset_is_honored(self) -> None:
        reader = PageReader()
        body = _ARTICLE.encode("cp1252")
        with patch.object(page_reader, "urlopen", return_value=_FakeResponse(body, "text/html; charset=cp1252")):
            self.assertIn("Cats purr", reader.read("https://example.com/purring"))


class ReadManyTests(unittest.IsolatedAsyncioTestCase):
    """Batch reading keeps only what it actually got, and respects its cap."""

    class _StubReader(PageReader):
        def __init__(self, pages: dict[str, str]) -> None:
            super().__init__()
            self.pages = pages
            self.seen: list[str] = []

        def read(self, url: str) -> str:
            self.seen.append(url)
            return self.pages.get(url, "")

    async def test_unreadable_pages_are_absent_from_the_result(self) -> None:
        reader = self._StubReader({"https://a.example/1": "text one"})
        result = await reader.read_many(["https://a.example/1", "https://b.example/2"])
        self.assertEqual(result, {"https://a.example/1": "text one"})

    async def test_duplicate_urls_are_read_once(self) -> None:
        reader = self._StubReader({"https://a.example/1": "text one"})
        await reader.read_many(["https://a.example/1", "https://a.example/1"])
        self.assertEqual(reader.seen, ["https://a.example/1"])

    async def test_max_pages_caps_the_batch(self) -> None:
        reader = self._StubReader({f"https://a.example/{i}": "text" for i in range(6)})
        reader.max_pages = 2
        result = await reader.read_many([f"https://a.example/{i}" for i in range(6)])
        self.assertEqual(len(result), 2)

    async def test_no_urls_means_no_work(self) -> None:
        reader = self._StubReader({})
        self.assertEqual(await reader.read_many([]), {})
        self.assertEqual(reader.seen, [])


if __name__ == "__main__":
    unittest.main()
