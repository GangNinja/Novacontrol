"""Search plan extracts top results: execution returns an answer, not a page load.

Pins the full chain added to the browser search flow:

  * plan shape — a web-search plan is navigate -> EXTRACT(mode=search_results),
    and a plain navigate gains no extract step,
  * unwrap — DuckDuckGo's /l/?uddg=<encoded> redirect links resolve to the
    real target URL; direct links pass through,
  * composition — the executor renders the extract payload into the plan
    summary (the answer the UI shows) via _summarize_search_results,
  * end to end — a REAL headless Chromium loads a local page with the DuckDuckGo
    HTML-endpoint markup (a.result__a / .result__snippet, uddg-wrapped hrefs)
    and the extract step returns actual titles/links/snippets, which the
    executed plan's summary then carries.

The end-to-end test skips cleanly when Playwright's bundled Chromium is absent.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from novacontrol.application import (
    NovaControlApplication,
    _summarize_search_results,
)
from novacontrol.browser import NoopBrowserRunner
from novacontrol.browser.controller import (
    PlaywrightBrowserRunner,
    _unwrap_result_url,
)

ROOT = Path(__file__).resolve().parent.parent

# DuckDuckGo lite-endpoint result markup, served from a local page so the
# browser test needs no network. Hrefs use the /l/?uddg= redirect form the
# endpoint really serves; links are a.result-link, snippets td.result-snippet.
DDG_PAGE = """<!doctype html>
<html><head><title>quantum computing at DuckDuckGo</title></head>
<body>
  <table>
  <tr><td><a rel="nofollow" class="result-link" href="/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FQuantum_computing&amp;rut=abc">Quantum computing - Wikipedia</a></td></tr>
  <tr><td class="result-snippet">Quantum computing is the use of quantum phenomena to perform computation.</td></tr>
  <tr><td><a rel="nofollow" class="result-link" href="https://example.com/direct-link">A direct link result</a></td></tr>
  <tr><td class="result-snippet">Direct links must pass through unwrapped.</td></tr>
  <tr><td><a rel="nofollow" class="result-link" href="/l/?uddg=https%3A%2F%2Fwww.ibm.com%2Ftopics%2Fquantum-computing&amp;rut=def">What is quantum computing? - IBM</a></td></tr>
  </table>
</body></html>
"""


# Bing result markup: li.b_algo rows, first a[href] = title link, p = snippet,
# /ck/a?u=a1<base64url> redirect hrefs (with Bing's HTML-encoded ampersand).
BING_PAGE = """<!doctype html>
<html><head><title>quantum computing - Bing</title></head>
<body>
  <li class="b_algo">
    <h2><a href="https://www.bing.com/ck/a?&amp;u=a1aHR0cHM6Ly9lbi53aWtpcGVkaWEub3JnL3dpa2kvUXVhbnR1bV9jb21wdXRpbmc&amp;ntb=1">Quantum computing - Wikipedia</a></h2>
    <p>Quantum computing is the use of quantum phenomena to perform computation.</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://www.ibm.com/topics/quantum-computing">What is quantum computing? - IBM</a></h2>
    <p>IBM's primer on qubits and quantum advantage.</p>
  </li>
</body></html>
"""


class UnwrapResultUrlTests(unittest.TestCase):
    """Search-engine redirect hrefs resolve to the real target."""

    def test_ddg_uddg_link_unwraps_to_target(self) -> None:
        self.assertEqual(
            _unwrap_result_url("/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FQuantum_computing&rut=abc"),
            "https://en.wikipedia.org/wiki/Quantum_computing",
        )

    def test_direct_links_pass_through(self) -> None:
        self.assertEqual(
            _unwrap_result_url("https://example.com/page"), "https://example.com/page"
        )
        self.assertEqual(_unwrap_result_url(""), "")


class SearchPlanShapeTests(unittest.IsolatedAsyncioTestCase):
    """The planned workflow and the answer composition."""

    async def test_search_plan_is_navigate_then_extract(self) -> None:
        app = NovaControlApplication()
        app.browser.runner = NoopBrowserRunner()
        plan = app.plan_browser_command("search the web for rust vs go")
        actions = plan["workflow"]["actions"]
        self.assertEqual([a["type"] for a in actions], ["navigate", "extract"])
        self.assertEqual(actions[1]["parameters"]["mode"], "search_results")
        self.assertEqual(actions[1]["parameters"]["limit"], 6)

    async def test_plain_navigation_gains_no_extract(self) -> None:
        app = NovaControlApplication()
        app.browser.runner = NoopBrowserRunner()
        plan = app.plan_browser_command("navigate to example.com")
        self.assertEqual(
            [a["type"] for a in plan["workflow"]["actions"]], ["navigate"]
        )

    def test_summary_composition_from_results_payload(self) -> None:
        executed = [
            {"output": {"search_results": [
                {"title": "Quantum computing - Wikipedia",
                 "url": "https://en.wikipedia.org/wiki/Quantum_computing",
                 "snippet": "…"},
                {"title": "What is quantum computing? - IBM",
                 "url": "https://www.ibm.com/topics/quantum-computing"},
            ]}},
        ]
        summary = _summarize_search_results(executed)
        self.assertIn("Top 2 web results:", summary)
        self.assertIn("1. Quantum computing - Wikipedia — https://en.wikipedia.org/wiki/Quantum_computing", summary)
        self.assertIn("2. What is quantum computing? - IBM", summary)

    def test_summary_empty_without_results_payload(self) -> None:
        # Plain navigation outputs and noop runs produce no fabricated answer.
        self.assertEqual(_summarize_search_results([{"output": {"url": "https://example.com"}}]), "")
        self.assertEqual(_summarize_search_results([{"output": {"would_run": {}}}]), "")
        self.assertEqual(_summarize_search_results([]), "")

    def test_summary_skips_result_rows_without_title_or_url(self) -> None:
        executed = [{"output": {"search_results": [
            {"title": "", "url": "", "snippet": "noise"},
            {"title": "Only title, no url"},
        ]}}]
        summary = _summarize_search_results(executed)
        self.assertIn("Top 1 web results:", summary)
        self.assertIn("Only title, no url", summary)


class SearchExtractionEndToEndTests(unittest.TestCase):
    """Real Chromium: DDG-format page -> extract -> composed answer."""

    def setUp(self) -> None:
        if not PlaywrightBrowserRunner.is_available():
            self.skipTest("playwright not installed")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                pw.chromium.launch(headless=True).close()
        except Exception:
            self.skipTest("playwright bundled Chromium not available")

    def _drive(self, page_html: str) -> dict:
        """Plan + execute a real-browser search against local page_html."""
        async def scenario() -> dict:
            import threading
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802 - stdlib API
                    payload = page_html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *args: object) -> None:
                    pass  # keep test output clean

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            application = NovaControlApplication()
            try:
                application.browser.runner = PlaywrightBrowserRunner(headless=True)
                # Route the whole flow at the local page: patch web_search_url
                # BEFORE planning so the token's deep-copied plan really targets
                # the local page (mutating the plan dict after minting would
                # NOT affect execution — the token store snapshots the plan).
                with mock.patch(
                    "novacontrol.application_helpers.web_search_url",
                    return_value=f"http://127.0.0.1:{port}/results",
                ):
                    plan = application.plan_browser_command(
                        "search the web for quantum computing"
                    )
                    executed = await application.execute_browser_command(
                        "search the web for quantum computing",
                        approval_token=plan["approval"]["token"],
                    )
                return executed
            finally:
                await application.browser.runner.close()
                server.shutdown()
                server.server_close()

        return asyncio.run(scenario())

    def test_extract_returns_titles_links_and_summary_composes(self) -> None:
        executed = self._drive(DDG_PAGE)
        self.assertEqual(executed["status"], "executed")
        extract_output = executed["execution_results"][1]["output"]
        results = extract_output["search_results"]
        self.assertEqual(extract_output["count"], 3)
        self.assertEqual(results[0]["title"], "Quantum computing - Wikipedia")
        self.assertEqual(
            results[0]["url"], "https://en.wikipedia.org/wiki/Quantum_computing",
            "uddg redirect href must unwrap to the real target",
        )
        self.assertEqual(results[1]["url"], "https://example.com/direct-link")
        self.assertEqual(results[1]["snippet"], "Direct links must pass through unwrapped.")
        # No snippet element in row 3 -> empty snippet, not a crash.
        self.assertEqual(results[2]["snippet"], "")

        # The executor composed the answer into the summary the UI shows.
        summary = executed["summary"]
        self.assertIn("Top 3 web results:", summary)
        self.assertIn("Quantum computing - Wikipedia — https://en.wikipedia.org/wiki/Quantum_computing", summary)
        self.assertNotIn("Executed", summary.splitlines()[0])

    def test_extract_supports_bing_result_markup(self) -> None:
        executed = self._drive(BING_PAGE)
        self.assertEqual(executed["status"], "executed")
        extract_output = executed["execution_results"][1]["output"]
        results = extract_output["search_results"]
        self.assertEqual(extract_output["count"], 2)
        self.assertEqual(results[0]["title"], "Quantum computing - Wikipedia")
        self.assertEqual(
            results[0]["url"], "https://en.wikipedia.org/wiki/Quantum_computing",
            "Bing /ck/a?u=a1... redirect must unwrap to the real target",
        )
        self.assertEqual(
            results[0]["snippet"], "Quantum computing is the use of quantum phenomena to perform computation."
        )
        self.assertEqual(results[1]["url"], "https://www.ibm.com/topics/quantum-computing")
        self.assertIn("Top 2 web results:", executed["summary"])


if __name__ == "__main__":
    unittest.main()
