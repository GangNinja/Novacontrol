"""UI audit: responsive fit + stylesheet A/B.

Three checks that catch the failures a screenshot review misses:

1. **Fit** — walks every sidebar panel at every breakpoint and measures
   horizontal overflow (document scrollWidth plus any element whose box sticks
   out past the right edge). A panel that needs sideways scrolling is the
   "doesn't fit the screen" bug.

1c. **Navigation is present and never an overlay** — the sidebar must have a
   width at every audit width and must not sit fixed over the workspace. The
   off-canvas drawer failed this: at 700px the nav covered the panel, and a
   whole-panel click looked like clipped content.

1b. **One panel at a time, filling the screen** — the same walk also checks that
   exactly one panel is laid out (an id-selector `display` rule once beat
   `.panel { display: none }`, so the Command Center stayed rendered under
   whatever panel was open and pushed the chosen one ~1100px below the fold),
   that the clicked panel starts inside the viewport, and that it fills the
   workspace instead of leaving a dead strip down the right edge.

2. **Stylesheet A/B** (``--baseline``) — hashes the computed style of every
   element over ~50 properties, swaps the page's stylesheet for a baseline copy
   (served from a throwaway local server, so the file can live anywhere), and
   re-hashes. Zero differing elements means the stylesheet edit changed nothing
   that actually reaches the DOM — the check to run before deleting legacy CSS
   generations.

Requires the browser extra (``pip install -e ".[browser]" && python -m playwright install chromium``)
and a running app:

    python scripts/ui_audit.py --url http://127.0.0.1:8001/
    python scripts/ui_audit.py --url http://127.0.0.1:8001/ --baseline old.css

Exit code is 0 when every panel fits and (with a baseline) nothing drifted.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

WIDTHS: tuple[int, ...] = (1920, 1600, 1366, 1280, 1024, 900, 820, 700, 600, 480, 390)
PANELS: tuple[str, ...] = (
    "Command", "Chat", "Explore", "Routing", "J.A.R.V.I.S", "Vision",
    "Build", "Learn", "System", "Settings", "CLI",
)

# Properties that make up a component's rendered identity: box model, type,
# paint, layout, and effects.
PROPS: tuple[str, ...] = (
    "display", "position", "top", "right", "bottom", "left", "width", "height",
    "minWidth", "minHeight", "maxWidth", "maxHeight", "marginTop", "marginRight",
    "marginBottom", "marginLeft", "paddingTop", "paddingRight", "paddingBottom",
    "paddingLeft", "fontSize", "fontWeight", "fontFamily", "lineHeight",
    "letterSpacing", "textTransform", "color", "backgroundColor", "backgroundImage",
    "borderTopWidth", "borderTopStyle", "borderTopColor", "borderRightWidth",
    "borderRightStyle", "borderBottomWidth", "borderBottomStyle", "borderLeftWidth",
    "borderLeftStyle", "borderTopLeftRadius", "borderTopRightRadius",
    "borderBottomRightRadius", "borderBottomLeftRadius", "boxShadow", "opacity",
    "transform", "flexDirection", "flexWrap", "justifyContent", "alignItems", "gap",
    "rowGap", "gridTemplateColumns", "overflowX", "overflowY", "zIndex", "cursor",
    "textAlign", "whiteSpace", "filter", "backdropFilter", "visibility", "order",
)

NO_ANIM = (
    "*, *::before, *::after { animation: none !important; "
    "transition: none !important; caret-color: transparent !important; }"
)

COLLECT_JS = """
(props) => {
  const hash = (s) => { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0; return h; };
  const els = [...document.querySelectorAll('*')];
  return {
    count: els.length,
    tags: els.map((el) => el.tagName + (el.id ? '#' + el.id : '')),
    sigs: els.map((el) => { const cs = getComputedStyle(el); return hash(props.map((p) => cs[p]).join('\\u0001')); }),
  };
}
"""

OVERFLOW_JS = """
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const over = Math.round(r.right - vw);
    if (over <= 1) continue;
    const style = getComputedStyle(el);
    if (style.position === 'fixed') continue;
    offenders.push({ tag: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''), over, w: Math.round(r.width) });
  }
  offenders.sort((a, b) => b.over - a.over);
  return { horizontalScroll: Math.max(0, doc.scrollWidth - doc.clientWidth), offenders: offenders.slice(0, 4) };
}
"""

# Swap the app stylesheet for the baseline one and wait for it to actually be
# applied — the load event fires cross-origin, so no cssRules access is needed.
SWAP_JS = """
async (href) => {
  const link = [...document.querySelectorAll('link[rel=stylesheet]')]
    .find((x) => (x.getAttribute('href') || '').includes('styles'));
  if (!link) throw new Error('app stylesheet link not found');
  await new Promise((resolve) => {
    link.addEventListener('load', resolve, { once: true });
    link.setAttribute('href', href);
    setTimeout(resolve, 10000);
  });
  document.body.offsetHeight;
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
}
"""

CLICK_PANEL_JS = """
(label) => {
  const items = [...document.querySelectorAll('button, a, [role=button], .nav-item, li')];
  const hit = items.find((el) => (el.textContent || '').trim().toLowerCase() === label.toLowerCase());
  if (!hit) return false;
  hit.click();
  return true;
}
"""

# Only the active panel may be laid out. `#homePanel { display: flex }` shipped
# once and beat `.panel { display: none }` on ID specificity, which left TWO
# panels in the workspace: the Command Center filled the viewport and the panel
# the user actually clicked began below the fold.
PANEL_STATE_JS = """
() => {
  const shown = [...document.querySelectorAll('.panel')]
    .filter((p) => getComputedStyle(p).display !== 'none');
  const active = document.querySelector('.panel.active');
  const box = active ? active.getBoundingClientRect() : null;
  const workspace = document.querySelector('.workspace');
  const ws = workspace ? workspace.getBoundingClientRect() : null;
  return {
    shown: shown.map((p) => p.id),
    active: active ? active.id : null,
    activeTop: box ? Math.round(box.top) : null,
    rightGap: box && ws ? Math.round(ws.right - box.right) : null,
  };
}
"""

# The reading column (`#chatPanel`) is deliberately narrower than the workspace —
# a comfortable measure for conversation — so the fill check skips it.
_READING_MEASURE_PANELS = ("chatPanel",)
# --gutter-x tops out at 56px; anything past that plus slack is dead space.
_MAX_GUTTER_PX = 56
_FILL_CHECK_MAX_WIDTH = 1920


def _panel_state_failures(width: int, panel: str, state: dict[str, Any]) -> list[str]:
    """Failures for one (width, panel): panels stacked, off-screen, or unfilled."""
    failures: list[str] = []
    if len(state["shown"]) != 1 or state["shown"][0] != state["active"]:
        failures.append(
            f"{width}px {panel}: panels rendered at once {state['shown']} "
            f"(only '{state['active']}' should be laid out)"
        )
    if state["activeTop"] is None or not 0 <= state["activeTop"] < 900:
        failures.append(
            f"{width}px {panel}: the clicked panel starts at y={state['activeTop']} "
            "— outside the viewport, so its content is below the fold"
        )
    if panel not in _READING_MEASURE_PANELS and width <= _FILL_CHECK_MAX_WIDTH:
        gap = state["rightGap"]
        if gap is not None and gap > _MAX_GUTTER_PX + 8:
            failures.append(
                f"{width}px {panel}: content column leaves {gap}px of dead space on the "
                "right (the pane does not fill the screen)"
            )
    return failures


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:  # keep the audit output clean
        pass


def _serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    """Serve ``directory`` on an ephemeral loopback port; returns (base_url, server)."""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def _settle(page: Any) -> None:
    """Wait for real fonts before measuring.

    Text metrics drive the layout, so measuring against fallback fonts hides
    exactly the overflow that wide display faces cause. The font CDN is given a
    short budget; if it is unreachable the audit still runs on fallbacks.
    """
    page.evaluate("() => document.fonts ? document.fonts.ready : null")
    page.wait_for_timeout(400)


NAV_JS = """
() => {
  const nav = document.querySelector('nav') || document.querySelector('.nav');
  if (!nav) return null;
  const items = [...nav.querySelectorAll('button, a')];
  const vw = window.innerWidth;
  const clipped = items
    .filter((el) => { const r = el.getBoundingClientRect(); return r.right > vw + 1 || r.left < -1; })
    .map((el) => (el.textContent || '').trim());
  const sidebar = document.querySelector('.sidebar');
  const work = document.querySelector('.workspace');
  const sideBox = sidebar ? sidebar.getBoundingClientRect() : null;
  const workBox = work ? work.getBoundingClientRect() : null;
  const fixed = sidebar ? getComputedStyle(sidebar).position === 'fixed' : false;
  return {
    items: items.length,
    clipped,
    horizontalScroll: Math.max(0, nav.scrollWidth - nav.clientWidth),
    sidebarWidth: sideBox ? Math.round(sideBox.width) : 0,
    // A fixed sidebar sitting over the workspace is the off-canvas drawer: it
    // covers the panel the reader asked for.
    overlaysWorkspace: Boolean(fixed && sideBox && workBox && sideBox.right > workBox.left + 1),
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit NovaControl's web UI.")
    parser.add_argument("--url", default="http://127.0.0.1:8001/", help="running app URL")
    parser.add_argument("--baseline", type=Path, help="previous stylesheet to A/B against")
    parser.add_argument("--widths", type=int, nargs="*", default=list(WIDTHS))
    args = parser.parse_args()

    baseline_url: str | None = None
    server: socketserver.TCPServer | None = None
    if args.baseline is not None:
        base, server = _serve(args.baseline.resolve().parent)
        baseline_url = f"{base}/{args.baseline.name}"

    failures: list[str] = []
    drift: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width in args.widths:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.set_default_timeout(20000)
                page.goto(args.url, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                _settle(page)
                page.evaluate("css => { const s = document.createElement('style'); s.textContent = css;"
                              " document.head.appendChild(s); }", NO_ANIM)

                if baseline_url is not None:
                    live = page.evaluate(COLLECT_JS, list(PROPS))
                    page.evaluate(SWAP_JS, baseline_url)
                    old = page.evaluate(COLLECT_JS, list(PROPS))
                    if live["count"] != old["count"]:
                        drift.append(f"{width}px: element count {live['count']} -> {old['count']}")
                    else:
                        changed = [i for i in range(live["count"]) if live["sigs"][i] != old["sigs"][i]]
                        if changed:
                            sample = [live["tags"][i] for i in changed[:5]]
                            drift.append(f"{width}px: {len(changed)} element(s) restyled ({', '.join(sample)})")

                for panel in PANELS:
                    if not page.evaluate(CLICK_PANEL_JS, panel):
                        continue
                    page.wait_for_timeout(220)
                    res = page.evaluate(OVERFLOW_JS)
                    if res["horizontalScroll"] > 0 or res["offenders"]:
                        failures.append(
                            f"{width}px {panel}: page overflow {res['horizontalScroll']}px, "
                            f"offenders {json.dumps(res['offenders'])}"
                        )
                    failures.extend(_panel_state_failures(width, panel, page.evaluate(PANEL_STATE_JS)))
                    nav = page.evaluate(NAV_JS)
                    if nav and (nav["clipped"] or nav["horizontalScroll"] > 0):
                        failures.append(
                            f"{width}px {panel}: navigation hides destinations "
                            f"({len(nav['clipped'])} of {nav['items']} off-screen: "
                            f"{', '.join(nav['clipped'][:5])})"
                        )
                    if nav and nav["sidebarWidth"] <= 0:
                        failures.append(
                            f"{width}px {panel}: the sidebar has no width — navigation is "
                            "unreachable without a toggle"
                        )
                    elif nav and nav["overlaysWorkspace"]:
                        failures.append(
                            f"{width}px {panel}: the sidebar is fixed over the workspace, "
                            "so it covers the panel the reader opened"
                        )
                page.close()
            browser.close()
    finally:
        if server is not None:
            server.shutdown()

    for line in failures:
        print("FIT FAIL:", line)
    for line in drift:
        print("STYLE DRIFT:", line)
    if baseline_url is not None:
        print(f"stylesheets compared: live vs {args.baseline}")
    print(f"panels audited: {len(PANELS)} x {len(args.widths)} width(s)")
    if failures or drift:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS — every panel fits, one panel at a time; " +
          ("no computed-style drift" if baseline_url else "stylesheet A/B skipped"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
