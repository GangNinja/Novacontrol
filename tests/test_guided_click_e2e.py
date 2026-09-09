"""Playwright e2e spec: a real guided click renders its result card honestly.

Companion to the static/DOM tests (test_web_platform.py, test_buglog_vision.py):
those pin the rules; this spec proves them in a real browser over real HTTP —

  1. an open verification bug is seeded for the flow (written directly through
     the real BugLog, since bugs have no create endpoint),
  2. the Vision panel's Locate & Click is driven like a user would,
  3. the result card renders the action pill ("Completed"), the located-by
     pill, the Verified/Unverified verification pill, and the click
     coordinates,
  4. a VERIFIED click auto-resolves the open bug — the bug log shows it
     fixed with the auto-resolve evidence and the card says how many,
  5. an UNVERIFIED click (identical frames) reports honestly and records a
     new open bug.

The server under test is the real create_app() stack with the desktop runner
swapped for ScriptedVisionRunner (tests/_guided_click_app.py), which executes
the click as two synthetic frames — so the pixel-diff verification path is
exercised for real, deterministically, without moving a cursor.
NOVA_VISION_FRAME_MODE flips the same flow to its failure branch.

Skips cleanly when Playwright or a launchable Chromium channel is unavailable.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - exercised when playwright is not installed
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent

# The real bug-log module drives the seed, so the file format is never
# duplicated by hand in this spec.
from novacontrol.core.buglog import BugLog  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GuidedClickPlaywrightBase(unittest.TestCase):
    """Boots the scripted-runner server; subclasses hold the assertions."""

    frame_mode = "changed"

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

        # Disposable app data dir + seeded open bug for THIS flow and label.
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.bug_log = BugLog(self.data_dir / "bugs.json")
        self.bug_log.record(
            "Vision click 'Library' could not be verified automatically.",
            where="vision: guided_click",
            details={"label": "Library"},
        )

        self.port = _free_port()
        env = dict(
            os.environ,
            PYTHONPATH=str(ROOT / "src"),
            NOVA_DATA_DIR=str(self.data_dir),
            NOVA_VISION_FRAME_MODE=self.frame_mode,
        )
        # cwd=ROOT so the click's screenshot frames land in the checkout (the
        # runner writes relative paths) — same as production behavior.
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests._guided_click_app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=flags,
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
        if self.server and self.server.stderr:
            self.server.stderr.close()
        self.tmp.cleanup()
        for frame in ("vision_locate.png", "vision_after.png"):
            try:
                (ROOT / frame).unlink()
            except OSError:
                pass

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
        stderr = b""
        try:
            if self.server.stderr:
                stderr = self.server.stderr.read() or b""
        except OSError:
            pass
        self.fail(f"test server did not become healthy: {stderr[-500:]!r}")

    def _get_json(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _drive_guided_click(self) -> tuple[dict, list[str], str]:
        """Load the app, run a guided click via the Vision panel, return
        (card text, pill labels, raw output-container HTML)."""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=self.channel, headless=True, args=["--disable-gpu"])
            try:
                page = browser.new_context().new_page()
                page.goto(f"http://127.0.0.1:{self.port}/", wait_until="load")
                page.wait_for_function(
                    "document.readyState === 'complete' && !!document.querySelector('[data-panel=\"visionPanel\"]')",
                    timeout=20000,
                )
                page.click('[data-panel="visionPanel"]')
                page.wait_for_selector("#visionClickInput", state="visible", timeout=5000)
                page.fill("#visionClickInput", "Library")
                page.click("#visionClickButton")
                # The click runs locate -> click -> verify over HTTP; wait out
                # the card (success or the shared error card, both visible).
                page.wait_for_function(
                    "() => { const el = document.getElementById('visionDescribeOutput');"
                    " return el && !el.classList.contains('empty-state')"
                    " && el.querySelector('.vision-result-card, .error-card'); }",
                    timeout=30000,
                )
                return (
                    page.inner_text("#visionDescribeOutput"),
                    [p.strip() for p in page.eval_on_selector_all(
                        "#visionDescribeOutput .vision-result-card .pill",
                        "els => els.map(e => e.textContent)",
                    )],
                    page.inner_html("#visionDescribeOutput"),
                )
            finally:
                browser.close()


class GuidedClickVerifiedTests(GuidedClickPlaywrightBase):
    """Identical-frame contrast: verification SAW the screen change."""

    frame_mode = "changed"

    def test_verified_click_renders_card_and_resolves_open_bug(self) -> None:
        text, pills, _html = self._drive_guided_click()
        # The output container applies text-transform: uppercase; compare
        # case-insensitively so the assertions target content, not CSS.
        text = text.lower()

        # The result card rendered as a card with the real payload, not the
        # generic JSON dump.
        self.assertIn("Guided click: Library".lower(), text)
        self.assertIn("Completed", pills)
        self.assertIn("Verified", pills)
        self.assertIn("located by ocr", pills)
        self.assertIn("Clicked at (150, 80)".lower(), text)
        self.assertIn("Screen changed by".lower(), text)
        # The seeded open bug was auto-resolved by this verified click and the
        # card says so.
        self.assertIn("Auto-resolved 1 open bug".lower(), text)

        bugs = self._get_json("/bugs")["bugs"]
        seeded = next(b for b in bugs if "could not be verified" in b["what"])
        self.assertEqual(seeded["status"], "fixed")
        proof = seeded["details"].get("auto_resolved") or {}
        self.assertIsNotNone(proof.get("resolved_at"), "auto-resolve must stamp when")
        self.assertIsNotNone(proof.get("changed_pct"), "auto-resolve must carry the evidence")


class GuidedClickUnverifiedTests(GuidedClickPlaywrightBase):
    """Same flow with identical frames: the card must report Unverified."""

    frame_mode = "identical"

    def test_unverified_click_reports_honestly_and_records_bug(self) -> None:
        text, pills, _html = self._drive_guided_click()
        text = text.lower()

        self.assertIn("Guided click: Library".lower(), text)
        self.assertIn("Completed", pills, "the click itself still succeeded")
        self.assertIn("Unverified: no visible change", pills)
        self.assertNotIn("Verified", pills)
        self.assertNotIn("Auto-resolved", text)

        # The honest-failure branch recorded a NEW open bug for the flow.
        bugs = self._get_json("/bugs")["bugs"]
        new_bug = next(
            b for b in bugs
            if b["status"] == "open" and "no visible change" in b["what"]
        )
        self.assertEqual(new_bug["details"].get("label"), "Library")


if __name__ == "__main__":
    unittest.main()
