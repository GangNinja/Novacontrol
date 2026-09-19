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
    # Every stylesheet in static/ participates in the focus-ring contract so
    # an additional layer (e.g. nc-os.css) can never drop ring coverage.
    css = "\n".join(p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.css")))
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


def _class_exists_in(html: str, js: str, cls: str) -> bool:
    """Does `cls` name a class the frontend actually creates?

    Two creation paths are recognized: a class attribute in index.html, and a
    hyphen-bounded token anywhere in the JS corpus (covers whole-string tokens
    like "cyber-checkbox" AND modifier tokens inside el() base strings like
    "cyber-btn danger small task-delete"). Hyphen-bounded so `task-delete`
    cannot be satisfied by an unrelated `task-delete-x`.
    """
    token = rf"(?<![\w-]){re.escape(cls)}(?![\w-])"
    if re.search(rf'class="[^"]*{token}', html):
        return True
    return re.search(token, js) is not None


def _dead_focus_ring_classes(css: str, html: str, js: str) -> list[tuple[str, str]]:
    """(selector, class) pairs where a :focus-visible ring rule names a class
    that exists nowhere in index.html or the JS renderers — i.e. a dead rule.
    Only ring rules count (rules declaring an outline, none of them none).
    """
    dead: list[tuple[str, str]] = []
    for selector, body in _focus_rules(css):
        if ":focus-visible" not in selector:
            continue
        if "outline" not in body or "outline: none" in body:
            continue
        for cls in re.findall(r"\.([A-Za-z_][\w-]*)", selector):
            if not _class_exists_in(html, js, cls):
                dead.append((selector.strip(), cls))
    return dead


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


class FocusRingReverseContractTests(unittest.TestCase):
    """Reverse contract: a :focus-visible rule may not name a ghost class.

    The forward contract (every interactive class has a ring rule) stops
    missing styles; this one stops DEAD styles: when a component is deleted or
    renamed, its focus rule lingers in styles.css silently. The rule must name
    a class that actually exists in index.html or the JS renderers.
    """

    def setUp(self) -> None:
        self.html, self.css, self.js = _read_static()

    def test_every_focus_visible_class_exists_in_html_or_renderers(self) -> None:
        rules = [s for s, b in _focus_rules(self.css) if ":focus-visible" in s]
        self.assertTrue(rules, "no :focus-visible rules found to check")
        dead = _dead_focus_ring_classes(self.css, self.html, self.js)
        self.assertEqual(
            dead, [],
            "dead :focus-visible rule(s) — no element in index.html or the JS "
            "renderers ever carries these class(es): "
            + ", ".join(f".{cls} (from '{sel[:60]}…')" if len(sel) > 60 else f".{cls} (from '{sel}')"
                        for sel, cls in dead),
        )

    def test_reverse_contract_has_teeth(self) -> None:
        """Negative control: a fabricated class must be reported dead, a real
        class must resolve, and a lookalike prefix must not satisfy a token."""
        # A fabricated class does not exist in the live corpus.
        self.assertFalse(
            _class_exists_in(self.html, self.js, "focus-ring-ghost-nine"),
            "fabricated class unexpectedly resolved — the contract has no teeth",
        )
        # Sanity: real classes from both creation paths resolve.
        self.assertTrue(_class_exists_in(self.html, self.js, "cyber-btn"))
        self.assertTrue(_class_exists_in(self.html, self.js, "task-delete"))
        # Hyphen-boundary: `task-delete` must not be satisfied by `task-delete-x`.
        self.assertFalse(
            _class_exists_in('', 'el("button", "task-delete-x", "x")', "task-delete"),
        )
        # The collector reports exactly the ghost rule.
        css = (
            ".cyber-btn:focus-visible { outline: var(--focus-ring); }\n"
            ".focus-ring-ghost-nine:focus-visible { outline: 2px solid red; }\n"
            "/* outline-less rules are not ring rules and stay out of scope */\n"
            ".focus-ring-ghost-ten:focus-visible { color: red; }\n"
        )
        dead = _dead_focus_ring_classes(css, self.html, self.js)
        self.assertEqual([cls for _, cls in dead], ["focus-ring-ghost-nine"])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _resume_process_threads(pid: int) -> None:
    """Resume every thread of a CREATE_SUSPENDED process (Windows-only use)."""
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.ResumeThread.restype = wintypes.DWORD
    TH32CS_SNAPTHREAD, THREAD_SUSPEND_RESUME = 0x4, 0x0002
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == -1:
        return
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        has = k32.Thread32First(snap, ctypes.byref(entry))
        while has:
            if entry.th32OwnerProcessID == pid:
                handle = k32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if handle:
                    k32.ResumeThread(handle)
                    k32.CloseHandle(handle)
            has = k32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)


def _win_job_kill_on_close() -> int | None:
    """Windows: create a Job object whose processes die when the handle closes.

    Edge re-execs through a short-lived launcher process; terminating the
    launcher's pid orphans the real browser tree (observed as zombie headless
    Edge processes interfering with later runs). Assigning the launcher to a
    kill-on-close job pins the whole tree — children inherit the job — so
    closing the handle in cleanup is deterministic. Returns a raw HANDLE or
    None off-Windows / on any failure (best effort by contract).
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("Basic", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimits()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    # JobObjectExtendedLimitInformation == 9 (the basic class rejects the
    # extended-sized buffer this struct family exposes reliably).
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


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
        self.browser_err = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        browser_args = [self.browser, "--headless", "--disable-gpu", "--no-first-run",
                        f"--remote-debugging-port={self.dbg_port}",
                        f"--user-data-dir={self.tmpdir}", "about:blank"]
        if sys.platform != "win32":
            # CI Linux images: the Chrome sandbox can fail to initialize under
            # containers/root; both flags are standard headless-CI hygiene.
            browser_args[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
        self._job = _win_job_kill_on_close()  # None off-Windows
        is_windows = sys.platform == "win32"
        browser_creationflags = flags
        if is_windows:
            # CREATE_SUSPENDED: assign the launcher to the kill-on-close job
            # before it can re-exec the real browser process. POSIX rejects
            # any nonzero creationflags with ValueError, so this must stay
            # Windows-only (Linux CI broke here in run #6).
            browser_creationflags |= 0x4  # CREATE_SUSPENDED
        self.browser_proc = subprocess.Popen(
            browser_args,
            stdout=subprocess.DEVNULL, stderr=self.browser_err,
            creationflags=browser_creationflags,
        )
        if is_windows:
            assigned = False
            if self._job is not None:
                import ctypes
                from ctypes import wintypes

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.AssignProcessToJobObject.restype = wintypes.BOOL
                k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
                k32.OpenProcess.restype = wintypes.HANDLE
                k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                # PROCESS_SET_QUOTA | PROCESS_TERMINATE — the access the job API
                # documents for assignment.
                proc = k32.OpenProcess(0x0100 | 0x0001, False, self.browser_proc.pid)
                if proc:
                    assigned = bool(k32.AssignProcessToJobObject(self._job, proc))
                    k32.CloseHandle(proc)
            _resume_process_threads(self.browser_proc.pid)
            if not assigned:
                self._job = None  # best effort: cleanup falls back to terminate()
        self._wait_debugger()

    def _cleanup(self) -> None:
        for proc in (getattr(self, "browser_proc", None), getattr(self, "server", None)):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        # Closing the job handle kills the whole browser tree even when the
        # launcher already exited (kill-on-close); None = off-Windows/fallback.
        job = getattr(self, "_job", None)
        if job:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(job)
        err = getattr(self, "browser_err", None)
        if err is not None:
            try:
                err.close()
            except OSError:
                pass

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
        # The CDP port is the only truth. Some Edge builds re-exec through a
        # launcher process that exits code 0 within the first second while the
        # real browser (a separate tree member) keeps serving — so a launcher
        # exit is NOT fatal by itself. It only accelerates failure: if the
        # port stays dead for a grace period after the exit, the browser is
        # genuinely gone and we fail fast with its stderr. The connect_ex
        # pre-probe keeps refused-port iterations cheap on Windows.
        deadline = time.monotonic() + 30.0
        exited_at: float | None = None
        grace_after_exit = 4.0
        while time.monotonic() < deadline:
            if self.browser_proc.poll() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                elif time.monotonic() - exited_at > grace_after_exit:
                    self.fail(
                        f"headless browser exited (code {self.browser_proc.returncode}) "
                        f"and its debugging endpoint never came up: "
                        f"{self._browser_err_tail()}"
                    )
            probe = socket.socket()
            try:
                probe.settimeout(0.5)
                accepted = (
                    probe.connect_ex(("127.0.0.1", self.dbg_port)) == 0
                )
            finally:
                probe.close()
            if accepted:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.dbg_port}/json/list", timeout=1
                    ) as resp:
                        targets = json.loads(resp.read().decode())
                    page = next(t for t in targets if t.get("type") == "page")
                    self.ws_url = page["webSocketDebuggerUrl"]
                    return
                except (OSError, StopIteration, KeyError, ValueError):
                    pass
            time.sleep(0.25)
        self.fail(
            "headless browser debugging endpoint never came up: "
            + self._browser_err_tail()
        )
    def _browser_err_tail(self) -> str:
        try:
            self.browser_err.seek(0)
            return self.browser_err.read().strip()[-500:] or "<stderr empty>"
        except (OSError, ValueError):
            return "<stderr unavailable>"

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