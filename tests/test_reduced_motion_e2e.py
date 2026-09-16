"""Playwright e2e spec: prefers-reduced-motion emulation collapses CSS motion.

Companion to the static contract tests in test_web_platform.py (the CSS media
block and the effects.js JS guards). The static pins prove the *rules*; this
spec proves the rules *take effect in a real browser*: it boots the app,
emulates ``prefers-reduced-motion: reduce`` via Playwright's CDP media
emulation, and asserts the computed animation/transition durations of live
elements are near-zero — contrast-proven by probing the same elements without
emulation, where they must still report their real durations.

It also asserts the JS effects guard fired under emulation: the background
canvas is never drawn (the 3D loop short-circuited before its first frame).

Skips cleanly when Playwright (the project's optional ``browser`` extra) or a
launchable Chromium channel (Edge/Chrome) is unavailable.
"""

from __future__ import annotations

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

# Returns parsed millisecond durations (worst/longest across a comma list),
# the page's reduced-motion media state, and whether bgCanvas was ever painted.
# A canvas that the effects.js guard short-circuited stays byte-identical to a
# freshly cleared canvas of the same size.
PROBE = """(() => {
  const maxMs = (el, prop) => {
    let worst = 0;
    for (const part of String(getComputedStyle(el)[prop]).split(",")) {
      const num = parseFloat(part);
      if (Number.isNaN(num)) continue;
      const ms = /ms/.test(part) ? num : num * 1000;
      if (ms > worst) worst = ms;
    }
    return worst;
  };
  const panel = document.getElementById("homePanel");
  const btn = document.getElementById("homeHealthButton");
  const input = document.getElementById("tokenInput");
  const src = document.getElementById("bgCanvas");
  const blank = src && src.width
    ? (() => {
        const ref = document.createElement("canvas");
        ref.width = src.width;
        ref.height = src.height;
        const ctx = ref.getContext("2d");
        ctx.clearRect(0, 0, ref.width, ref.height);
        return src.toDataURL() === ref.toDataURL();
      })()
    : null;
  return {
    ready: document.readyState === "complete",
    matchMedia: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    animMs: panel ? maxMs(panel, "animationDuration") : -1,
    btnTransMs: btn ? maxMs(btn, "transitionDuration") : -1,
    inputTransMs: input ? maxMs(input, "transitionDuration") : -1,
    canvasBlank: blank,
  };
})()"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ReducedMotionPlaywrightTests(unittest.TestCase):
    """Live computed-style check under emulated prefers-reduced-motion."""

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
        env = dict(__import__("os").environ, PYTHONPATH=str(ROOT / "src"))
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

    def _probe(self, reduce_motion: bool) -> dict:
        """Load the app and return the PROBE payload, emulating reduced motion
        when asked (Playwright's emulate_media drives CDP setEmulatedMedia)."""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel=self.channel, headless=True, args=["--disable-gpu"]
            )
            try:
                context = browser.new_context()
                page = context.new_page()
                if reduce_motion:
                    page.emulate_media(reduced_motion="reduce")
                page.goto(f"http://127.0.0.1:{self.port}/", wait_until="load")
                page.wait_for_function(
                    "document.readyState === 'complete' && !!document.querySelector('#homePanel')",
                    timeout=20000,
                )
                if not reduce_motion:
                    time.sleep(1.2)  # let the canvas loop paint frames
                return page.evaluate(PROBE)
            finally:
                browser.close()

    def test_live_os_toggle_stops_and_restarts_canvas_and_tilt(self) -> None:
        """The effects.js guard reacts to a MID-SESSION OS toggle, not just boot.

        Contrast proof in four phases over one page session:
          1. no emulation: canvas paints, tilt writes transforms,
          2. reduce ON (emulated): within a frame the canvas is blanked AND
             stays blank across a sample window (stopped, not just cleared)
             and a fresh mousemove no longer writes tilt transforms,
          3. reduce OFF again: the canvas loop restarts and tilt re-binds,
          4. the blank snapshots during phase 2 equal a cleared reference
             canvas byte-for-byte, so 'no visible change' is real.
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel=self.channel, headless=True, args=["--disable-gpu"]
            )
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{self.port}/", wait_until="load")
                page.wait_for_function(
                    "document.readyState === 'complete' && !!document.querySelector('#homePanel')",
                    timeout=20000,
                )
                time.sleep(1.2)  # let the canvas loop paint frames

                # Phase 1: running, painting, and tilt armed.
                running = page.evaluate(PROBE)
                self.assertIs(running["canvasBlank"], False,
                              "canvas should be animating before the toggle")
                tilt_before = self._tilt_transform_after_move(page)
                self.assertIn("perspective", tilt_before,
                              "tilt should write transforms before the toggle")

                # Phase 2: the OS toggle arrives MID-SESSION.
                page.emulate_media(reduced_motion="reduce")
                page.wait_for_function(
                    "window.matchMedia('(prefers-reduced-motion: reduce)').matches",
                    timeout=5000,
                )
                time.sleep(0.3)  # give the change listener a frame to react
                stopped_1 = page.evaluate(PROBE)
                self.assertIs(stopped_1["canvasBlank"], True,
                              "canvas must be blanked when reduced motion turns on")
                time.sleep(0.6)  # sample window: still blank == loop STOPPED
                stopped_2 = page.evaluate(PROBE)
                self.assertIs(stopped_2["canvasBlank"], True,
                              "canvas kept painting after the toggle — loop not stopped")
                tilt_off = self._tilt_transform_after_move(page)
                self.assertEqual(tilt_off, "",
                                 "tilt must not write transforms after the toggle")

                # Phase 3: toggle back off — motion must come back.
                # (Explicit no-preference: None does not reliably clear the
                # reduced-motion emulation in this Playwright/CDP version.)
                page.emulate_media(reduced_motion="no-preference")
                page.wait_for_function(
                    "!window.matchMedia('(prefers-reduced-motion: reduce)').matches",
                    timeout=5000,
                )
                time.sleep(0.5)  # loop restarts on the next animation frame
                restarted = page.evaluate(PROBE)
                self.assertIs(restarted["canvasBlank"], False,
                              "canvas must restart when reduced motion turns off")
                tilt_again = self._tilt_transform_after_move(page)
                self.assertIn("perspective", tilt_again,
                              "tilt must re-bind when reduced motion turns off")
            finally:
                browser.close()

    def _tilt_transform_after_move(self, page) -> str:
        """Move the mouse over a visible card and return its inline transform.

        Uses real mousemove events over the first visible .surface so the
        assertion observes the actual tilt handlers, not any internal flag.
        """
        page.evaluate(
            "document.querySelectorAll('.surface, .command-console, .info-card, .metric-card')"
            ".forEach(c => { c.style.transform = ''; })"
        )
        box = page.evaluate(
            """(() => {
              const cards = [...document.querySelectorAll(
                '.surface, .command-console, .info-card, .metric-card'
              )];
              const card = cards.find(c => {
                const r = c.getBoundingClientRect();
                return r.width > 40 && r.height > 40;
              });
              if (!card) return null;
              const r = card.getBoundingClientRect();
              return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
            })()"""
        )
        if box is None:
            self.fail("no visible card found for the tilt probe")
        page.mouse.move(box["x"], box["y"])
        page.mouse.move(box["x"] + 6, box["y"] + 4)  # second event: deltas update
        return page.evaluate(
            """(() => {
              const card = [...document.querySelectorAll(
                '.surface, .command-console, .info-card, .metric-card'
              )].find(c => (c.style.transform || '').length > 0);
              return card ? card.style.transform : "";
            })()"""
        )

    def test_reduced_motion_collapses_motion_on_live_elements(self) -> None:
        reduce = self._probe(reduce_motion=True)
        default = self._probe(reduce_motion=False)

        # The CDP emulation reached the page: matchMedia agrees with the context.
        self.assertIs(reduce["ready"], True)
        self.assertIs(reduce["matchMedia"], True)
        self.assertIs(default["matchMedia"], False)

        # Contrast proof: without emulation these elements genuinely animate,
        # so the near-zero numbers below are caused by the media emulation,
        # not by elements that never had motion.
        for key, real_ms in (("animMs", 300), ("btnTransMs", 200), ("inputTransMs", 200)):
            self.assertGreaterEqual(
                default[key], real_ms,
                f"{key} should report its real duration without emulation, got {default[key]}ms",
            )
        # The ask: near-zero computed durations under emulation (the CSS block
        # forces 0.01ms; anything under 1ms is the collapse).
        for key in ("animMs", "btnTransMs", "inputTransMs"):
            self.assertLessEqual(
                reduce[key], 1.0,
                f"{key} must be near zero under prefers-reduced-motion, got {reduce[key]}ms",
            )

        # JS side fired end-to-end: under emulation the effects.js guard ran
        # before the first frame, so bgCanvas was never painted; without
        # emulation the loop is live and the canvas is non-blank.
        self.assertIs(reduce["canvasBlank"], True,
                      "bgCanvas should be untouched under reduced motion")
        self.assertIs(default["canvasBlank"], False,
                      "bgCanvas should animate without emulation")


if __name__ == "__main__":
    unittest.main()
