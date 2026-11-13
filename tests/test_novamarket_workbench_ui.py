"""Contract tests for the standalone OrderBlock and TradingWatch routes.

These are source-level checks on the React shell. They deliberately pin the
wiring a build alone cannot prove: Overview is still the dashboard, both hash
routes and navigation entries exist, both pages read the shared NM-14 APIs,
they share one instrument identity, and the analysis pages never call an
execution endpoint.

Behavioural API and browser checks remain separate; this file is fast enough to
run with the rest of the Python contract suite.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web" / "src"


def _read(relative: str) -> str:
    return (WEB / relative).read_text(encoding="utf-8")


class WorkbenchFrontendContractTests(unittest.TestCase):
    def test_overview_remains_the_existing_dashboard(self) -> None:
        app = _read("App.tsx")
        self.assertIn('case "dashboard": return <DashboardPage />;', app)
        self.assertIn('case "market": return <MarketPage />;', app)

    def test_both_new_routes_are_wired(self) -> None:
        app = _read("App.tsx")
        self.assertIn('case "orderblock": return <OrderBlockPage />;', app)
        self.assertIn('case "tradingwatch": return <TradingWatchPage />;', app)
        nav = _read("nav.ts")
        self.assertIn('label: "Trading desks"', nav)
        self.assertIn('{ id: "orderblock", label: "OrderBlock"', nav)
        self.assertIn('{ id: "tradingwatch", label: "TradingWatch"', nav)
        self.assertIn('orderblock: "OrderBlock"', nav)
        self.assertIn('tradingwatch: "TradingWatch"', nav)

    def test_both_pages_use_the_existing_workbench_endpoints(self) -> None:
        orderblock = _read("pages/OrderBlock.tsx")
        tradingwatch = _read("pages/TradingWatch.tsx")
        self.assertIn('"/market/orderblock?interval=1d&limit=50"', orderblock)
        self.assertIn('`/market/orderblock/${encodeURIComponent(selected)}?interval=1d`', orderblock)
        self.assertIn('"/market/tradingwatch?interval=1d&limit=50"', tradingwatch)
        self.assertIn("/market/tradingwatch/replay/", tradingwatch)
        self.assertIn("WatchlistPanel", tradingwatch)
        self.assertIn("WatchlistButton", orderblock)
        self.assertIn("WatchlistButton", tradingwatch)

    def test_the_two_desks_share_one_canonical_selection(self) -> None:
        shared = _read("pages/WorkbenchShared.tsx")
        orderblock = _read("pages/OrderBlock.tsx")
        tradingwatch = _read("pages/TradingWatch.tsx")
        self.assertIn('DESK_SELECTION_KEY = "novamarket.deskInstrument"', shared)
        self.assertIn("useDeskInstrument", shared)
        self.assertIn("useDeskInstrument", orderblock)
        self.assertIn("useDeskInstrument", tradingwatch)
        self.assertNotIn("localStorage.setItem", orderblock)
        self.assertNotIn("localStorage.setItem", tradingwatch)

    def test_analysis_pages_do_not_execute_actions(self) -> None:
        for relative in ("pages/OrderBlock.tsx", "pages/TradingWatch.tsx"):
            source = _read(relative)
            self.assertNotIn("apiPost", source, relative)
            self.assertNotIn("/market/execute", source, relative)
            self.assertNotIn("/market/orders", source, relative)
            self.assertNotIn("place_order", source, relative)

    def test_derivatives_show_the_official_source_or_a_blocked_state(self) -> None:
        orderblock = _read("pages/OrderBlock.tsx")
        tradingwatch = _read("pages/TradingWatch.tsx")
        self.assertIn("F&O scanner", orderblock)
        self.assertIn("fno.reason", orderblock)
        self.assertIn("contracts_registered", orderblock)
        self.assertIn("fnoRows", orderblock)
        self.assertIn("mapping_status", orderblock)
        self.assertIn("expired_excluded", orderblock)
        self.assertIn("Official NSE F&O · futures scanner", orderblock)
        self.assertIn("Index options", tradingwatch)
        self.assertIn("Official NSE F&O bhavcopy", tradingwatch)
        self.assertIn("options.fields_unavailable", tradingwatch)
        self.assertIn("options.reason", tradingwatch)
        self.assertIn("No official option-chain source is configured", tradingwatch)

    def test_replay_draws_only_the_cursor_prefix(self) -> None:
        tradingwatch = _read("pages/TradingWatch.tsx")
        self.assertIn("replayBars.slice(0, Math.min(cursor + 1, replayBars.length))", tradingwatch)
        self.assertIn("candleBars({ bars: shownBars })", tradingwatch)
        self.assertIn("look_ahead_guard", tradingwatch)
        self.assertIn("Historical · no look-ahead", tradingwatch)

    def test_shared_table_headers_expose_scope_and_sort_state(self) -> None:
        ui = _read("components/ui.tsx")
        self.assertIn('scope="col"', ui)
        self.assertIn("aria-sort=", ui)

    def test_dashboard_market_pulse_uses_quoted_observations(self) -> None:
        dashboard = _read("pages/Dashboard.tsx")
        self.assertIn('useApi<any>("/market/screener?limit=12&available_only=true")', dashboard)
        self.assertNotIn('"/market/instruments?limit=6"', dashboard)
        self.assertIn("arrayFrom(pulse.data, \"rows\")", dashboard)
        self.assertIn("quoteValue(quote.data) ?? instrument.price", dashboard)

    def test_dashboard_horizon_status_is_not_the_first_book_status(self) -> None:
        dashboard = _read("pages/Dashboard.tsx")
        self.assertIn("function horizonStatus(statuses: unknown): string", dashboard)
        self.assertIn("return values.every((value) => value === values[0]) ? values[0] : \"mixed\";", dashboard)
        self.assertIn("<StatusBadge value={horizonStatus(item.statuses)} />", dashboard)
        self.assertNotIn("(item.statuses || [])[0]", dashboard)

    def test_line_chart_expands_a_flat_series_for_readable_ticks(self) -> None:
        charts = _read("components/charts.tsx")
        self.assertIn("if (min === max) {", charts)
        self.assertIn("const pad = Math.abs(min) * 0.05 || 1;", charts)

    def test_shared_modal_and_skip_link_are_keyboard_safe(self) -> None:
        ui = _read("components/ui.tsx")
        self.assertIn("previousFocusRef", ui)
        self.assertIn('event.key !== "Tab"', ui)
        self.assertIn("previous.focus()", ui)
        self.assertIn("aria-describedby", ui)
        app = _read("App.tsx")
        self.assertIn('href="#main-content"', app)
        self.assertIn('id="main-content"', app)


class AutonomyFrontendContractTests(unittest.TestCase):
    """Pin the NM-R1 remediation contracts that a build cannot prove."""

    def setUp(self) -> None:
        self.autonomy = _read("pages/Autonomy.tsx")

    def test_decisions_explain_the_mode_gate_instead_of_only_disabling(self) -> None:
        self.assertIn('mode === "paused"', self.autonomy)
        self.assertIn("New decision runs are blocked while paused.", self.autonomy)
        self.assertIn("Start monitoring before running the decision pipeline.", self.autonomy)
        self.assertIn("Decision run unavailable.", self.autonomy)
        self.assertIn("role=\"status\"", self.autonomy)

    def test_decision_button_is_disabled_by_the_explained_gate(self) -> None:
        self.assertIn(
            "disabled={!controlsEnabled || Boolean(decisionBlockedReason)}",
            self.autonomy,
        )
        self.assertIn("Run decision pipeline", self.autonomy)

    def test_reliability_panel_uses_the_existing_report_and_recovery_routes(self) -> None:
        self.assertIn('useApi<any>("/market/autonomy/reliability"', self.autonomy)
        self.assertIn('"/market/autonomy/recover"', self.autonomy)
        for label in (
            "Pipeline health",
            "Scheduler health",
            "Lease expires",
            "Unresolved reconciliation records",
            "Recover and disable",
            "Refresh reliability",
            "No unresolved reconciliation records.",
        ):
            self.assertIn(label, self.autonomy)

    def test_reliability_panel_has_loading_empty_failure_and_stale_states(self) -> None:
        self.assertIn("Reading the pipeline reliability report", self.autonomy)
        self.assertIn("Reliability refresh failed", self.autonomy)
        self.assertIn("The last authoritative report remains visible below.", self.autonomy)
        self.assertIn("reliability.error && !reliability.data", self.autonomy)
        self.assertIn("reliability.loading && !reliability.data", self.autonomy)

    def test_recovery_is_confirmed_operator_only_and_never_persisted(self) -> None:
        self.assertIn("Recover autonomy and return to disabled?", self.autonomy)
        self.assertIn("It never resumes autonomy", self.autonomy)
        self.assertIn("apiPost(", self.autonomy)
        self.assertIn("operatorToken", self.autonomy)
        self.assertNotIn("localStorage", self.autonomy)
        self.assertNotIn("sessionStorage", self.autonomy)


if __name__ == "__main__":
    unittest.main()
