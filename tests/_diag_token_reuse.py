"""Diagnose the second-run token-reuse flow with the OS shell present."""
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


port = _free_port()
tmpdir = tempfile.mkdtemp(prefix="nc-diag-")
import os
env = dict(os.environ)
env["PYTHONPATH"] = str(ROOT / "src")
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "novacontrol.api.app:create_app", "--factory",
     "--host", "127.0.0.1", "--port", str(port)],
    cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
        break
    except Exception:
        time.sleep(0.5)

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])
        page = browser.new_context().new_page()
        page.on("console", lambda m: print("CONSOLE:", m.type, m.text[:160]))
        page.on("pageerror", lambda e: print("PAGEERROR:", str(e)[:200]))
        page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page.wait_for_function(
            "document.readyState === 'complete' && !!document.querySelector('.nav-item')"
            " && !document.getElementById('statusText').textContent.includes('Initializing')",
            timeout=20000,
        )
        page.evaluate("""(() => {
          window.__planExecuteLog = [];
          const orig = window.fetch;
          window.fetch = function(...args) {
            const url = String(args[0] || "");
            if (/\\/(browser|command)\\/(plan|execute)/.test(url)) {
              window.__planExecuteLog.push(url.replace(location.origin, ""));
            }
            return orig.apply(this, args);
          };
        })()""")
        page.click('[data-panel="jarvisPanel"]')
        page.click("#jarvisDeviceBrowser")
        page.click('[data-jarvis-browser="search the web for quantum computing"]')
        page.wait_for_function("!!document.querySelector('#jarvisOutput button')", timeout=20000)
        page.click("#jarvisRunButton")
        page.wait_for_function(
            "document.getElementById('jarvisOutput').textContent.includes('Approved & Executed')",
            timeout=45000,
        )
        print("AFTER RUN 1:", page.evaluate("window.__planExecuteLog"))
        page.evaluate("""(() => {
          window.__clicks = [];
          const b = document.getElementById('jarvisRunButton');
          const tag = (e) => {
            const t = e.target.id || e.target.className || e.target.tagName;
            const r = b.getBoundingClientRect();
            window.__clicks.push(`${e.type}@${Math.round(e.clientX)},${Math.round(e.clientY)} -> ${t} btnTop=${Math.round(r.top)} wsScroll=${document.getElementById('mainContent').scrollTop} winScroll=${window.scrollY}`);
          };
          ['pointerdown','pointerup','mousedown','mouseup','click'].forEach(t =>
            document.addEventListener(t, tag, true));
        })()""")
        page.click("#jarvisRunButton")
        page.wait_for_timeout(1500)
        print("RUN2 events:")
        for line in page.evaluate("window.__clicks"):
            print("   ", line)
        print("RUN2 hits:", page.evaluate("""(() => {
          const b = document.getElementById('jarvisRunButton');
          const r = b.getBoundingClientRect();
          const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
          const el = document.elementFromPoint(cx, cy);
          const cs = getComputedStyle(b);
          return { btnRect: { t: Math.round(r.top), l: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height) },
                   hit: el ? (el.id || el.className || el.tagName) : null,
                   transform: cs.transform, willChange: cs.willChange, pointerEvents: cs.pointerEvents,
                   wsScroll: document.getElementById('mainContent').scrollTop,
                   devPixelRatio: window.devicePixelRatio };
        })()"""))
        # Programmatic click as a cross-check
        page.evaluate("document.getElementById('jarvisRunButton').click()")
        page.wait_for_timeout(2500)
        print("AFTER JS CLICK:", page.evaluate("window.__planExecuteLog"))
        try:
            page.wait_for_function(
                """(() => {
                  const log = window.__planExecuteLog;
                  return log.filter(u => u.endsWith('/plan')).length === 2
                    && log.filter(u => u.endsWith('/execute')).length === 2;
                })()""",
                timeout=30000,
            )
            print("AFTER RUN 2:", page.evaluate("window.__planExecuteLog"))
            print("RESULT: PASS")
        except Exception:
            print("AFTER RUN 2 (timeout):", page.evaluate("window.__planExecuteLog"))
            print("OUTPUT TAIL:", page.inner_text("#jarvisOutput")[:300])
            print("RUN BTN CLASSES:", page.get_attribute("#jarvisRunButton", "class"))
            print("RESULT: FAIL")
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)
