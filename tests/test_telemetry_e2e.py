"""Playwright e2e spec: the Command Center telemetry is live, real and cheap.

The unit suite (`test_telemetry.py`) pins the collector's honesty contract and
that serving a poll never spawns a probe process. This spec proves the *client*
half in a real browser against a real server:

  * the section renders one card per metric with a real value, a bar whose width
    matches the reported percentage, and the NOVA CORE ribbon read-out;
  * a metric this machine cannot measure renders "Unavailable" with the reason
    from the payload — never a zero;
  * polling runs on its ~2s cadence while the tab is visible and **stops while
    it is hidden** (the efficiency contract: a background tab must not keep
    reading the machine), then resumes immediately on return;
  * the NOVA CORE indicator opens the detail read-out on the Command tab.

Skips cleanly when Playwright (the project's optional ``browser`` extra) or a
launchable Chromium channel (Edge/Chrome) is unavailable.
"""

from __future__ import annotations

import json
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

# Counts every telemetry poll the page issues, so the test measures the real
# loop rather than trusting an interval constant.
COUNT_POLLS = """() => {
  if (!window.__pollProbe) {
    window.__pollProbe = { count: 0 };
    const real = window.fetch;
    window.fetch = (...args) => {
      if (String(args[0]).includes('/system/telemetry')) window.__pollProbe.count += 1;
      return real(...args);
    };
  }
  return window.__pollProbe.count;
}"""

SET_VISIBILITY = """(hidden) => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  Object.defineProperty(document, 'visibilityState', {
    configurable: true, get: () => (hidden ? 'hidden' : 'visible'),
  });
  document.dispatchEvent(new Event('visibilitychange'));
}"""

READ_BOARD = """() => {
  const cards = [...document.querySelectorAll('#telemetryGrid .telemetry-card')];
  return {
    sectionVisible: !!document.getElementById('telemetrySection')?.offsetParent,
    metrics: cards.map(c => c.dataset.metric),
    values: Object.fromEntries(cards.map(c => [
      c.dataset.metric, c.querySelector('.telemetry-value').textContent,
    ])),
    bars: Object.fromEntries(cards.map(c => [
      c.dataset.metric, c.querySelector('.telemetry-fill').style.width,
    ])),
    available: Object.fromEntries(cards.map(c => [c.dataset.metric, c.dataset.available])),
    notes: Object.fromEntries(cards.map(c => [
      c.dataset.metric, c.querySelector('.telemetry-note').textContent,
    ])),
    core: document.getElementById('coreReadout').textContent,
    detailsHidden: document.getElementById('telemetryDetails').hidden,
  };
}"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TelemetryPlaywrightTests(unittest.TestCase):
    """Live browser checks for the Command Center telemetry board."""

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
        # The sampler is left ON here (unlike the unit suite): this spec is about
        # the real end-to-end board, including the sampled metrics.
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        env.pop("NOVACONTROL_DISABLE_TELEMETRY_SAMPLER", None)
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

    def _payload(self) -> dict:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/system/telemetry", timeout=30
        ) as resp:
            return json.load(resp)

    def _open_page(self, playwright):
        browser = playwright.chromium.launch(
            channel=self.channel, headless=True, args=["--disable-gpu"]
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{self.port}/", wait_until="load")
        page.wait_for_function(
            "document.readyState === 'complete' && !!document.getElementById('telemetryGrid')",
            timeout=20000,
        )
        page.wait_for_function(
            "document.querySelectorAll('#telemetryGrid .telemetry-card').length >= 8",
            timeout=20000,
        )
        # The GPU/network/temperature probe costs seconds, so wait for the
        # sampler's first result before reading the board: this spec asserts the
        # complete live board, not the sampling-in-progress state.
        deadline = time.time() + 40
        while time.time() < deadline:
            if self._payload()["hardware"]["sampler"]["expensive_sampled"]:
                break
            time.sleep(1.0)
        else:
            self.fail("the telemetry sampler never produced a sample")
        return browser, page

    def test_board_renders_real_values_and_honest_absences(self) -> None:
        """Every card shows a measured value or a reason — and matches the API."""
        payload = self._payload()
        hardware = payload["hardware"]
        with sync_playwright() as playwright:
            browser, page = self._open_page(playwright)
            try:
                time.sleep(2.5)  # let a couple of polls land
                board = page.evaluate(READ_BOARD)
                api = page.evaluate("async () => (await (await fetch('/system/telemetry')).json())")
            finally:
                browser.close()

        self.assertTrue(board["sectionVisible"], "the telemetry section must be rendered")
        for metric in ("cpu", "memory", "storage", "gpu", "network", "battery", "temperature", "uptime"):
            self.assertIn(metric, board["metrics"], f"missing {metric} card")

        # The board is the API's numbers, not decoration.
        self.assertEqual(board["available"]["memory"], str(api["hardware"]["memory"]["available"]).lower())
        if api["hardware"]["cpu"]["available"]:
            self.assertRegex(board["values"]["cpu"], r"^\d+%$")
        if api["hardware"]["memory"]["available"]:
            self.assertIn("/", board["values"]["memory"])
        if api["hardware"]["host"].get("uptime_seconds"):
            self.assertRegex(board["values"]["uptime"], r"\d+[dhm]")
        self.assertRegex(board["values"]["battery"], r"^\d+%$|^Unavailable$")

        # A bar width is the reported percentage, and an unmeasurable metric has
        # no bar at all — plus a reason, never a zero.
        if api["hardware"]["memory"]["available"]:
            percent = api["hardware"]["memory"]["percent"]
            self.assertAlmostEqual(
                float(board["bars"]["memory"].rstrip("%")), percent, delta=1.0,
                msg="memory bar must track the measured percentage",
            )
        if not api["hardware"]["temperature"]["available"]:
            self.assertEqual(board["values"]["temperature"], "Unavailable")
            self.assertEqual(board["bars"]["temperature"], "0%")
            self.assertIn(
                api["hardware"]["temperature"]["reason"][:30],
                board["notes"]["temperature"],
                "the card must show the reason the metric is missing",
            )
        # The NOVA CORE read-out quotes the same CPU/RAM the card shows.
        self.assertRegex(board["core"], r"CPU \d+% · RAM \d+% · \d+ TASKS")

    def test_polling_pauses_while_hidden_and_resumes(self) -> None:
        """The loop is ~2s while visible, silent while hidden, live on return."""
        with sync_playwright() as playwright:
            browser, page = self._open_page(playwright)
            try:
                page.evaluate(COUNT_POLLS)

                page.evaluate("() => { window.__pollProbe.count = 0; }")
                time.sleep(5)
                visible = page.evaluate("() => window.__pollProbe.count")
                self.assertGreaterEqual(visible, 2, f"expected ~2s polling, saw {visible} in 5s")
                self.assertLessEqual(visible, 4, f"polling faster than 2s: {visible} in 5s")

                page.evaluate(SET_VISIBILITY, True)
                page.evaluate("() => { window.__pollProbe.count = 0; }")
                time.sleep(6)
                hidden = page.evaluate("() => window.__pollProbe.count")
                self.assertEqual(hidden, 0, f"a hidden tab must not poll, saw {hidden} in 6s")

                page.evaluate(SET_VISIBILITY, False)
                page.evaluate("() => { window.__pollProbe.count = 0; }")
                time.sleep(6)
                resumed = page.evaluate("() => window.__pollProbe.count")
                self.assertGreaterEqual(resumed, 2, f"polling did not resume, saw {resumed}")
            finally:
                browser.close()

    def test_nova_core_indicator_opens_the_detail_readout(self) -> None:
        """The header indicator jumps to the Command tab and expands the details."""
        with sync_playwright() as playwright:
            browser, page = self._open_page(playwright)
            try:
                # Start somewhere else so the click has to navigate.
                page.click('.nav-item[data-panel="systemPanel"]')
                page.wait_for_function(
                    "document.getElementById('systemPanel').classList.contains('active')",
                    timeout=5000,
                )
                self.assertIs(page.evaluate("() => document.getElementById('telemetryDetails').hidden"), True)

                page.click("#coreIndicator")
                page.wait_for_function(
                    "document.getElementById('homePanel').classList.contains('active')",
                    timeout=5000,
                )
                page.wait_for_function(
                    "document.getElementById('telemetryDetails').hidden === false",
                    timeout=5000,
                )
                rows = page.evaluate(
                    """() => {
                      const d = document.getElementById('telemetryDetails');
                      return [...d.querySelectorAll('dt')].map(x => x.textContent);
                    }"""
                )
            finally:
                browser.close()

        for expected in ("Host", "Uptime", "GPU", "Network interfaces", "AI engine", "Vision", "Automation"):
            self.assertIn(expected, rows, f"detail read-out must report {expected}")


if __name__ == "__main__":
    unittest.main()
