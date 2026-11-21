"""An absent measurement must stay absent in every NovaMarket workspace.

The Risk Intelligence fix removed the ``num(value, fallback)`` style that printed
a confident number for a field the API had reported as null.  These tests pin
the same rule for the other pages: the shared helpers are exercised
behaviourally (the real ``lib.ts`` runs in Node), and each touched page is held
to the fabrication that was removed from it and the guard that replaced it.

A static page check cannot see a rendered row, so the live browser check stays
separate; what is pinned here is that a measurement can no longer reach a page
without an absence guard.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web" / "src"
GUARDS = ("== null", "!= null", "Number.isFinite", "signTone(", "measured(", "?? ")


def _read(relative: str) -> str:
    return (WEB / relative).read_text(encoding="utf-8")


def _unguarded_lines(source: str, needle: str) -> list[str]:
    """Every line that renders ``needle`` without an absence guard on that line."""

    return [
        line.strip()
        for line in source.splitlines()
        if needle in line and not any(guard in line for guard in GUARDS)
    ]


class AbsenceHelperTests(unittest.TestCase):
    """Behavioural: ``measured()`` and ``signTone()`` from the real lib.ts."""

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        lib_uri = (WEB / "lib.ts").resolve().as_uri()
        script = f"""
import {{ measured, signTone }} from {json.dumps(lib_uri)};
const values = [
  ["null", null],
  ["undefined", undefined],
  ["empty", ""],
  ["nan", Number.NaN],
  ["text", "not a number"],
  ["zero", 0],
  ["negative", -1.5],
  ["string", "1234.5"],
  ["commas", "1,234.5"],
];
console.log(JSON.stringify(values.map(([name, value]) => ({{
  name,
  measured0: measured(value, 0),
  measured2: measured(value, 2),
  measured4: measured(value, 4),
  tone: signTone(value),
}}))));
"""
        completed = subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(REPO / "web"),
        )
        if completed.returncode != 0:
            raise AssertionError(f"node could not read lib.ts: {completed.stderr[-800:]}")
        rows = json.loads(completed.stdout.strip().splitlines()[-1])
        cls.rows = {row["name"]: row for row in rows}

    def test_an_absent_value_has_no_number(self) -> None:
        for name in ("null", "undefined", "empty", "nan", "text"):
            row = self.rows[name]
            self.assertEqual(row["measured2"], "—", name)
            self.assertEqual(row["measured0"], "—", name)
            self.assertEqual(row["tone"], "muted", name)

    def test_a_measured_zero_is_still_a_measured_zero(self) -> None:
        # The bug was null becoming 0, not 0 itself: a real zero must render.
        zero = self.rows["zero"]
        self.assertEqual(zero["measured2"], "0.00")
        self.assertEqual(zero["measured0"], "0")
        self.assertEqual(zero["tone"], "positive")

    def test_signs_and_digits_follow_the_measurement(self) -> None:
        self.assertEqual(self.rows["negative"]["measured2"], "-1.50")
        self.assertEqual(self.rows["negative"]["tone"], "negative")
        self.assertEqual(self.rows["string"]["measured2"], "1234.50")
        self.assertEqual(self.rows["commas"]["measured4"], "1234.5000")
        self.assertEqual(self.rows["commas"]["tone"], "positive")


class WorkbenchSharedAbsenceTests(unittest.TestCase):
    def test_a_missing_momentum_score_is_not_a_zero_meter(self) -> None:
        source = _read("pages/WorkbenchShared.tsx")
        self.assertNotIn("num(value, 0)", source)
        self.assertIn('if (!Number.isFinite(numeric)) return <span className="muted">—</span>;', source)


class OrderBlockAbsenceTests(unittest.TestCase):
    def test_direction_and_participation_need_a_measurement(self) -> None:
        source = _read("pages/OrderBlock.tsx")
        self.assertNotIn("function changeClass", source)
        self.assertNotIn("Number(row.change_in_open_interest ?? 0)", source)
        self.assertEqual(_unguarded_lines(source, "row.bullish_participation_pct)"), [])
        self.assertEqual(_unguarded_lines(source, "row.bearish_participation_pct)"), [])
        self.assertIn("signTone(row.average_change_pct)", source)
        self.assertIn("signTone(row.change_in_open_interest)", source)
        self.assertIn('xray.average_change_pct == null ? "neutral"', source)


class TradingWatchAbsenceTests(unittest.TestCase):
    def test_an_unmeasured_change_carries_no_direction(self) -> None:
        source = _read("pages/TradingWatch.tsx")
        self.assertNotIn('num(row?.change_pct) >= 0 ? "bullish"', source)
        self.assertIn(': "unknown";', source)
        self.assertIn("signTone(row.CE?.change_in_open_interest)", source)
        self.assertIn("signTone(row.PE?.change_in_open_interest)", source)
        self.assertIn("signTone(item.average_change_pct)", source)
        self.assertIn('className={`delta ${signTone(row.change_pct)}`}', source)


class PortfolioManagementAbsenceTests(unittest.TestCase):
    def test_positions_and_exposure_do_not_invent_zero(self) -> None:
        source = _read("pages/PortfolioManagement.tsx")
        self.assertNotIn("num(row.unrealized_pnl, 0) < 0", source)
        self.assertEqual(_unguarded_lines(source, "num(row.quantity, 0)"), [])
        self.assertEqual(_unguarded_lines(source, "num(row.evidence_score, 0)"), [])
        self.assertEqual(_unguarded_lines(source, "num(exposure.data?.concentration?.hhi"), [])
        self.assertEqual(_unguarded_lines(source, "num(exposure.data?.diversification?.score"), [])
        self.assertIn('row.quantity == null ? "—"', source)
        self.assertIn("signTone(row.unrealized_pnl)", source)
        self.assertIn('exposure.data?.concentration?.hhi == null ? "—"', source)
        self.assertIn('exposure.data?.diversification?.score == null ? "—"', source)


class ForecastModelsAbsenceTests(unittest.TestCase):
    def test_delay_and_contributions_keep_absence(self) -> None:
        source = _read("pages/ForecastModels.tsx")
        self.assertEqual(_unguarded_lines(source, "num(row.observation_delay_seconds, 0)"), [])
        self.assertEqual(_unguarded_lines(source, "num(item.contribution, 0)"), [])
        self.assertNotIn("num(item.mae_increase, 0)", source)
        self.assertIn('row.observation_delay_seconds == null ? "—"', source)
        self.assertIn("measured(item.mae_increase, 6)", source)


class RlResearchAbsenceTests(unittest.TestCase):
    def test_drawdown_and_return_tone_need_a_measurement(self) -> None:
        source = _read("pages/RLResearch.tsx")
        self.assertIn("const drawdown = portfolio.drawdown ?? summary.max_drawdown;", source)
        self.assertIn('drawdown == null ? "—"', source)
        self.assertIn('summary.total_return == null ? "neutral"', source)
        self.assertNotIn('tone={num(summary.total_return) < 0 ? "bad" : "good"}', source)


class PredictionIntelligenceAbsenceTests(unittest.TestCase):
    def test_counters_show_absence_instead_of_zero(self) -> None:
        source = _read("pages/PredictionIntelligence.tsx")
        self.assertNotIn("num(state.data.predictions?.active_forecasts, 0)", source)
        self.assertNotIn("num(state.data.decisions?.cycles, 0)", source)
        self.assertNotIn("num(data.current_authoritative?.stale_prices, 0)}", source)
        self.assertNotIn("num(data.point_in_time?.future_forecasts_excluded, 0)", source)
        self.assertIn('state.data.predictions?.active_forecasts ?? "—"', source)
        self.assertIn('data.current_authoritative?.stale_prices ?? "—"', source)
        self.assertIn('data.point_in_time?.future_forecasts_excluded ?? "—"', source)
        self.assertIn('row.contribution == null ? "—"', source)


class DiscoveryAbsenceTests(unittest.TestCase):
    def test_a_candidate_without_a_score_is_not_scored_zero(self) -> None:
        source = _read("pages/Intelligence.tsx")
        self.assertIn("function scoreOf(row: any): number | null", source)
        self.assertNotIn("scoreOf(row).toFixed(1)", source)
        self.assertIn("function scoreText(row: any): string", source)
        self.assertIn("function scoreOutOf(row: any): string", source)
        self.assertIn("(scoreOf(row) ?? -1) >= 70", source)
        self.assertIn("sortValue: (row) => scoreOf(row) ?? -1", source)


class DashboardAbsenceTests(unittest.TestCase):
    def test_overall_pnl_tone_is_not_green_without_a_measurement(self) -> None:
        source = _read("pages/Dashboard.tsx")
        self.assertNotIn('tone={num(summary.portfolio_pnl_inr) < 0 ? "bad" : "good"}', source)
        self.assertIn('summary.portfolio_pnl_inr == null ? "neutral"', source)


class PortfolioOptimizationAbsenceTests(unittest.TestCase):
    def test_change_columns_and_pnl_tones_need_a_measurement(self) -> None:
        source = _read("pages/PortfolioOptimization.tsx")
        self.assertNotIn('num(row.weight_difference) >= 0 ? "delta positive"', source)
        self.assertNotIn('num(row.portfolio_value_change) < 0 ? "delta negative"', source)
        self.assertNotIn('tone={num(snapshot.unrealized_pnl) >= 0 ? "good" : "bad"}', source)
        self.assertIn("signTone(row.weight_difference)", source)
        self.assertIn("signTone(row.portfolio_value_change)", source)
        self.assertIn('snapshot.unrealized_pnl == null ? "neutral"', source)
        self.assertIn('snapshot.realized_pnl == null ? "neutral"', source)


class PortfoliosAbsenceTests(unittest.TestCase):
    def test_weights_returns_and_proposals_keep_absence(self) -> None:
        source = _read("pages/Portfolios.tsx")
        self.assertEqual(_unguarded_lines(source, "pct(num(row.weight) * 100)"), [])
        self.assertEqual(_unguarded_lines(source, "pct(num(row.turnover) * 100)"), [])
        self.assertEqual(_unguarded_lines(source, "pct(num(pendingProposal.drift) * 100)"), [])
        self.assertEqual(_unguarded_lines(source, "pct(num(metrics.return_pct))"), [])
        self.assertEqual(_unguarded_lines(source, "pct(num(valuation.cash_weight)"), [])
        self.assertEqual(_unguarded_lines(source, "num(valuation.unrealized_pnl) < 0"), [])
        self.assertIn('valuation.cash_weight == null ? "—"', source)
        self.assertIn('metrics.return_pct == null ? "—"', source)
        self.assertIn('row.turnover == null ? "—"', source)
        self.assertIn('pendingProposal.drift == null ? "—"', source)


class TradingAbsenceTests(unittest.TestCase):
    def test_policy_limits_and_replay_metrics_keep_absence(self) -> None:
        source = _read("pages/Trading.tsx")
        self.assertEqual(_unguarded_lines(source, "pctUnsigned(num(policy.max_position_weight)"), [])
        self.assertEqual(_unguarded_lines(source, "pctUnsigned(num(policy.max_sector_weight)"), [])
        self.assertEqual(_unguarded_lines(source, "pctUnsigned(num(row.max_position_weight)"), [])
        self.assertEqual(_unguarded_lines(source, "pctUnsigned(num(row.max_drawdown_pct))"), [])
        self.assertEqual(_unguarded_lines(source, "pct(num(metrics.total_return) * 100)"), [])
        self.assertEqual(_unguarded_lines(source, "pct(num(row.loss_pct ?? row.impact_pct"), [])
        self.assertIn('policy.max_position_weight == null ? "—"', source)
        self.assertIn('metrics.total_return == null ? "—"', source)


class PredictionEvaluationAbsenceTests(unittest.TestCase):
    def test_backtest_metrics_keep_absence(self) -> None:
        source = _read("pages/PredictionEvaluation.tsx")
        self.assertEqual(_unguarded_lines(source, "pct(num(backtestMetrics.total_return) * 100)"), [])
        self.assertEqual(
            _unguarded_lines(source, "pct(num(backtestMetrics.max_drawdown_pct ?? backtestMetrics.max_drawdown) * 100)"),
            [],
        )
        self.assertIn('backtestMetrics.total_return == null ? "—"', source)
        self.assertIn('backtestMetrics.max_drawdown_pct == null && backtestMetrics.max_drawdown == null', source)


class RiskIntelligencePatternTests(unittest.TestCase):
    """The page the user fixed stays honest about an absent measurement."""

    def test_number_text_refuses_absence(self) -> None:
        source = _read("pages/RiskIntelligence.tsx")
        self.assertIn('if (value === null || value === undefined || value === "") return "—";', source)
        self.assertNotIn("num(value, 0).toFixed", source)


if __name__ == "__main__":
    unittest.main()
