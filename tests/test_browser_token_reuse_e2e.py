"""Playwright e2e spec: Approve And Run reuses the minted browser token.

Pins the persisted-token pattern on the browser device of the JARVIS panel:

  1. a chip/plan flow mints exactly ONE plan (POST /browser/plan) whose token
     is remembered by the frontend (rememberApproval via renderCommand),
  2. the composer's Approve And Run executes that stored token — the network
     log must show NO second /browser/plan before the /browser/execute,
  3. after the single-use token is consumed, a second Approve And Run falls
     back to exactly one re-plan followed by one execute (a stale or missing
     token can never dead-end, and it never double-plans).

The execution runs against the live search flow (Bing + Playwright extract),
so the spec also proves the executed answer renders. Skips cleanly when
Playwright or a launchable Chromium channel is unavailable.
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BrowserTokenReuseE2ETests(unittest.TestCase):
    """Token minted once by the plan; Approve And Run consumes it without re-planning."""

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

    def test_approve_and_run_reuses_token_without_replanning(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel=self.channel, headless=True, args=["--disable-gpu"]
            )
            try:
                page = browser.new_context().new_page()
                page.goto(f"http://127.0.0.1:{self.port}/", wait_until="load")
                page.wait_for_function(
                    "document.readyState === 'complete' && !!document.querySelector('.nav-item')"
                    " && !document.getElementById('statusText').textContent.includes('Initializing')",
                    timeout=20000,
                )
                # Count every plan/execute POST the page makes. Installed AFTER
                # load: a navigation would wipe any earlier instrumentation.
                page.evaluate(
                    """(() => {
                      window.__planExecuteLog = [];
                      const orig = window.fetch;
                      window.fetch = function(...args) {
                        const url = String(args[0] || "");
                        if (/\\/(browser|command)\\/(plan|execute)/.test(url)) {
                          window.__planExecuteLog.push(url.replace(location.origin, ""));
                        }
                        return orig.apply(this, args);
                      };
                    })()"""
                )

                # Open the JARVIS panel, switch to the Browser device, and
                # plan via the example chip.
                page.click('[data-panel="jarvisPanel"]')
                page.wait_for_selector("#jarvisDeviceBrowser", state="visible", timeout=10000)
                page.click("#jarvisDeviceBrowser")
                page.click('[data-jarvis-browser="search the web for quantum computing"]')
                page.wait_for_function(
                    """(() => {
                      const out = document.getElementById('jarvisOutput');
                      return out && !out.classList.contains('empty-state')
                        && out.textContent.includes('waiting') === false
                        && !!out.querySelector('button');
                    })()""",
                    timeout=15000,
                )
                log = page.evaluate("window.__planExecuteLog")
                self.assertEqual(
                    log, ["/browser/plan"],
                    f"planning must mint exactly one plan, got {log}",
                )

                # Composer Approve And Run: must execute the STORED token —
                # no /browser/plan request may precede the /browser/execute.
                page.click("#jarvisRunButton")
                page.wait_for_function(
                    "document.getElementById('jarvisOutput').textContent.includes('Approved & Executed')",
                    timeout=45000,
                )
                log = page.evaluate("window.__planExecuteLog")
                self.assertEqual(
                    log, ["/browser/plan", "/browser/execute"],
                    f"Approve And Run must reuse the stored token (no re-plan), got {log}",
                )
                # The executed search composed its answer.
                self.assertRegex(
                    page.inner_text("#jarvisOutput"), r"Top \d+ web results:"
                )

                # Second Approve And Run: the single-use token is consumed, so
                # exactly one re-plan + one execute must follow (fallback, not
                # a dead-end, and never a double-plan).
                page.click("#jarvisRunButton")
                page.wait_for_function(
                    """(() => {
                      const log = window.__planExecuteLog;
                      const planCount = log.filter(u => u.endsWith('/plan')).length;
                      const execCount = log.filter(u => u.endsWith('/execute')).length;
                      return planCount === 2 && execCount === 2;
                    })()""",
                    timeout=60000,
                )
                log = page.evaluate("window.__planExecuteLog")
                self.assertEqual(
                    log,
                    ["/browser/plan", "/browser/execute", "/browser/plan", "/browser/execute"],
                    f"second run must re-plan exactly once then execute, got {log}",
                )
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
