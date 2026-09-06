"""Accessibility contract for the web UI's keyboard focus ring.

Two complementary surfaces:

* ``FocusRingCssContractTests`` (always runs, fast): pins the CSS ring policy —
  ``--focus-ring`` resolves to a 2px solid outline, every interactive class
  authored in index.html / the JS renderers is covered by a focus rule that
  declares an outline, and no base ``outline: none`` escapes without a
  ``:focus-visible`` restoration.

* ``FocusRingBrowserWalkTests`` (skipped when no Chromium browser and node are
  available): drives a headless Edge/Chrome over CDP with trusted Tab key
  events — real keyboard modality — walking every focusable element in every
  panel and asserting ``:focus-visible`` matches with a 2px solid outline on
  all of them.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "novacontrol" / "web" / "static"
WALKER = Path(__file__).resolve().parent / "focus_walk.js"

INTERACTIVE_TAGS = ("button", "a", "input", "select", "textarea")


def _read_static() -> tuple[str, str, str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js_files = [STATIC / "app.js"]
    js_dir = STATIC / "js"
    if js_dir.is_dir():
        js_files.extend(sorted(js_dir.glob("*.js")))
    js = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
    return html, css, js


def _interactive_base_classes(html: str, js: str) -> set[str]:
    """First (base) class of every interactive element, from HTML and the JS renderers.

    Modifier classes (``primary``, ``small``, ``active``) ride on their base class:
    an element with ``class="cyber-btn primary"`` is covered by
    ``.cyber-btn:focus-visible``, so coverage is judged per base class.
    """
    bases: set[str] = set()
    tags = "|".join(INTERACTIVE_TAGS)
    for m in re.finditer(rf"<({tags})\b[^>]*\bclass=\"([^\"]+)\"", html):
        bases.add(m.group(2).split()[0])
    for m in re.finditer(rf'el\("({tags})"\s*,\s*"([^"]+)"', js):
        if m.group(2):
            bases.add(m.group(2).split()[0])
    return bases


def _focus_rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) pairs; comments removed so selectors stay exact, and focus
    rules live at top level, so a flat scan is exact for them."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


class FocusRingCssContractTests(unittest.TestCase):
    """Fast, always-on pins for the ring policy in the committed CSS."""

    def setUp(self) -> None:
        self.html, self.css, self.js = _read_static()

    def test_focus_ring_token_is_2px_solid(self) -> None:
        m = re.search(r"--focus-ring:\s*([^;]+);", self.css)
        self.assertIsNotNone(m, "Missing --focus-ring token")
        ring = m.group(1).strip()
        self.assertIn("2px", ring)
        self.assertIn("solid", ring)
        self.assertNotIn("none", ring)
        color = re.search(r"var\((--[\w-]+)\)", ring)
        if color:
            cm = re.search(re.escape(color.group(1)) + r":\s*([^;]+);", self.css)
            self.assertIsNotNone(cm, f"Missing {color.group(1)} token")
            self.assertNotIn("none", cm.group(1))

    def test_every_focus_visible_rule_declares_a_ring(self) -> None:
        rules = [r for r in _focus_rules(self.css) if ":focus-visible" in r[0]]
        self.assertTrue(rules, "no :focus-visible rules found")
        for selector, body in rules:
            self.assertIn("outline", body, f"{selector.strip()}: focus rule is missing an outline")
            self.assertNotIn("outline: none", body)

    def test_every_interactive_class_has_a_focus_ring_rule(self) -> None:
        rules = [(s, b) for s, b in _focus_rules(self.css) if ":focus" in s]
        for cls in sorted(_interactive_base_classes(self.html, self.js)):
            covered = any(
                re.search(rf"(?:^|[^\w-])\.{re.escape(cls)}(?![\w-])", sel)
                and "outline" in body and "outline: none" not in body
                for sel, body in rules
            )
            self.assertTrue(
                covered,
                f"interactive class .{cls} has no focus rule declaring an outline",
            )

    def test_no_base_outline_none_without_focus_restoration(self) -> None:
        for selector, body in _focus_rules(self.css):
            if "outline: none" not in body:
                continue
            for base in (s.strip() for s in selector.split(",")):
                if not base:
                    continue
                restored = any(
                    ":focus" in sel2 and base in sel2
                    and "outline" in body2 and "outline: none" not in body2
                    for sel2, body2 in _focus_rules(self.css)
                )
                self.assertTrue(
                    restored,
                    f"{base} sets outline: none but has no :focus rule restoring a ring",
                )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FocusRingBrowserWalkTests(unittest.TestCase):
    """Live keyboard walk: real Tab key events, real :focus-visible state."""

    def setUp(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node not available for the CDP walker")
        self.browser = None
        for candidate in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            shutil.which("chromium") or shutil.which("google-chrome") or "",
        ):
            if candidate and Path(candidate).exists():
                self.browser = candidate
                break
        if self.browser is None:
            self.skipTest("no headless Chromium browser found")

        self.app_port = _free_port()
        self.dbg_port = _free_port()
        self.tmpdir = tempfile.mkdtemp(prefix="nc-focus-")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        env = dict(__import__("os").environ, PYTHONPATH=str(ROOT / "src"))
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "novacontrol.api.app:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.app_port)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        )
        self.addCleanup(self._cleanup)
        self._wait_healthy()
        self.browser_proc = subprocess.Popen(
            [self.browser, "--headless=new", "--disable-gpu", "--no-first-run",
             f"--remote-debugging-port={self.dbg_port}",
             f"--user-data-dir={self.tmpdir}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
        )
        self._wait_debugger()

    def _cleanup(self) -> None:
        for proc in (getattr(self, "browser_proc", None), getattr(self, "server", None)):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def _wait_healthy(self) -> None:
        url = f"http://127.0.0.1:{self.app_port}/health"
        for _ in range(80):
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.25)
        self.fail("test server did not become healthy")

    def _wait_debugger(self) -> None:
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.dbg_port}/json/list", timeout=1
                ) as resp:
                    targets = json.loads(resp.read().decode())
                page = next(t for t in targets if t.get("type") == "page")
                self.ws_url = page["webSocketDebuggerUrl"]
                return
            except (OSError, StopIteration, KeyError, ValueError):
                time.sleep(0.25)
        self.fail("headless browser debugging endpoint never came up")

    def test_tab_walk_shows_2px_solid_focus_ring_everywhere(self) -> None:
        result = subprocess.run(
            ["node", str(WALKER), self.ws_url, f"http://127.0.0.1:{self.app_port}/"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode not in (0, 1):
            self.fail(f"walker crashed: {result.stderr.strip()[:500]}")
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreater(report["total"], 0, "walk visited no focusable elements")
        self.assertTrue(
            report["ok"],
            f"{len(report['failures'])} element(s) lack the 2px solid focus ring: "
            + json.dumps(report["failures"][:10], indent=2),
        )


if __name__ == "__main__":
    unittest.main()