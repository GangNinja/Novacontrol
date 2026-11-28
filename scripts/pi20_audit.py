"""PI-20 workspace audit: accessibility, responsive widths, performance.

Structured automated review of `#/predictionintel`, not a WCAG certification.

    python scripts/pi20_audit.py --base http://127.0.0.1:5173 --api http://127.0.0.1:8011

Checks
------
A. Accessibility — per view and for the whole workspace: document language,
   skip link, main landmark, nav label, one h1 with ordered headings, labelled
   form controls, named buttons/links, image alt text, duplicate ids, table
   header scopes, contrast of rendered text, keyboard focus order with a
   visible focus indicator, and the tablist contract (roving tabIndex, one
   `aria-selected`, ArrowLeft/ArrowRight/Home/End, `aria-controls` and a
   panel whose `aria-labelledby` names the active tab).
B. Responsive — the page is laid out at twelve widths from 1920 to 320 px;
   document horizontal overflow, overflowing elements outside a scrollable
   container, tab-strip clipping and the table wrapper are recorded.
C. Performance — navigation timing and paint entries, resource transfer,
   largest script, API latency per endpoint, and time from tab click to a
   settled panel.

Exit code 0 when no accessibility, overflow or latency failure is found.
Results are written to `.ui_audit/pi20_audit.json` for the completion report.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WIDTHS: tuple[int, ...] = (1920, 1440, 1366, 1280, 1024, 900, 820, 768, 600, 480, 390, 320)

API_ROUTES: tuple[tuple[str, str], ...] = (
    ("status", "/market/intelligence/status"),
    ("contracts", "/market/intelligence/contracts"),
    ("overview", "/market/intelligence/overview"),
    ("forecasts", "/market/intelligence/forecasts?limit=50"),
    ("compare", "/market/intelligence/compare"),
    ("lifecycle", "/market/intelligence/lifecycle?limit=50"),
    ("performance", "/market/intelligence/performance?limit=50"),
    ("regimes", "/market/intelligence/regimes?limit=50"),
    ("decisions", "/market/intelligence/decisions?limit=25"),
    ("research", "/market/intelligence/research?limit=25"),
    ("portfolio", "/market/intelligence/portfolio"),
    ("policies", "/market/intelligence/policies"),
    ("filters", "/market/intelligence/filters"),
)

# Latency budget for a read-only presentation route. A dashboard read above
# this is a bounded-query defect, not a slow market-data feed.
API_LATENCY_BUDGET_MS = 2000.0
TAB_SETTLE_BUDGET_MS = 4000.0

AUDIT_JS = r"""
() => {
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    // A closed <details> hides its body without removing layout boxes in
    // Chromium, and the accessibility tree excludes it.
    const closed = el.closest('details:not([open])');
    if (closed && !el.closest('summary')) return false;
    return el.getClientRects().length > 0;
  };
  const text = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const name = (el) => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const value = labelled.split(/\s+/)
        .map((id) => document.getElementById(id)?.textContent || '').join(' ').trim();
      if (value) return value;
    }
    return (el.getAttribute('aria-label') || '').trim()
      || (el.getAttribute('title') || '').trim() || text(el);
  };
  const labelFor = (el) => {
    if ((el.getAttribute('aria-label') || '').trim()) return 'aria-label';
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled && labelled.split(/\s+/)
      .some((id) => document.getElementById(id)?.innerText?.trim())) return 'aria-labelledby';
    if (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.textContent?.trim())
      return 'label[for]';
    if (el.closest('label')?.textContent?.trim()) return 'wrapping-label';
    if ((el.getAttribute('title') || '').trim()) return 'title';
    return '';
  };
  const describe = (el) => {
    const id = el.id ? `#${el.id}` : '';
    const cls = typeof el.className === 'string' && el.className.trim()
      ? `.${el.className.trim().split(/\s+/).slice(0, 2).join('.')}` : '';
    return `${el.tagName.toLowerCase()}${id}${cls}`;
  };
  const scope = document.querySelector('main') || document.body;
  const controls = [...scope.querySelectorAll('input, select, textarea')].filter(visible);
  const unlabeled = controls.filter((el) => !labelFor(el)).map((el) => ({
    element: describe(el), type: el.getAttribute('type') || el.tagName.toLowerCase()
  }));
  const buttons = [...scope.querySelectorAll('button')].filter(visible);
  const unnamedButtons = buttons.filter((el) => !name(el)).map(describe);
  const links = [...scope.querySelectorAll('a[href]')].filter(visible);
  const unnamedLinks = links.filter((el) => !name(el)).map(describe);
  const images = [...scope.querySelectorAll('img')].filter(visible);
  const missingAlt = images.filter((el) => !el.hasAttribute('alt')).map(describe);
  const ids = [...document.querySelectorAll('[id]')].map((el) => el.id);
  const counts = new Map();
  for (const id of ids) counts.set(id, (counts.get(id) || 0) + 1);
  const duplicateIds = [...counts.entries()].filter(([, count]) => count > 1)
    .map(([id, count]) => ({ id, count }));
  const headings = [...scope.querySelectorAll('h1, h2, h3, h4, h5, h6')].filter(visible)
    .map((el) => ({ level: Number(el.tagName.slice(1)), text: text(el).slice(0, 100) }));
  const headingSkips = [];
  for (let i = 1; i < headings.length; i += 1) {
    if (headings[i].level > headings[i - 1].level + 1) headingSkips.push({ from: headings[i - 1], to: headings[i] });
  }
  const tables = [...scope.querySelectorAll('table')].filter(visible);
  const tableIssues = tables.map((table, index) => {
    const headers = [...table.querySelectorAll('th')].filter(visible);
    const missingScope = headers.filter((th) => !th.getAttribute('scope')).length;
    return { index, headers: headers.length, missingScope, rows: table.querySelectorAll('tbody tr').length };
  }).filter((item) => item.headers === 0 || item.missingScope > 0);

  const parse = (raw) => {
    const match = String(raw || '').match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)/i);
    if (!match) return null;
    return [Number(match[1]), Number(match[2]), Number(match[3]), match[4] === undefined ? 1 : Number(match[4])];
  };
  const over = (front, back) => {
    const alpha = front[3] + back[3] * (1 - front[3]);
    if (alpha <= 0) return [0, 0, 0, 0];
    return [
      (front[0] * front[3] + back[0] * back[3] * (1 - front[3])) / alpha,
      (front[1] * front[3] + back[1] * back[3] * (1 - front[3])) / alpha,
      (front[2] * front[3] + back[2] * back[3] * (1 - front[3])) / alpha,
      alpha,
    ];
  };
  const background = (el) => {
    const layers = [];
    let node = el;
    while (node && node !== document.documentElement) {
      const s = getComputedStyle(node);
      if (s.backgroundImage && s.backgroundImage !== 'none') return null;
      const color = parse(s.backgroundColor);
      if (color && color[3] > 0.01) {
        layers.push(color);
        if (color[3] >= 0.99) break;
      }
      node = node.parentElement;
    }
    let base = [6, 16, 28, 1];
    for (let i = layers.length - 1; i >= 0; i -= 1)
      base = over(layers[i], base);
    return base;
  };
  const luminance = (color) => {
    const channel = (value) => {
      const v = value / 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2]);
  };
  const contrastFailures = [];
  const nodes = [...scope.querySelectorAll('p, span, small, td, th, label, a, button, h1, h2, h3, h4, strong, em, li, summary, option')]
    .filter((el) => visible(el) && el.children.length === 0 && text(el).length > 1)
    .slice(0, 400);
  for (const el of nodes) {
    const s = getComputedStyle(el);
    const fg = parse(s.color);
    const bg = background(el);
    if (!fg || !bg) continue;
    const effective = over(fg, bg);
    const size = Number.parseFloat(s.fontSize);
    const weight = Number.parseInt(s.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const required = large ? 3 : 4.5;
    const l1 = luminance(effective);
    const l2 = luminance(bg);
    const value = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    if (value + 0.01 < required) {
      contrastFailures.push({
        element: describe(el), text: text(el).slice(0, 90),
        ratio: Number(value.toFixed(2)), required,
        fontSize: s.fontSize, fontWeight: s.fontWeight,
      });
    }
  }

  const tablist = document.querySelector('[role="tablist"]');
  const tabs = tablist ? [...tablist.querySelectorAll('[role="tab"]')] : [];
  const selected = tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true');
  const panel = document.querySelector('[role="tabpanel"]');
  const tabState = {
    count: tabs.length,
    selected: selected.map((tab) => tab.id),
    roving: tabs.every((tab) => (tab.getAttribute('aria-selected') === 'true' ? tab.tabIndex === 0 : tab.tabIndex === -1)),
    controlsMatch: tabs.every((tab) => Boolean(tab.getAttribute('aria-controls')) && Boolean(document.getElementById(tab.getAttribute('aria-controls')))),
    panelRole: panel ? 'tabpanel' : '',
    panelLabelledBy: panel?.getAttribute('aria-labelledby') || '',
    activeTabId: selected[0]?.id || '',
    activeTabLabel: selected[0] ? text(selected[0]) : '',
    labels: tabs.map((tab) => text(tab)),
    clipped: tabs.filter((tab) => {
      const rect = tab.getBoundingClientRect();
      return rect.right > window.innerWidth + 2 || rect.width < 24 || rect.height < 12;
    }).map((tab) => text(tab)),
  };

  return {
    hash: window.location.hash,
    htmlLang: document.documentElement.getAttribute('lang') || '',
    skipLink: Boolean(document.querySelector('.skip-link')),
    main: Boolean(document.querySelector('main')),
    navLabels: [...document.querySelectorAll('nav')].filter(visible).map((el) => el.getAttribute('aria-label') || ''),
    h1Count: headings.filter((item) => item.level === 1).length,
    headings: headings.slice(0, 12),
    headingSkips,
    controlCount: controls.length + buttons.length + links.length,
    unlabeled,
    unnamedButtons,
    unnamedLinks,
    missingAlt,
    duplicateIds,
    tableCount: tables.length,
    tableIssues,
    contrastChecked: nodes.length,
    contrastFailures: contrastFailures.sort((a, b) => a.ratio - b.ratio).slice(0, 20),
    tabState,
    horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
  };
}
"""

FOCUS_JS = r"""
() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return { valid: false, tag: 'body', name: '' };
  const s = getComputedStyle(el);
  const outline = s.outlineStyle !== 'none' && Number.parseFloat(s.outlineWidth || '0') > 0;
  const shadow = s.boxShadow && s.boxShadow !== 'none';
  return {
    valid: true,
    tag: el.tagName.toLowerCase(),
    name: (el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 100),
    focusVisible: Boolean(outline || shadow),
    inMain: Boolean(el.closest('main')),
    role: el.getAttribute('role') || '',
  };
}
"""

OVERFLOW_JS = r"""
() => {
  const vw = window.innerWidth;
  const insideScroller = (el) => {
    let node = el.parentElement;
    while (node && node !== document.body) {
      const s = getComputedStyle(node);
      if ((s.overflowX === 'auto' || s.overflowX === 'scroll') && node.scrollWidth > node.clientWidth) return true;
      node = node.parentElement;
    }
    return false;
  };
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0 && el.getClientRects().length > 0;
  };
  const offenders = [...document.querySelectorAll('main *')].filter((el) => {
    if (!visible(el) || insideScroller(el)) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.right > vw + 2;
  }).slice(0, 8).map((el) => {
    const rect = el.getBoundingClientRect();
    return {
      element: `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${
        typeof el.className === 'string' && el.className.trim() ? `.${el.className.trim().split(/\s+/).slice(0, 2).join('.')}` : ''
      }`,
      right: Math.round(rect.right),
      width: Math.round(rect.width),
    };
  });
  const tableWrap = document.querySelector('main .table-wrap');
  const tablist = document.querySelector('[role="tablist"]');
  return {
    viewport: vw,
    documentScrollWidth: document.documentElement.scrollWidth,
    overflow: document.documentElement.scrollWidth > vw + 2,
    offenders,
    tableWrap: tableWrap ? {
      clientWidth: Math.round(tableWrap.clientWidth),
      scrollWidth: Math.round(tableWrap.scrollWidth),
      internalScroll: tableWrap.scrollWidth > tableWrap.clientWidth + 2,
    } : null,
    tablist: tablist ? {
      width: Math.round(tablist.getBoundingClientRect().width),
      height: Math.round(tablist.getBoundingClientRect().height),
      scrollWidth: Math.round(tablist.scrollWidth),
      clientWidth: Math.round(tablist.clientWidth),
    } : null,
    h1Visible: Boolean(document.querySelector('main h1')),
  };
}
"""


def api_probe(api: str, token: str, repeats: int = 3) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for label, path in API_ROUTES:
        samples: list[float] = []
        status = 0
        size = 0
        for _ in range(repeats):
            request = urllib.request.Request(f"{api.rstrip('/')}{path}", headers={"Authorization": f"Bearer {token}"})
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed loopback origin
                    body = response.read()
                    status = response.status
                    size = len(body)
            except Exception as error:  # noqa: BLE001 - recorded, not raised
                results.append({"endpoint": label, "path": path, "error": str(error)[:200]})
                samples = []
                break
            samples.append(round((time.perf_counter() - started) * 1000.0, 2))
        if samples:
            results.append({
                "endpoint": label,
                "path": path,
                "status": status,
                "bytes": size,
                "median_ms": round(statistics.median(samples), 2),
                "max_ms": max(samples),
                "samples_ms": samples,
            })
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--api", default="http://127.0.0.1:8011")
    parser.add_argument(
        "--token",
        default=os.environ.get("NOVACONTROL_API_TOKEN")
        or os.environ.get("NOVACONTROL_ADMIN_TOKEN", ""),
        help="Bearer credential; defaults to NOVACONTROL_API_TOKEN or NOVACONTROL_ADMIN_TOKEN",
    )
    parser.add_argument("--out", default=".ui_audit/pi20_audit.json")
    parser.add_argument("--widths", type=int, nargs="*", default=list(WIDTHS))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    url = f"{args.base.rstrip('/')}/#/predictionintel"
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(message.text[:300]) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)[:300]))

        started = time.perf_counter()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("h1", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=60_000)
        load_ms = round((time.perf_counter() - started) * 1000.0, 2)

        navigation = page.evaluate(
            """() => {
                const nav = performance.getEntriesByType('navigation')[0] || {};
                const paint = performance.getEntriesByType('paint').map((entry) => ({ name: entry.name, startTime: entry.startTime }));
                const resources = performance.getEntriesByType('resource');
                const scripts = resources.filter((entry) => entry.name.endsWith('.js'));
                const largest = scripts.sort((a, b) => b.transferSize - a.transferSize)[0];
                return {
                    ttfbMs: nav.responseStart ? Number(nav.responseStart.toFixed(1)) : null,
                    domContentLoadedMs: nav.domContentLoadedEventEnd ? Number(nav.domContentLoadedEventEnd.toFixed(1)) : null,
                    loadEventMs: nav.loadEventEnd ? Number(nav.loadEventEnd.toFixed(1)) : null,
                    paint,
                    requests: resources.length,
                    transferredBytes: resources.reduce((sum, entry) => sum + (entry.transferSize || 0), 0),
                    scripts: scripts.length,
                    largestScript: largest ? { name: largest.name.split('/').pop(), transferBytes: largest.transferSize } : null,
                };
            }"""
        )

        # A. Accessibility on the landing view.
        audits: dict[str, Any] = {}
        audits["overview"] = page.evaluate(AUDIT_JS)
        page.evaluate("() => { document.body.tabIndex = -1; document.body.focus(); }")
        stops: list[dict[str, Any]] = []
        for _ in range(8):
            page.keyboard.press("Tab")
            stops.append(page.evaluate(FOCUS_JS))
        keyboard = {
            "stops": stops,
            "skip_link_first": bool(stops and "skip to main" in stops[0].get("name", "").lower()),
            "all_visible_focus": all(stop.get("focusVisible") for stop in stops if stop.get("valid")),
            "all_valid": all(stop.get("valid") for stop in stops),
        }

        # Tablist keyboard contract.
        page.evaluate("() => document.querySelector('[role=\"tab\"]').focus()")
        before = page.evaluate("() => document.querySelector('[aria-selected=\"true\"]')?.id")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(250)
        after_right = page.evaluate("() => ({ id: document.querySelector('[aria-selected=\"true\"]')?.id, active: document.activeElement?.id })")
        page.keyboard.press("End")
        page.wait_for_timeout(250)
        after_end = page.evaluate("() => ({ id: document.querySelector('[aria-selected=\"true\"]')?.id, active: document.activeElement?.id })")
        page.keyboard.press("Home")
        page.wait_for_timeout(400)
        tab_keyboard = {
            "before": before,
            "after_arrow_right": after_right,
            "after_end": after_end,
            "arrow_right_moves_selection": bool(after_right.get("id") and after_right.get("id") != before),
            "arrow_right_moves_focus": bool(after_right.get("active")),
            "end_selects_last": bool(after_end.get("id", "").endswith("-replay")),
            "home_returns_first": page.evaluate("() => document.querySelector('[aria-selected=\"true\"]')?.id") == before,
        }

        # Accessibility of every view.
        labels = audits["overview"]["tabState"]["labels"]
        tab_ids = page.evaluate("() => [...document.querySelectorAll('[role=\"tab\"]')].map((tab) => tab.id)")
        tab_timings: list[dict[str, Any]] = []
        for tab_id in tab_ids:
            label = page.evaluate("(id) => document.getElementById(id)?.innerText || ''", tab_id)
            settled_started = time.perf_counter()
            page.click(f"#{tab_id}")
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001 - recorded below
                pass
            elapsed = round((time.perf_counter() - settled_started) * 1000.0, 1)
            page.wait_for_timeout(200)
            view_id = tab_id.replace("prediction-intel-tab-", "")
            audits[view_id] = page.evaluate(AUDIT_JS)
            tab_timings.append({"tab": label, "id": view_id, "settle_ms": elapsed})
        page.click("#prediction-intel-tab-overview")
        page.wait_for_timeout(400)

        # B. Responsive widths.
        widths: list[dict[str, Any]] = []
        for width in args.widths:
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(400)
            measured = page.evaluate(OVERFLOW_JS)
            measured["width"] = width
            widths.append(measured)
        page.set_viewport_size({"width": 1440, "height": 900})

        browser.close()

    api = api_probe(args.api, args.token)

    # Aggregate accessibility findings.
    issues: list[dict[str, Any]] = []
    for view, audit in audits.items():
        prefix = {"view": view}
        for entry in audit["unlabeled"]:
            issues.append({**prefix, "check": "unlabeled_control", **entry})
        for entry in audit["unnamedButtons"]:
            issues.append({**prefix, "check": "unnamed_button", "element": entry})
        for entry in audit["unnamedLinks"]:
            issues.append({**prefix, "check": "unnamed_link", "element": entry})
        for entry in audit["missingAlt"]:
            issues.append({**prefix, "check": "missing_alt", "element": entry})
        for entry in audit["duplicateIds"]:
            issues.append({**prefix, "check": "duplicate_id", **entry})
        for entry in audit["headingSkips"]:
            issues.append({**prefix, "check": "heading_skip", **entry})
        for entry in audit["tableIssues"]:
            issues.append({**prefix, "check": "table_header", **entry})
        for entry in audit["contrastFailures"]:
            issues.append({**prefix, "check": "contrast", **entry})
        if audit["h1Count"] != 1:
            issues.append({**prefix, "check": "h1_count", "count": audit["h1Count"]})
        if not audit["htmlLang"]:
            issues.append({**prefix, "check": "html_lang"})
        if not audit["skipLink"]:
            issues.append({**prefix, "check": "skip_link"})
        if not audit["main"]:
            issues.append({**prefix, "check": "main_landmark"})
        if not audit["navLabels"] or not all(audit["navLabels"]):
            issues.append({**prefix, "check": "nav_label", "labels": audit["navLabels"]})
        state = audit["tabState"]
        if state["count"] != 13:
            issues.append({**prefix, "check": "tab_count", "count": state["count"]})
        if len(state["selected"]) != 1:
            issues.append({**prefix, "check": "tab_selected", "selected": state["selected"]})
        if not state["roving"]:
            issues.append({**prefix, "check": "tab_roving"})
        if not state["controlsMatch"]:
            issues.append({**prefix, "check": "tab_controls"})
        if state["panelRole"] != "tabpanel" or state["panelLabelledBy"] != state["activeTabId"]:
            issues.append({**prefix, "check": "tabpanel_label", "panel": state["panelLabelledBy"], "tab": state["activeTabId"]})
        if state["clipped"]:
            issues.append({**prefix, "check": "tab_clipped", "tabs": state["clipped"]})
    if not keyboard["skip_link_first"]:
        issues.append({"view": "keyboard", "check": "skip_link_first", "stops": keyboard["stops"][:2]})
    if not keyboard["all_visible_focus"]:
        issues.append({"view": "keyboard", "check": "focus_visible", "stops": keyboard["stops"]})
    if not keyboard["all_valid"]:
        issues.append({"view": "keyboard", "check": "focus_order", "stops": keyboard["stops"]})
    for check in ("arrow_right_moves_selection", "arrow_right_moves_focus", "end_selects_last", "home_returns_first"):
        if not tab_keyboard[check]:
            issues.append({"view": "tabs", "check": check, "detail": tab_keyboard})

    overflow_failures = [item for item in widths if item["overflow"] or item["offenders"] or not item["h1Visible"]]
    slow_api = [item for item in api if item.get("median_ms", 0) > API_LATENCY_BUDGET_MS or item.get("error")]
    slow_tabs = [item for item in tab_timings if item["settle_ms"] > TAB_SETTLE_BUDGET_MS]

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": url,
        "api_url": args.api,
        "method": "Playwright Chromium automated inspection; not a WCAG conformance certification.",
        "timing": {"load_ms": load_ms, "navigation": navigation, "tabs": tab_timings, "api": api},
        "accessibility": {
            "keyboard": keyboard,
            "tab_keyboard": tab_keyboard,
            "views": {view: audit["tabState"]["activeTabLabel"] for view, audit in audits.items()},
            "controls_per_view": {view: audit["controlCount"] for view, audit in audits.items()},
            "issues": issues,
            "checks": {
                "views_audited": len(audits),
                "controls_audited": sum(audit["controlCount"] for audit in audits.values()),
                "tables_audited": sum(audit["tableCount"] for audit in audits.values()),
                "contrast_nodes": sum(audit["contrastChecked"] for audit in audits.values()),
            },
        },
        "responsive": {"widths": widths, "failures": overflow_failures},
        "performance": {
            "api_latency_budget_ms": API_LATENCY_BUDGET_MS,
            "tab_settle_budget_ms": TAB_SETTLE_BUDGET_MS,
            "slow_api": slow_api,
            "slow_tabs": slow_tabs,
        },
        "console_errors": console_errors,
        "page_errors": page_errors,
        "limitations": [
            "Automated checks cannot establish semantic quality, reading order or cognitive accessibility.",
            "Contrast sampling skips gradient/image backgrounds to avoid invented values.",
            "No screen-reader session (NVDA/JAWS/VoiceOver) was run; no WCAG certification is claimed.",
            "Widths are CSS viewports at 100% zoom; browser zoom and OS text scaling are separate.",
        ],
    }
    result["verdict"] = "PASS" if not (
        issues or overflow_failures or slow_api or slow_tabs or console_errors or page_errors
    ) else "FAIL"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "verdict": result["verdict"],
        "views_audited": result["accessibility"]["checks"]["views_audited"],
        "controls_audited": result["accessibility"]["checks"]["controls_audited"],
        "accessibility_issues": len(issues),
        "responsive_failures": [item["width"] for item in overflow_failures],
        "slow_api": [item["endpoint"] for item in slow_api],
        "slow_tabs": [item["tab"] for item in slow_tabs],
        "console_errors": len(console_errors),
        "page_errors": len(page_errors),
        "load_ms": load_ms,
        "fcp_ms": next((item["startTime"] for item in navigation["paint"] if item["name"] == "first-contentful-paint"), None),
    }, indent=2))
    for entry in issues[:20]:
        print(json.dumps(entry))
    for item in overflow_failures:
        print(json.dumps({"width": item["width"], "overflow": item["overflow"], "offenders": item["offenders"]}))
    print(f"wrote {out}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
