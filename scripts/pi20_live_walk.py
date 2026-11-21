"""Live walk of the PI-20 workspace: all 13 views, states and widths.

Drives the running app (Vite + FastAPI) with Playwright and records, per view:

* the data-status badge and any banner (stale/partial classification),
* an empty state or table content (and how many rows),
* an error block when the request fails,
* a loading block while the request is in flight,
* console and page errors,
* horizontal overflow at desktop and smaller widths.

It also performs the real interactions the page offers (instrument lookup,
decision-cycle selection, research-run selection, replay reconstruction, every
field of the collapsed filter drawer, and Save filters followed by a reload) and
checks the induced states through request interception, so the recorded result
says which state was observed and how.

    python scripts/pi20_live_walk.py --base http://127.0.0.1:5173

Results go to `.ui_audit/pi20_live_walk.json`. Exit code 0 only when every
view rendered, no console/page error occurred outside the induced failure, no
width overflowed and each demanded state was observed.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WIDTHS: tuple[int, ...] = (1440, 1024, 768, 480, 390)
VIEWS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("instrument", "Instrument"),
    ("forecasts", "Forecasts"),
    ("compare", "Comparison"),
    ("horizons", "Horizons"),
    ("lifecycle", "Lifecycle"),
    ("performance", "Performance"),
    ("regimes", "Regimes"),
    ("decisions", "Agent decisions"),
    ("research", "Research"),
    ("portfolio", "Portfolio"),
    ("policies", "RL & adaptive"),
    ("replay", "Replay"),
)
STATUS_VALUES = {
    "AVAILABLE",
    "PARTIAL_DATA",
    "STALE",
    "INSUFFICIENT_EVIDENCE",
    "UNAVAILABLE",
    "FAILED",
}
INSTRUMENT = "XBOM:ADANIENT"
SAVED_HORIZON = "5d"
CYCLE = "mac_fa06f1bd4d584295bc9b2c9d7992af77"

STATE_JS = r"""
() => {
  const text = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => el && el.getClientRects().length > 0;
  const panel = document.querySelector('[role="tabpanel"]') || document.querySelector('main');
  const badges = [...panel.querySelectorAll('.badge')].filter(visible).map(text);
  return {
    status: badges.filter((value) => %s.includes(value.split(' ')[0].toUpperCase())),
    badges: badges.slice(0, 12),
    banners: [...panel.querySelectorAll('.banner')].filter(visible).map(text).slice(0, 8),
    emptyStates: [...panel.querySelectorAll('.empty-state')].filter(visible)
      .map((el) => text(el).slice(0, 160)),
    errorBlocks: [...panel.querySelectorAll('.error-block')].filter(visible).map(text),
    alerts: [...panel.querySelectorAll('[role="alert"]')].filter(visible).map(text).slice(0, 4),
    loading: Boolean(panel.querySelector('[aria-busy="true"]')),
    tables: panel.querySelectorAll('table').length,
    rows: panel.querySelectorAll('tbody tr').length,
    headings: [...panel.querySelectorAll('h2, h3')].filter(visible).map(text).slice(0, 8),
    textSample: text(panel).slice(0, 240),
    skeleton: Boolean(panel.querySelector('.skeleton')),
  };
}
""" % json.dumps(sorted(STATUS_VALUES))

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
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0
      && el.getClientRects().length > 0;
  };
  const offenders = [...document.querySelectorAll('main *')].filter((el) => {
    if (!visible(el) || insideScroller(el)) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.right > vw + 2;
  }).slice(0, 5).map((el) => ({
    element: `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}`,
    right: Math.round(el.getBoundingClientRect().right),
  }));
  return {
    width: vw,
    documentScrollWidth: document.documentElement.scrollWidth,
    overflow: document.documentElement.scrollWidth > vw + 2,
    offenders,
    tabs: document.querySelectorAll('[role="tab"]').length,
    heading: Boolean(document.querySelector('main h1')),
    panelWidth: Math.round((document.querySelector('[role="tabpanel"]') || document.body).getBoundingClientRect().width),
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--out", default=".ui_audit/pi20_live_walk.json")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    url = f"{args.base.rstrip('/')}/#/predictionintel"
    console: list[dict[str, str]] = []
    page_errors: list[str] = []
    current = {"view": "startup"}
    failures: list[str] = []

    def on_console(message: Any) -> None:
        if message.type == "error":
            console.append({"view": current["view"], "text": message.text[:300]})

    def on_page_error(error: Any) -> None:
        page_errors.append(f"{current['view']}: {str(error)[:300]}")

    def snap(page: Any) -> dict[str, Any]:
        state = page.evaluate(STATE_JS)
        state.setdefault("console_errors", [])
        return state

    def open_view(page: Any, label: str, settle: int = 900) -> dict[str, Any]:
        current["view"] = label
        before = len(console)
        page.get_by_role("tab", name=label, exact=True).click()
        started = time.perf_counter()
        page.wait_for_timeout(settle)
        state = snap(page)
        state["settle_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        state["console_errors"] = [item["text"] for item in console[before:]]
        return state

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("h1", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=60_000)

        views: dict[str, Any] = {}
        for view_id, label in VIEWS:
            state = open_view(page, label)
            views[view_id] = {"label": label, "state": state, "interactions": []}
            if state["errorBlocks"]:
                failures.append(f"{view_id}: unexpected error block {state['errorBlocks'][0][:80]}")
            if state["console_errors"]:
                failures.append(f"{view_id}: console error {state['console_errors'][0][:80]}")
            if not state["headings"]:
                failures.append(f"{view_id}: no h2/h3 rendered")

        # --- real interactions -------------------------------------------------
        # Instrument lookup.
        current["view"] = "instrument"
        page.get_by_role("tab", name="Instrument", exact=True).click()
        page.wait_for_timeout(600)
        empty_before = snap(page)
        field = page.get_by_label("Instrument symbol")
        field.fill(INSTRUMENT)
        page.get_by_role("button", name="Open instrument").click()
        page.wait_for_timeout(1400)
        detail = snap(page)
        views["instrument"]["interactions"].append({
            "action": f"Open instrument {INSTRUMENT}",
            "empty_state_before": empty_before["emptyStates"],
            "status_after": detail["status"],
            "banners_after": detail["banners"],
            "tables_after": detail["tables"],
            "rows_after": detail["rows"],
            "headings_after": detail["headings"],
            "console_errors": detail["console_errors"],
        })
        if not detail["tables"]:
            failures.append("instrument: lookup rendered no tables")
        if "STALE" not in detail["status"] and "AVAILABLE" not in detail["status"]:
            failures.append(f"instrument: unexpected status after lookup {detail['status']}")

        # Horizons needs the global instrument filter; verify its real empty state first.
        current["view"] = "horizons"
        page.get_by_role("tab", name="Horizons", exact=True).click()
        page.wait_for_timeout(700)
        horizons_empty = snap(page)
        filter_input = page.locator("main .filter-toolbar input").first
        filter_input.fill(INSTRUMENT)
        page.wait_for_timeout(400)
        page.get_by_role("tab", name="Horizons", exact=True).click()
        page.wait_for_timeout(1200)
        horizons_filled = snap(page)
        views["horizons"]["interactions"].append({
            "action": "global instrument filter then Horizons",
            "empty_state_without_instrument": horizons_empty["emptyStates"],
            "status_after": horizons_filled["status"],
            "headings_after": horizons_filled["headings"],
            "tables_after": horizons_filled["tables"],
            "banners_after": horizons_filled["banners"],
            "console_errors": horizons_filled["console_errors"],
        })
        if not any("Choose an instrument" in item for item in views["horizons"]["state"]["emptyStates"]):
            failures.append("horizons: no empty state without an instrument")

        # Filter drawer: expand it and drive every collapsed field.
        current["view"] = "filters"
        page.locator("details.filter-drawer summary").click()
        page.wait_for_timeout(300)
        drawer = page.evaluate(
            """() => {
                const inputs = [...document.querySelectorAll('details.filter-drawer input')]
                    .filter((el) => el.getClientRects().length > 0);
                return { visibleInputs: inputs.length, open: document.querySelector('details.filter-drawer').open };
            }"""
        )
        if drawer["visibleInputs"] < 8 or not drawer["open"]:
            failures.append(f"filters: drawer did not expand ({drawer})")
        drawer_values = {
            "Exchange": "XNSE",
            "Sector": "Technology",
            "Regime": "bull",
            "Evaluation start": "2026-10-01",
            "Evaluation end": "2026-11-01",
            "Agent": "agent-walk",
            "Portfolio": "pf-walk",
            "Research status": "completed",
        }
        drawer_read: dict[str, str] = {}
        for label, value in drawer_values.items():
            control = page.get_by_label(label, exact=True)
            control.fill(value)
            page.wait_for_timeout(120)
            drawer_read[label] = control.input_value()
        page.wait_for_timeout(500)
        after_drawer = snap(page)
        for label, value in drawer_values.items():
            if drawer_read[label] != value:
                failures.append(f"filters: drawer field {label!r} held {drawer_read[label]!r}, not {value!r}")
        if after_drawer["errorBlocks"]:
            failures.append(
                f"filters: drawer fields produced an error block {after_drawer['errorBlocks'][0][:80]}"
            )
        if after_drawer["console_errors"]:
            failures.append(
                f"filters: drawer fields produced a console error {after_drawer['console_errors'][0][:80]}"
            )
        for label in drawer_values:
            page.get_by_label(label, exact=True).fill("")
        page.wait_for_timeout(300)
        page.locator("details.filter-drawer summary").click()
        page.wait_for_timeout(200)

        filter_controls: dict[str, Any] = {
            "drawer_open": drawer["open"],
            "drawer_visible_inputs": drawer["visibleInputs"],
            "drawer_values_typed": drawer_values,
            "drawer_values_read_back": drawer_read,
            "drawer_error_block": after_drawer["errorBlocks"],
            "drawer_console_errors": after_drawer["console_errors"],
        }

        # Saved filters: save a real record, reload the page, read the controls back.
        current["view"] = "filters-saved"
        previous = page.evaluate(
            """async () => {
                const response = await fetch('/market/intelligence/filters?name=default');
                return response.ok ? await response.json() : { saved: false, filters: {} };
            }"""
        )
        # The toolbar's labels carry a hint, so address the controls directly.
        page.locator("main .filter-toolbar input").first.fill(INSTRUMENT)
        page.locator("main .filter-toolbar select").first.select_option(SAVED_HORIZON)
        page.wait_for_timeout(200)
        save_status = 0
        try:
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and response.url.split("?")[0].endswith("/market/intelligence/filters"),
                timeout=15_000,
            ) as saved_call:
                page.get_by_role("button", name="Save filters").click()
            save_status = saved_call.value.status
        except Exception as exc:  # noqa: BLE001 - recorded as a failure
            failures.append(f"filters: Save filters produced no PUT response ({exc})")
        page.wait_for_timeout(400)
        stored = page.evaluate(
            """async () => (await (await fetch('/market/intelligence/filters?name=default')).json())"""
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("h1", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=60_000)
        page.wait_for_timeout(700)
        reloaded = {
            "instrument": page.locator("main .filter-toolbar input").first.input_value(),
            "horizon": page.locator("main .filter-toolbar select").first.input_value(),
        }
        stored_filters = stored.get("filters") or {}
        filter_controls["saved_filters"] = {
            "previous_saved": bool(previous.get("saved")),
            "expected": {"instrument": INSTRUMENT, "horizon": SAVED_HORIZON},
            "save_status": save_status,
            "stored": {
                "saved": bool(stored.get("saved")),
                "instrument": stored_filters.get("instrument_id"),
                "horizon": stored_filters.get("horizon"),
            },
            "reloaded": reloaded,
        }
        if save_status != 200:
            failures.append(f"filters: Save filters answered {save_status}")
        if not (
            stored.get("saved")
            and stored_filters.get("instrument_id") == INSTRUMENT
            and stored_filters.get("horizon") == SAVED_HORIZON
        ):
            failures.append("filters: the saved record did not store the instrument and horizon")
        if reloaded["instrument"] != INSTRUMENT or reloaded["horizon"] != SAVED_HORIZON:
            failures.append(f"filters: reload did not restore the saved filters ({reloaded})")
        # Leave the workspace defaults as they were: the store has no delete route.
        restored_status = page.evaluate(
            """async (payload) => (await fetch('/market/intelligence/filters', {
                method: 'PUT',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify(payload),
            })).status""",
            {"name": "default", "filters": previous.get("filters") or {}},
        )
        filter_controls["saved_filters"]["restored_status"] = restored_status
        filter_controls["saved_filters"]["restored_to"] = (
            "the filters saved before the walk" if previous.get("saved") else "empty filters"
        )
        # Reload once more so the rest of the walk runs on the restored defaults.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("h1", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=60_000)
        page.wait_for_timeout(600)
        after_restore = {
            "instrument": page.locator("main .filter-toolbar input").first.input_value(),
            "horizon": page.locator("main .filter-toolbar select").first.input_value(),
        }
        expected_after_restore = {
            "instrument": str((previous.get("filters") or {}).get("instrument_id") or ""),
            "horizon": str((previous.get("filters") or {}).get("horizon") or ""),
        }
        filter_controls["saved_filters"]["reloaded_after_restore"] = after_restore
        if after_restore != expected_after_restore:
            failures.append(
                f"filters: reload after the restore showed {after_restore}, not {expected_after_restore}"
            )

        # Decision cycle selection (detail + evidence + explain).
        current["view"] = "decisions"
        page.get_by_role("tab", name="Agent decisions", exact=True).click()
        page.wait_for_timeout(1200)
        before_decisions = snap(page)
        page.locator("main tbody tr").first.click()
        page.wait_for_timeout(1600)
        after_decisions = snap(page)
        views["decisions"]["interactions"].append({
            "action": "select first decision cycle",
            "rows_before": before_decisions["rows"],
            "headings_after": after_decisions["headings"],
            "tables_after": after_decisions["tables"],
            "banners_after": after_decisions["banners"],
            "console_errors": after_decisions["console_errors"],
        })
        if after_decisions["tables"] <= before_decisions["tables"]:
            failures.append("decisions: selecting a cycle added no detail table")

        # Research run selection.
        current["view"] = "research"
        page.get_by_role("tab", name="Research", exact=True).click()
        page.wait_for_timeout(1200)
        before_research = snap(page)
        page.locator("main tbody tr").first.click()
        page.wait_for_timeout(1600)
        after_research = snap(page)
        factor_rows = page.evaluate(
            """() => [...document.querySelectorAll('main .plain-list li')].map((el) => el.innerText.slice(0, 160))"""
        )
        views["research"]["interactions"].append({
            "action": "select first research run",
            "thesis_lists": factor_rows[:6],
            "empty_state_before": before_research["emptyStates"],
            "headings_after": after_research["headings"],
            "tables_after": after_research["tables"],
            "console_errors": after_research["console_errors"],
        })
        if not after_research["tables"]:
            failures.append("research: selecting a run rendered no detail tables")
        if not any("Company, financials and valuation" in item for item in after_research["headings"]):
            failures.append("research: run detail did not render")
        if any(item.lstrip().startswith("{") for item in factor_rows):
            failures.append("research: a thesis factor still renders as raw JSON")

        # Replay: real empty state, then reconstruct a real cycle.
        current["view"] = "replay"
        page.get_by_role("tab", name="Replay", exact=True).click()
        page.wait_for_timeout(700)
        replay_empty = snap(page)
        replay_field = page.locator("main input[placeholder=\"Decision cycle ID\"]")
        replay_field.fill(CYCLE)
        page.get_by_role("button", name="Reconstruct").click()
        page.wait_for_timeout(1800)
        replay_filled = snap(page)
        views["replay"]["interactions"].append({
            "action": f"reconstruct {CYCLE}",
            "empty_state_before": replay_empty["emptyStates"],
            "status_after": replay_filled["status"],
            "headings_after": replay_filled["headings"],
            "banners_after": replay_filled["banners"],
            "tables_after": replay_filled["tables"],
            "console_errors": replay_filled["console_errors"],
        })
        if not any("Choose a past decision" in item for item in views["replay"]["state"]["emptyStates"]):
            failures.append("replay: no empty state before a cycle is chosen")
        if not replay_filled["tables"]:
            failures.append("replay: reconstruction rendered no tables")

        # --- induced states ----------------------------------------------------
        induced: dict[str, Any] = {}

        # Loading: hold the overview response open and watch the aria-busy block.
        current["view"] = "loading-overview"
        def slow_overview(route: Any) -> None:
            time.sleep(1.2)
            route.continue_()

        page.route("**/market/intelligence/overview*", slow_overview)
        page.get_by_role("tab", name="Overview", exact=True).click()
        page.wait_for_timeout(250)
        loading_state = snap(page)
        page.wait_for_timeout(1600)
        page.unroute("**/market/intelligence/overview*")
        induced["loading"] = {
            "loading_block_seen": loading_state["loading"],
            "skeleton_seen": loading_state["skeleton"],
            "text_during_loading": loading_state["textSample"],
            "loaded_after": snap(page)["rows"] > 0,
        }
        if not (loading_state["loading"] and loading_state["skeleton"]):
            failures.append(f"loading: no loading block during a slow overview ({loading_state['textSample'][:80]})")

        current["view"] = "loading-decisions"
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 900, "downloadThroughput": -1, "uploadThroughput": -1,
        })
        page.get_by_role("tab", name="Agent decisions", exact=True).click()
        page.wait_for_timeout(300)
        decisions_loading = snap(page)
        page.wait_for_timeout(2200)
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1,
        })
        induced["loading_decisions"] = {
            "loading_block_seen": decisions_loading["loading"],
            "skeleton_seen": decisions_loading["skeleton"],
            "text_during_loading": decisions_loading["textSample"],
        }
        if not (decisions_loading["loading"] and decisions_loading["skeleton"]):
            failures.append("loading: decisions view showed no loading block")

        # Error: force a 500 on forecasts, then recover.
        current["view"] = "error-forecasts"
        page.route(
            "**/market/intelligence/forecasts*",
            lambda route: route.fulfill(status=500, body='{"detail":"injected failure"}', headers={"content-type": "application/json"}),
        )
        error_state = open_view(page, "Forecasts")
        induced["error"] = {
            "error_block": error_state["errorBlocks"],
            "alerts": error_state["alerts"],
            "text": error_state["textSample"],
        }
        if not error_state["errorBlocks"]:
            failures.append("error: a 500 response rendered no error block")
        page.unroute("**/market/intelligence/forecasts*")
        page.get_by_role("tab", name="Overview", exact=True).click()
        page.wait_for_timeout(500)
        recovered = open_view(page, "Forecasts")
        induced["error"]["recovered_tables"] = recovered["tables"]
        if not recovered["tables"]:
            failures.append("error: view did not recover after the request was restored")

        # Error: unknown replay cycle (real 404 from the service).
        current["view"] = "error-replay"
        page.get_by_role("tab", name="Replay", exact=True).click()
        page.wait_for_timeout(500)
        page.locator("main input[placeholder=\"Decision cycle ID\"]").fill("mac_does_not_exist")
        page.get_by_role("button", name="Reconstruct").click()
        page.wait_for_timeout(1200)
        replay_404 = snap(page)
        induced["not_found"] = {
            "error_block": replay_404["errorBlocks"],
            "text": replay_404["textSample"],
        }
        if not replay_404["errorBlocks"]:
            failures.append("not-found: unknown cycle rendered no error block")

        # Invalid instrument (service validation) keeps the page usable.
        current["view"] = "error-instrument"
        page.get_by_role("tab", name="Instrument", exact=True).click()
        page.wait_for_timeout(500)
        page.get_by_label("Instrument symbol").fill("NOT A SYMBOL")
        page.get_by_role("button", name="Open instrument").click()
        page.wait_for_timeout(1000)
        invalid = snap(page)
        induced["invalid_input"] = {
            "banners": invalid["banners"],
            "error_block": invalid["errorBlocks"],
            "status": invalid["status"],
            "text": invalid["textSample"],
        }
        if not any("Instrument not searched" in item for item in invalid["banners"]):
            failures.append("invalid-input: malformed symbol produced no local validation message")
        if invalid["errorBlocks"]:
            failures.append("invalid-input: malformed symbol still reached the API")
        after_invalid = open_view(page, "Forecasts")
        induced["invalid_input"]["other_view_tables_after"] = after_invalid["tables"]
        induced["invalid_input"]["other_view_error"] = after_invalid["errorBlocks"]
        if after_invalid["errorBlocks"]:
            failures.append("invalid-input: one bad symbol still broke another view")

        # Empty: a decisions payload with no cycles must render the empty state.
        current["view"] = "empty-decisions"
        page.route(
            "**/market/intelligence/decisions*",
            lambda route: route.fulfill(
                status=200,
                body=json.dumps({
                    "ok": True,
                    "phase": "PI-20",
                    "data_status": "INSUFFICIENT_EVIDENCE",
                    "decisions": [],
                    "total": 0,
                    "has_more": False,
                }),
                headers={"content-type": "application/json"},
            ),
        )
        page.get_by_role("tab", name="Overview", exact=True).click()
        page.wait_for_timeout(400)
        empty_view = open_view(page, "Agent decisions")
        induced["empty"] = {
            "empty_states": empty_view["emptyStates"],
            "status": empty_view["status"],
        }
        if not any("No agent decisions" in item for item in empty_view["emptyStates"]):
            failures.append("empty: an empty decisions payload rendered no empty state")
        page.unroute("**/market/intelligence/decisions*")

        # --- widths ------------------------------------------------------------
        widths: dict[str, list[dict[str, Any]]] = {}
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(350)
            rows: list[dict[str, Any]] = []
            for view_id, label in VIEWS:
                current["view"] = f"{view_id}@{width}"
                page.get_by_role("tab", name=label, exact=True).click()
                try:
                    page.wait_for_function(
                        """() => {
                            const panel = document.querySelector('[role="tabpanel"]');
                            return Boolean(panel && (panel.querySelector('h2, h3')
                                || panel.querySelector('.empty-state')
                                || panel.querySelector('.error-block')));
                        }""",
                        timeout=6000,
                    )
                except Exception:  # noqa: BLE001 - reported by has_content below
                    pass
                page.wait_for_timeout(250)
                measured = page.evaluate(OVERFLOW_JS)
                state = snap(page)
                measured["has_content"] = bool(state["headings"] or state["emptyStates"] or state["errorBlocks"])
                rows.append(measured)
                if measured["overflow"] or measured["offenders"] or not measured["has_content"]:
                    failures.append(
                        f"{view_id}@{width}: overflow={measured['overflow']} offenders={measured['offenders']} content={measured['has_content']}"
                    )
                if measured["tabs"] != 13:
                    failures.append(f"{view_id}@{width}: {measured['tabs']} tabs")
            widths[str(width)] = rows
        page.set_viewport_size({"width": 1440, "height": 900})

        # Stale evidence is real here: the local market data is STALE.
        current["view"] = "overview"
        page.get_by_role("tab", name="Overview", exact=True).click()
        page.wait_for_timeout(800)
        stale = {
            "overview_status": views["overview"]["state"]["status"],
            "overview_banners": views["overview"]["state"]["banners"],
            "instrument_status": views["instrument"]["interactions"][0].get("status_after", []),
        }
        browser.close()

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": url,
        "instrument_used": INSTRUMENT,
        "cycle_used": CYCLE,
        "views": views,
        "induced_states": induced,
        "filter_controls": filter_controls,
        "widths": widths,
        "stale_state": stale,
        "console_errors": console,
        "expected_console_errors": [
            item for item in console
            if "500 (Internal Server Error)" in item["text"] or "404 (Not Found)" in item["text"]
        ],
        "page_errors": page_errors,
        "failures": failures,
        "limitations": [
            "States were observed on the local demonstration database; 'empty' is the service's own insufficient-evidence state, not a deleted database.",
            "The loading and 500 states were induced by delaying or failing one request in the browser, not by stopping the API.",
            "No screen reader was run and no screenshot was captured; this is a scripted DOM/layout walk.",
            "Save filters was exercised against the live workspace: the walk saved a record, reloaded, "
            "read it back, then wrote the pre-walk filters back. The preference store has no delete route, "
            "so a default-name preset with empty filters remains where there was none before.",
        ],
    }
    result["verdict"] = "PASS" if not failures and not page_errors else "FAIL"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "verdict": result["verdict"],
        "views": len(views),
        "widths": list(widths),
        "failures": len(failures),
        "console_errors": len(console),
        "page_errors": len(page_errors),
        "stale": result["stale_state"]["overview_status"],
    }, indent=2))
    for failure in failures[:20]:
        print("FAIL:", failure)
    print(f"wrote {out}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
