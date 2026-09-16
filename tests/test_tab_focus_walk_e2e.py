"""Playwright e2e spec: a real Tab walk asserts computed focus rings live.

Companion to the other two focus surfaces — test_focus_rings.py pins the CSS
contract statically and walks via a hand-rolled CDP node script; this spec
proves the same rule through Playwright itself:

  1. boots the real app over HTTP (same recipe as the other e2e specs),
  2. presses trusted Tab keys (Playwright keyboard = CDP Input domain, genuine
     keyboard modality) until focus wraps back to the first element,
  3. after every hop asserts the focused element matches ``:focus-visible``
     and reports a computed 2px solid outline that is not transparent,
  4. repeats per panel by driving the existing nav buttons,
  5. negative control: injects ``outline: none !important`` mid-session and
     asserts the very same predicate flips to failing — proof the walk would
     catch a regression, not a vacuous pass.

Skips cleanly when Playwright (the project's optional ``browser`` extra) or a
launchable Chromium channel (Edge/Chrome) is unavailable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - exercised when playwright is not installed
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent

# Probe the currently focused element's computed outline state. Index into
# querySelectorAll("*") gives a stable identity across evaluate calls so the
# walk can detect wrap-around without DOM references surviving the boundary.
PROBE = """(() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    return { body: true };
  }
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    body: false,
    index: Array.prototype.indexOf.call(document.querySelectorAll("*"), el),
    tag: el.tagName,
    cls: typeof el.className === "string" ? el.className : "",
    text: (el.textContent || "").trim().slice(0, 40),
    visible: r.width > 0 && r.height > 0,
    fv: el.matches(":focus-visible"),
    outlineStyle: cs.outlineStyle,
    outlineWidth: cs.outlineWidth,
    outlineColor: cs.outlineColor,
  };
})()"""

MAX_HOPS = 400  # wrap detection ends the walk well before this; the cap is a hang guard


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TabFocusWalkPlaywrightTests(unittest.TestCase):
    """Trusted-Tab walk over every panel; computed outline on every hop."""

    def setUp(self) -> None:
        if sync_playwright is None:
            self.skipTest("playwright not installed (pip install -e .[browser])")
        self.channel = None
        for channel, exe in (
            ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ):
            if Path(exe).exists():
                self.channel = channel
                break
        if self.channel is None:
            self.skipTest("no Edge/Chrome available for the Playwright channel")

        self.port = _free_port()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "novacontrol.api.app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )
        self.addCleanup(self._cleanup)
        self._wait_healthy()

    def _cleanup(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()

    def _wait_healthy(self) -> None:
        url = f"http://127.0.0.1:{self.port}/health"
        for _ in range(80):
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.25)
        self.fail("test server did not become healthy")

    def _open_app(self, pw):
        browser = pw.chromium.launch(
            channel=self.channel, headless=True, args=["--disable-gpu"]
        )
        page = browser.new_context().new_page()
        page.goto(f"http://127.0.0.1:{self.port}/", wait_until="load")
        # Same readiness condition the CDP walker uses: app finished booting.
        page.wait_for_function(
            "document.readyState === 'complete' && !!document.querySelector('.nav-item')"
            " && !document.getElementById('statusText').textContent.includes('Initializing')",
            timeout=20000,
        )
        return browser, page

    @staticmethod
    def _probe_ok(el: dict) -> bool:
        """The predicate under test: focused element shows a visible ring."""
        return (
            bool(el["fv"])
            and el["outlineStyle"] == "solid"
            and el["outlineWidth"] == "2px"
            and "transparent" not in el["outlineColor"]
        )

    def _tab_walk(self, page: object) -> tuple[int, list[dict]]:
        """Press Tab until focus wraps to the first element; probe every hop."""
        page.evaluate(
            "document.activeElement && document.activeElement.blur && document.activeElement.blur()"
        )
        first_index = None
        stuck = 0
        total = 0
        failures: list[dict] = []
        for _hop in range(MAX_HOPS):
            page.keyboard.press("Tab")  # trusted key event: real keyboard modality
            el = page.evaluate(PROBE)
            if el.get("body"):
                # focus not moving yet (window focus race) or genuine wrap;
                # the stuck counter keeps a no-focus page from spinning forever
                stuck += 1
                if stuck > 8:
                    self.fail("Tab never moved focus: page has no input focus")
                continue
            stuck = 0
            if first_index is None:
                first_index = el["index"]
            elif el["index"] == first_index:
                break  # wrapped around to the first element — walk complete
            if not el["visible"]:
                continue  # Tab landed on a hidden control; nothing to assert
            total += 1
            if not self._probe_ok(el):
                failures.append(
                    {k: el[k] for k in ("tag", "cls", "text", "fv",
                                        "outlineStyle", "outlineWidth", "outlineColor")}
                )
        return total, failures

    def test_tab_walk_shows_2px_solid_outline_on_every_interactive_element(self) -> None:
        with sync_playwright() as pw:
            browser, page = self._open_app(pw)
            try:
                panels = page.eval_on_selector_all(
                    ".nav-item", "els => els.map(e => e.dataset.panel)"
                )
                self.assertGreater(len(panels), 0, "no nav panels found to walk")

                per_panel: dict[str, int] = {}
                all_failures: list[dict] = []
                for panel in panels:
                    # Drive the real nav like a user, then blur so Tab starts
                    # from the top of each panel's focusable set.
                    page.click(f'.nav-item[data-panel="{panel}"]')
                    total, failures = self._tab_walk(page)
                    per_panel[panel] = total
                    all_failures.extend(failures)

                grand_total = sum(per_panel.values())
                self.assertGreater(
                    grand_total, 10,
                    f"walk visited too few focusable elements: {per_panel}",
                )
                empty = [p for p, n in per_panel.items() if n == 0]
                self.assertEqual(
                    empty, [],
                    f"panels with zero focusable elements visited: {empty}",
                )
                self.assertEqual(
                    all_failures, [],
                    f"{len(all_failures)} focused element(s) lack the 2px solid"
                    f" :focus-visible ring:\n"
                    + "\n".join(str(f) for f in all_failures[:10]),
                )
            finally:
                browser.close()

    def test_focus_predicate_has_teeth(self) -> None:
        """Negative control: force outline:none and the same predicate must fail.

        Contrast proof that the walk's assertion detects a missing ring rather
        than passing vacuously on elements that never had one.
        """
        with sync_playwright() as pw:
            browser, page = self._open_app(pw)
            try:
                page.keyboard.press("Tab")
                el = page.evaluate(PROBE)
                self.assertFalse(el.get("body"), "Tab did not move focus")
                self.assertTrue(
                    self._probe_ok(el),
                    f"pre-injection element should pass: {el}",
                )

                # Mark the focused element so identity survives add_style_tag,
                # which inserts a <style> node and shifts querySelectorAll("*")
                # indices (the index-based identity the walker uses would lie).
                page.evaluate(
                    "document.activeElement.setAttribute('data-focus-walk-mark', '1')"
                )
                page.add_style_tag(
                    content="*, *::before, *::after { outline: none !important; }"
                )
                stripped = page.evaluate(PROBE)
                # Same element (still focused; a style tag never moves focus),
                # now ringless.
                self.assertTrue(
                    page.evaluate(
                        "document.activeElement.matches('[data-focus-walk-mark]')"
                    ),
                    "focus moved during style injection — identity check is invalid",
                )
                self.assertFalse(
                    self._probe_ok(stripped),
                    "predicate still passed with outline forced to none — it has no teeth",
                )
                self.assertNotEqual(stripped["outlineStyle"], "solid")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
