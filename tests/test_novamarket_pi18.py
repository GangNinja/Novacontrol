"""Focused PI-18 portfolio-optimization and capital-allocation tests."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from novacontrol.api.app import create_app
from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.novamarket.multiagent import AgentMessage, AgentRequest, MessageType
from novacontrol.novamarket.multiagent.agents import (
    DEFINITION_BY_ID,
    PortfolioIntelligenceAgent,
)
from novacontrol.novamarket.optimization import (
    AllocationMethod,
    OptimizationConfig,
    OptimizationInfeasibleError,
    OptimizationNotFoundError,
    OptimizationService,
    OptimizationValidationError,
)
from novacontrol.novamarket.optimization.analytics import (
    aligned_price_matrix,
    diversification_metrics,
    estimate_covariance,
    log_returns,
    risk_metrics,
)
from novacontrol.novamarket.optimization.models import OptimizationStateError
from novacontrol.novamarket.service import NovaMarketService
from novacontrol.novamarket.optimization.solvers import solve_weights

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
INSTRUMENTS = ["XNSE:AAA", "XNSE:BBB", "XNSE:CCC"]
SECTORS = ["technology", "financials", "energy"]


def _bars(
    count: int = 80,
    *,
    instrument_index: int = 0,
    flat: bool = False,
    volume: float | None = 250_000,
) -> list[dict[str, object]]:
    prices = [100.0 + instrument_index * 40.0, 100.0 + instrument_index * 40.0]
    rows: list[dict[str, object]] = []
    for index in range(count):
        previous = prices[-1]
        change = 0.0 if flat else 0.0012 + 0.006 * np.sin((index + instrument_index) / 4.0)
        price = previous * (1.0 + change + (0.0002 * instrument_index))
        prices.append(price)
        rows.append(
            {
                "observed_at": (NOW - timedelta(days=count - index)).isoformat(),
                "close": round(price, 6),
                "volume": volume,
            }
        )
    return rows


def context(
    *,
    cash: float = 400_000.0,
    total: float = 1_000_000.0,
    holdings: bool = True,
    include_forecasts: bool = True,
    forecast_horizon: str = "1d",
    forecast_age_hours: int = 1,
    expected_return: float = 0.002,
    bars_count: int = 80,
    identical_returns: bool = False,
    one_flat: bool = False,
    missing_volume: bool = False,
    stale_holding: bool = False,
    missing_bars: str = "",
) -> dict[str, object]:
    instruments = list(INSTRUMENTS)
    bars = {
        instrument: _bars(
            bars_count,
            instrument_index=index,
            flat=one_flat and index == 0,
            volume=None if missing_volume and index == 0 else 250_000,
        )
        for index, instrument in enumerate(instruments)
        if instrument != missing_bars
    }
    if identical_returns:
        base = bars[INSTRUMENTS[0]]
        for instrument in INSTRUMENTS[1:]:
            bars[instrument] = [
                {
                    "observed_at": row["observed_at"],
                    "close": float(row["close"]) * (1.0 + 0.001 * INSTRUMENTS.index(instrument)),
                    "volume": row["volume"],
                }
                for row in base
            ]
    forecasts: dict[str, object] = {}
    if include_forecasts:
        forecasts = {
            instrument: [
                {
                    "instrument_id": instrument,
                    "horizon": forecast_horizon,
                    "expected_return": expected_return + 0.0005 * index,
                    "generated_at": (NOW - timedelta(hours=forecast_age_hours)).isoformat(),
                    "validity": "valid",
                    "quality": "high",
                }
            ]
            for index, instrument in enumerate(instruments)
        }
    return {
        "portfolio": {
            "portfolio": {"portfolio_id": "pf_default", "updated_at": NOW.isoformat()},
            "valuation": {
                "cash": cash,
                "total_value": total,
                "invested": max(0.0, total - cash),
                "realized_pnl": 2_000.0,
                "unrealized_pnl": 3_000.0,
            },
            "holdings": (
                [
                    {
                        "instrument_id": instrument,
                        "quantity": total * 0.18 / 100.0,
                        "average_cost": 90.0,
                        "last_price": 100.0,
                        "market_value": total * 0.18,
                        "weight": 0.18,
                        "sector": SECTORS[index],
                        "asset_class": "equity",
                        "stale": stale_holding and index == 0,
                        "priced_at": NOW.isoformat(),
                    }
                    for index, instrument in enumerate(instruments)
                ]
                if holdings
                else []
            ),
        },
        "bars": bars,
        "instruments": {
            instrument: {
                "instrument_id": instrument,
                "exchange": "XNSE",
                "sector": SECTORS[index],
                "asset_class": "equity",
                "trading_status": "active",
            }
            for index, instrument in enumerate(instruments)
        },
        "forecasts": forecasts,
        "risk": {"policy": {"version": "nm08-test"}, "outcome": "accepted"},
        "interval": "1d",
    }


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "portfolio_id": "pf_default",
        "horizon": "1d",
        "instrument_ids": list(INSTRUMENTS),
        "max_volatility": 5.0,
        "min_history_observations": 30,
        "max_instrument_weight": 0.5,
        "max_sector_weight": 0.90,
        "min_cash_reserve": 0.05,
        "max_turnover": 1.0,
        "rebalance_threshold": 0.01,
        "liquidity_policy": "warn",
        "covariance_method": "shrinkage",
    }
    base.update(overrides)
    return base


def service(tmp_path: Path, ctx: dict[str, object] | None = None) -> OptimizationService:
    return OptimizationService(
        tmp_path / "optimization.sqlite3",
        context_loader=lambda request: ctx or context(),
        clock=lambda: NOW,
    )


def run_ok(tmp_path: Path, **overrides: object) -> dict[str, object]:
    svc = service(tmp_path)
    try:
        result = svc.run(payload(**overrides))
        assert result["ok"] is True, result
        return result["run"]
    finally:
        svc.close()


# -- architecture, input validation and isolation -----------------------------


def test_service_initialization_status_contract_and_health(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        assert svc.status()["phase"] == "PI-18"
        assert svc.contract()["authority"]["risk"] == "NM-08"
        assert svc.health()["health"] == "healthy"
        assert svc.status()["order_submission_available"] is False
    finally:
        svc.close()


def test_portfolio_snapshot_is_read_only_and_preserves_authoritative_values(
    tmp_path: Path,
) -> None:
    ctx = context()
    before = repr(ctx)
    run = run_ok(tmp_path)
    assert run["portfolio_snapshot"]["total_value"] == 1_000_000.0
    assert run["portfolio_snapshot"]["cash"] == 400_000.0
    assert run["financial_mutation"] is False
    assert repr(ctx) == before


def test_pi04_regime_is_surfaced_with_an_uncertainty_warning(tmp_path: Path) -> None:
    ctx = context()
    ctx["regimes"] = {
        INSTRUMENTS[0]: {
            "regime_id": "rg_high_vol",
            "primary_regime": "high_volatility",
            "secondary_conditions": ["weak_down"],
            "as_of": NOW.isoformat(),
            "method": "pi04-regime-1.0",
        }
    }
    svc = service(tmp_path, ctx)
    try:
        result = svc.run(payload())
    finally:
        svc.close()
    run = result["run"]
    regime = run["market_regime"][INSTRUMENTS[0]]
    assert regime["regime_id"] == "rg_high_vol"
    assert regime["primary_regime"] == "high_volatility"
    assert regime["secondary_conditions"] == ["weak_down"]
    assert any("high_volatility" in warning for warning in run["warnings"])
    assert run["status"] == "completed_with_warnings"


def test_input_validation_excludes_missing_bars_and_stale_prices(tmp_path: Path) -> None:
    svc = service(tmp_path, context(missing_bars="XNSE:CCC"))
    try:
        result = svc.validate_inputs(payload(), as_of=NOW)
        assert result["valid"] is True
        assert len(result["eligible"]) == 2
        assert any(row["instrument_id"] == "XNSE:CCC" for row in result["excluded"])
        svc.close()
    finally:
        svc.close()
    stale_service = service(tmp_path / "stale", context(stale_holding=True))
    try:
        stale = stale_service.validate_inputs(payload(), as_of=NOW)
        assert stale["valid"] is False
        assert stale["status"] == "completed_with_warnings"
    finally:
        stale_service.close()


def test_eligibility_requires_exchange_and_horizon_compatible_forecast(
    tmp_path: Path,
) -> None:
    svc = service(
        tmp_path,
        context(include_forecasts=True, forecast_horizon="5d", forecast_age_hours=1),
    )
    try:
        result = svc.validate_inputs(payload(horizon="1d"), as_of=NOW)
        assert result["valid"] is False
        assert result["status"] == "insufficient_data"
        assert all("horizon" in " ".join(row["reasons"]).lower() for row in result["excluded"])
    finally:
        svc.close()


def test_stale_forecast_and_missing_forecast_fail_explicitly(tmp_path: Path) -> None:
    svc = service(tmp_path, context(forecast_age_hours=24 * 10))
    try:
        result = svc.run(payload())
        assert result["ok"] is False
        assert result["run"]["status"] == "insufficient_data"
    finally:
        svc.close()
    missing = service(tmp_path / "missing", context(include_forecasts=False))
    try:
        result = missing.run(payload())
        assert result["ok"] is False
        assert result["run"]["status"] == "insufficient_data"
    finally:
        missing.close()


def test_zero_available_cash_no_positions_and_single_instrument(tmp_path: Path) -> None:
    zero_cash = run_ok(tmp_path, **{})
    assert zero_cash["target_weights"]
    assert zero_cash["cash_value"] >= 0
    # A zero-cash portfolio still has positive total value and cannot spend cash.
    ctx = context(cash=0.0, total=500_000.0)
    svc = service(tmp_path / "zero", ctx)
    try:
        result = svc.run(payload())
        assert result["ok"] is True
        assert result["run"]["cash_value"] >= 0
    finally:
        svc.close()
    no_positions = service(tmp_path / "none", context(holdings=False))
    try:
        assert no_positions.run(payload())["ok"] is True
    finally:
        no_positions.close()
    one = service(
        tmp_path / "one",
        context(),
    )
    try:
        result = one.run(payload(instrument_ids=["XNSE:AAA"]))
        assert result["ok"] is True
        assert len(result["run"]["target_weights"]) == 1
    finally:
        one.close()


# -- analytics -----------------------------------------------------------------


def test_aligned_returns_covariance_and_correlation_are_numerically_valid() -> None:
    from novacontrol.novamarket.optimization.models import PriceSeries

    observed = tuple(NOW - timedelta(days=30 - index) for index in range(30))
    series = {
        instrument: PriceSeries(
            instrument_id=instrument,
            interval="1d",
            observed_at=observed,
            close=tuple(100.0 + index + offset for index in range(30)),
            volume=tuple(1000.0 for _ in range(30)),
        )
        for offset, instrument in enumerate(INSTRUMENTS)
    }
    selected, timestamps, prices, warnings = aligned_price_matrix(
        series, INSTRUMENTS, minimum_observations=10
    )
    assert selected == INSTRUMENTS
    assert len(timestamps) == 30
    returns, _ = log_returns(prices)
    covariance = estimate_covariance(
        returns,
        selected,
        method="shrinkage",
        interval="1d",
        periods_per_year=252,
        shrinkage=0.1,
    )
    assert covariance.observations == 29
    matrix = np.asarray(covariance.matrix)
    assert np.allclose(matrix, matrix.T)
    assert np.all(np.linalg.eigvalsh(matrix) > 0)
    assert np.allclose(np.diag(covariance.correlation), 1.0)
    assert not warnings


def test_invalid_and_degenerate_covariance_is_handled() -> None:
    with pytest.raises(OptimizationValidationError):
        estimate_covariance(
            np.array([[np.nan, 0.1], [0.1, 0.2]]),
            ["A", "B"],
            method="sample",
            interval="1d",
            periods_per_year=252,
        )
    flat = np.zeros((20, 2))
    result = estimate_covariance(
        flat,
        ["A", "B"],
        method="sample",
        interval="1d",
        periods_per_year=252,
    )
    assert result.condition_number >= 1.0
    assert result.warnings


def test_risk_and_diversification_metrics_have_documented_meanings() -> None:
    from novacontrol.novamarket.optimization.models import CovarianceResult

    covariance = CovarianceResult(
        instruments=("A", "B"),
        matrix=((0.01, 0.002), (0.002, 0.02)),
        correlation=((1.0, 0.1414), (0.1414, 1.0)),
        observations=100,
        method="shrinkage",
        interval="1d",
        periods_per_year=252,
        condition_number=2.1,
    )
    returns = np.array([[0.01, -0.005], [-0.004, 0.006], [0.002, 0.001]] * 10)
    risk = risk_metrics(
        weights={"A": 0.6, "B": 0.4},
        covariance=covariance,
        return_matrix=returns,
        expected_returns={"A": 0.001, "B": 0.002},
        horizon_periods=1,
        risk_free_rate=0.05,
        sectors={"A": "tech", "B": "finance"},
    )
    assert risk["annualized_volatility"] > 0
    assert risk["sharpe_ratio"] is not None
    assert risk["cvar_95"] <= risk["var_95"]
    diversification = diversification_metrics(
        weights={"A": 0.9, "B": 0.1},
        covariance=covariance,
        sectors={"A": "tech", "B": "finance"},
        max_sector_weight=0.8,
    )
    assert diversification["effective_holdings"] < 2
    assert diversification["definition"]
    assert diversification["warnings"]


def test_sector_cap_warning_uses_portfolio_exposure_not_invested_share() -> None:
    from novacontrol.novamarket.optimization.models import CovarianceResult

    covariance = CovarianceResult(
        instruments=("A",),
        matrix=((0.0004,),),
        correlation=((1.0,),),
        observations=40,
        method="sample",
        interval="1d",
        periods_per_year=252,
        condition_number=1.0,
    )
    comfortably_capped = diversification_metrics(
        weights={"A": 0.18},
        covariance=covariance,
        sectors={"A": "unknown"},
        max_sector_weight=0.30,
    )
    assert "target sector weight exceeds the configured cap" not in comfortably_capped["warnings"]
    assert comfortably_capped["sector_weights"] == {"unknown": 1.0}
    over_cap = diversification_metrics(
        weights={"A": 0.40},
        covariance=covariance,
        sectors={"A": "unknown"},
        max_sector_weight=0.30,
    )
    assert "target sector weight exceeds the configured cap" in over_cap["warnings"]


# -- solvers and constraints ---------------------------------------------------


def test_every_allocation_method_returns_long_only_feasible_weights(
    tmp_path: Path,
) -> None:
    for method in (
        AllocationMethod.MEAN_VARIANCE.value,
        AllocationMethod.MIN_VARIANCE.value,
        AllocationMethod.RISK_PARITY.value,
        AllocationMethod.EQUAL_WEIGHT.value,
        AllocationMethod.INVERSE_VOLATILITY.value,
        AllocationMethod.CURRENT.value,
    ):
        run = run_ok(tmp_path / method, method=method)
        weights = run["target_weights"]
        assert all(float(value) >= 0 for value in weights.values()), method
        assert sum(float(value) for value in weights.values()) <= 0.951, method
        assert all(float(value) <= 0.501 for value in weights.values()), method


def test_cash_reserve_instrument_sector_and_turnover_constraints(
    tmp_path: Path,
) -> None:
    run = run_ok(
        tmp_path,
        max_instrument_weight=0.3,
        max_sector_weight=0.55,
        sector_limits={"technology": 0.2},
        min_cash_reserve=0.15,
        max_turnover=0.4,
    )
    weights = run["target_weights"]
    assert sum(float(value) for value in weights.values()) <= 0.851
    assert all(float(value) <= 0.301 for value in weights.values())
    assert (
        sum(float(weight) for instrument, weight in weights.items() if instrument == "XNSE:AAA")
        <= 0.201
    )
    assert run["allocation"]
    assert run["solver"]["fallback_used"] is False


def test_max_positions_leaves_capital_in_cash_instead_of_relaxing(tmp_path: Path) -> None:
    run = run_ok(tmp_path, max_positions=1, max_instrument_weight=0.5)
    assert len(run["target_weights"]) == 1
    assert run["cash_reserve"] > 0.4


def test_liquidity_participation_cap_and_missing_liquidity_policy(
    tmp_path: Path,
) -> None:
    run = run_ok(tmp_path / "cap", max_participation_rate=0.01)
    # Each asset has roughly 100k * 250k? -> small cap; every target is bounded by it.
    for row in run["allocation"]:
        assert row["liquidity_status"] in {"estimated", "missing"}
        if row["liquidity_status"] == "estimated":
            assert float(row["target_capital"]) <= float(row["liquidity_cap_value"]) + 1e-6
    missing = service(tmp_path / "missing-liq", context(missing_volume=True))
    try:
        excluded = missing.run(payload(liquidity_policy="exclude"))
        assert excluded["ok"] is True
        assert any(row["instrument_id"] == "XNSE:AAA" for row in excluded["run"]["excluded"])
        assert "XNSE:AAA" not in excluded["run"]["target_weights"]
        allowed = missing.run(payload(liquidity_policy="warn"))
        assert allowed["ok"] is True
    finally:
        missing.close()


def test_high_correlation_is_flagged_not_hidden(tmp_path: Path) -> None:
    svc = service(tmp_path, context(identical_returns=True))
    try:
        result = svc.run(payload())
        assert result["ok"] is True
        warnings = " ".join(result["run"]["warnings"]).lower()
        assert (
            "correlation" in warnings
            or result["run"]["diversification"]["average_correlation"] > 0.65
        )
    finally:
        svc.close()


def test_negative_and_zero_expected_returns_are_preserved(tmp_path: Path) -> None:
    negative = service(tmp_path / "negative", context(expected_return=-0.02))
    try:
        result = negative.run(payload())
        assert result["ok"] is True
        assert any(
            float(row["adjusted_return"]) < 0 for row in result["run"]["expected_returns"].values()
        )
    finally:
        negative.close()
    zero = service(tmp_path / "zero-ret", context(expected_return=0.0))
    try:
        result = zero.run(payload())
        assert result["ok"] is True
        assert (
            min(float(row["adjusted_return"]) for row in result["run"]["expected_returns"].values())
            == 0.0
        )
    finally:
        zero.close()


# -- horizons, rebalancing, scenarios and backtests -----------------------------


def test_multi_horizon_runs_preserve_separate_horizon_inputs(tmp_path: Path) -> None:
    for horizon in ("5m", "1d", "30d"):
        svc = service(
            tmp_path / horizon,
            context(forecast_horizon=horizon, bars_count=80),
        )
        try:
            result = svc.run(
                payload(
                    horizon=horizon,
                    max_forecast_age_hours=48,
                    max_price_age_hours=48,
                    instrument_ids=INSTRUMENTS,
                )
            )
            assert result["ok"] is True, (horizon, result)
            assert result["run"]["horizon"] == horizon
        finally:
            svc.close()


def test_rebalancing_recommendations_and_thresholds(tmp_path: Path) -> None:
    run = run_ok(tmp_path, rebalance_threshold=0.0)
    rows = run["rebalancing"]["intents"]
    assert rows
    assert {row["action"] for row in rows} <= {
        "increase_position",
        "reduce_position",
        "maintain_position",
        "open_position",
        "close_position",
        "hold_cash",
        "no_rebalancing_required",
    }
    assert run["rebalancing"]["estimated_transaction_costs"] >= 0
    no_trade = run_ok(
        tmp_path / "threshold",
        rebalance_threshold=0.25,
        min_trade_notional=1_000_000_000.0,
    )
    assert no_trade["rebalancing"]["action"] == "no_rebalancing_required"


def test_scenario_analysis_separates_historical_and_hypothetical(tmp_path: Path) -> None:
    run = run_ok(tmp_path)
    scenarios = run["scenarios"]
    assert scenarios
    assert any(row["kind"] == "historical" for row in scenarios)
    assert any(row["kind"] == "hypothetical" for row in scenarios)
    names = {str(row["name"]) for row in scenarios}
    assert {
        "historical_worst_window",
        "market_decline_10pct",
        "worst_sector_decline_15pct",
        "correlation_increase",
        "liquidity_deterioration",
        "largest_position_shock_20pct",
        "volatility_increase_25pct",
        "forecast_deterioration",
        "regime_transition",
    } <= names
    assert all(row["is_forecast"] is False for row in scenarios)
    svc = service(tmp_path)
    try:
        custom = svc.run_scenarios(
            run["run_id"],
            {"scenarios": [{"name": "custom", "shocks": {"XNSE:AAA": -0.2}}]},
        )
        assert custom["ok"] is True
        assert any(row["name"] == "custom" for row in custom["scenarios"])
        with pytest.raises(OptimizationValidationError):
            svc.run_scenarios(
                run["run_id"],
                {"scenarios": [{"name": "invalid", "shocks": {"XNSE:AAA": -2.0}}]},
            )
    finally:
        svc.close()


def test_backtesting_uses_point_in_time_history_and_charges_costs(tmp_path: Path) -> None:
    run = run_ok(tmp_path)
    svc = service(tmp_path)
    try:
        result = svc.backtest(run["run_id"], {"rebalance_frequency": 5})
        assert result["ok"] is True
        backtest = result["backtest"]
        assert backtest["look_ahead_controls"]["trailing_window_only"] is True
        assert backtest["look_ahead_controls"]["survivorship_bias_resolved"] is False
        assert all(method["status"] == "completed" for method in backtest["method_summary"])
        assert "current" in backtest["results"]
        assert all(float(method["trading_costs"]) >= 0 for method in backtest["method_summary"])
        assert backtest["results"]["equal_weight"]["metrics"]["max_drawdown"] <= 0
        assert svc.backtests(run_id=run["run_id"])["total"] >= 1
    finally:
        svc.close()


def test_market_benchmark_series_drives_covariance_and_backtest_comparison(
    tmp_path: Path,
) -> None:
    ctx = context()
    ctx["benchmark"] = {"instrument_id": "XNSE:MKT", "bars": _bars(80, instrument_index=3)}
    svc = service(tmp_path, ctx)
    try:
        result = svc.run(
            payload(covariance_method="market_model", benchmark_instrument_id="XNSE:MKT")
        )
        assert result["ok"] is True, result
        run = result["run"]
        assert run["covariance"]["method"] == "market_model"
        assert run["benchmark"]["used"] is True
        assert run["benchmark"]["instrument_id"] == "XNSE:MKT"
        assert run["benchmark"]["used_for_covariance"] is True
        assert run["benchmark"]["read_only"] is True
        assert run["financial_mutation"] is False
        history = run["analysis_history"]
        assert history["benchmark"]["instrument_id"] == "XNSE:MKT"
        assert len(history["benchmark"]["closes"]) == len(history["timestamps"])
        backtest = svc.backtest(run["run_id"], {"rebalance_frequency": 5})["backtest"]
        assert backtest["benchmark"]["kind"] == "market_series"
        assert backtest["benchmark"]["instrument_id"] == "XNSE:MKT"
        assert backtest["benchmark"]["buy_and_hold"] is True
        assert any("buy-and-hold market series" in line for line in backtest["assumptions"])
        equal_weight = backtest["results"]["equal_weight"]
        comparison = equal_weight["benchmark_comparison"]
        assert comparison["benchmark_kind"] == "market_series"
        assert comparison["benchmark_instrument_id"] == "XNSE:MKT"
        assert comparison["benchmark"] == "market_series:XNSE:MKT"
        expected = float(equal_weight["metrics"]["total_return"]) - float(
            backtest["benchmark"]["metrics"]["total_return"]
        )
        assert comparison["excess_total_return"] == pytest.approx(expected, abs=1e-12)
    finally:
        svc.close()


# The stored bars page holds fewer rows than the optimizer asks for, so an
# oldest-first read withholds the recent observations entirely.
_BARS_PAGE_CAP = 120


class _OptimizationContextSource:
    """The attributes ``_optimization_context`` reads, and nothing else."""

    def __init__(self, series: dict[str, list[dict[str, object]]]) -> None:
        self.series = series
        self.bars_calls: list[dict[str, object]] = []

    def portfolio_detail(self, portfolio_id: str) -> dict[str, object]:
        return {}

    def risk_portfolio(self, portfolio_id: str) -> dict[str, object]:
        return {}

    def portfolio_proposals(self, portfolio_id: str, **kwargs: object) -> dict[str, object]:
        return {}

    def market_bars(
        self,
        instrument_id: str,
        *,
        interval: str | None = None,
        limit: int | None = None,
        ascending: bool = False,
        **kwargs: object,
    ) -> dict[str, object]:
        self.bars_calls.append(
            {
                "instrument_id": instrument_id,
                "interval": interval,
                "limit": limit,
                "ascending": ascending,
            }
        )
        rows = self.series[instrument_id]
        ordered = rows if ascending else list(reversed(rows))
        capped = ordered[: min(int(limit or len(rows)), _BARS_PAGE_CAP)]
        return {"items": [dict(row) for row in capped]}

    def _multiagent_forecasts(
        self, instrument_id: str, horizon: str, as_of: datetime
    ) -> list[object]:
        return []

    def latest_feature_set(self, instrument_id: str) -> None:
        return None

    def latest_regime(self, instrument_id: str) -> None:
        return None


def test_the_optimization_context_reads_the_newest_bars_not_the_oldest() -> None:
    """A capped page read oldest-first handed the optimizer stale prices.

    The stored page is smaller than the 320 rows requested, so the context must
    ask for the newest observations and restore chronological order.  Reading
    the oldest rows leaves the newest price out and reports fresh data as stale.
    """
    instrument = INSTRUMENTS[0]
    start = datetime(2024, 1, 1, tzinfo=UTC)
    series = [
        {
            "observed_at": (start + timedelta(days=index)).isoformat(),
            "close": 100.0 + index,
            "volume": 250_000,
        }
        for index in range(500)
    ]
    source = _OptimizationContextSource({instrument: series})
    request = SimpleNamespace(
        portfolio_id="pf_default",
        horizon="1d",
        instrument_ids=[instrument],
        as_of=start + timedelta(days=500),
        benchmark_instrument_id="",
    )

    context = NovaMarketService._optimization_context(source, request)

    rows = context["bars"][instrument]["bars"]
    stamps = [row["observed_at"] for row in rows]
    # A chronological window ending at the newest observation is what the
    # optimizer's freshness and history checks need.
    assert stamps == sorted(stamps)
    assert len(rows) == _BARS_PAGE_CAP
    assert stamps[-1] == series[-1]["observed_at"]
    assert stamps[0] == series[-_BARS_PAGE_CAP]["observed_at"]


def test_market_model_covariance_refuses_a_missing_benchmark_series(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        with pytest.raises(OptimizationValidationError):
            svc.run(payload(covariance_method="market_model"))
        result = svc.run(
            payload(covariance_method="market_model", benchmark_instrument_id="XNSE:MKT")
        )
        assert result["ok"] is False
        run = result["run"]
        assert run["status"] == "insufficient_data"
        assert run["stage"] == "benchmark_alignment"
        assert "XNSE:MKT" in run["error"]
        assert run["financial_mutation"] is False
        with pytest.raises(OptimizationValidationError):
            svc.run(payload(benchmark_instrument_id="XNSE:AAA"))
    finally:
        svc.close()


def test_validate_reports_the_benchmark_coverage_before_a_run(tmp_path: Path) -> None:
    ctx = context()
    ctx["benchmark"] = {"instrument_id": "XNSE:MKT", "bars": _bars(80, instrument_index=3)}
    svc = service(tmp_path, ctx)
    try:
        result = svc.validate_inputs(
            payload(covariance_method="market_model", benchmark_instrument_id="XNSE:MKT")
        )
        assert result["valid"] is True
        benchmark = result["benchmark"]
        assert benchmark["instrument_id"] == "XNSE:MKT"
        assert benchmark["required"] is True
        assert benchmark["used"] is True
        assert benchmark["status"] == "used"
        assert benchmark["observations"] == 80
        assert benchmark["aligned_observations"] == 80
        assert benchmark["analysis_observations"] == 80
        assert result["aligned_observations"] == 80
        assert "covers 80 of 80" in benchmark["reason"]
        # An optional benchmark is reported the same way without being required.
        optional = svc.validate_inputs(payload(benchmark_instrument_id="XNSE:MKT"))
        assert optional["valid"] is True
        assert optional["benchmark"]["used"] is True
        assert optional["benchmark"]["required"] is False
    finally:
        svc.close()


def test_validate_refuses_an_unusable_benchmark_instead_of_a_clean_pass(
    tmp_path: Path,
) -> None:
    unconfigured = service(tmp_path)
    try:
        missing = unconfigured.validate_inputs(
            payload(covariance_method="market_model", benchmark_instrument_id="XNSE:MKT")
        )
        assert missing["ok"] is False
        assert missing["valid"] is False
        assert missing["status"] == "insufficient_data"
        assert "XNSE:MKT" in missing["detail"]
        assert missing["benchmark"]["used"] is False
        assert missing["benchmark"]["status"] == "unavailable"
        assert "unavailable" in missing["benchmark"]["reason"]
    finally:
        unconfigured.close()

    ctx = context()
    ctx["benchmark"] = {"instrument_id": "XNSE:MKT", "bars": _bars(10, instrument_index=3)}
    short = service(tmp_path, ctx)
    try:
        result = short.validate_inputs(
            payload(covariance_method="market_model", benchmark_instrument_id="XNSE:MKT")
        )
        assert result["valid"] is False
        assert result["status"] == "insufficient_data"
        benchmark = result["benchmark"]
        assert benchmark["status"] == "incomplete"
        assert benchmark["used"] is False
        assert benchmark["aligned_observations"] == 0
        assert "covers only 10 of 80 observations" in benchmark["reason"]
        assert any("market benchmark XNSE:MKT was not used" in line for line in result["warnings"])
    finally:
        short.close()


def test_backtest_without_a_benchmark_series_labels_the_strategy_fallback(
    tmp_path: Path,
) -> None:
    run = run_ok(tmp_path)
    svc = service(tmp_path)
    try:
        backtest = svc.backtest(run["run_id"], {})["backtest"]
        assert backtest["benchmark"]["kind"] == "strategy"
        assert backtest["benchmark"]["instrument_id"] == "equal_weight"
        assert backtest["benchmark"]["source"]
        completed = [
            item for item in backtest["results"].values() if item.get("status") == "completed"
        ]
        assert completed
        assert all(
            item["benchmark_comparison"]["benchmark_kind"] == "strategy" for item in completed
        )
        assert any("no market benchmark series" in line for line in backtest["assumptions"])
    finally:
        svc.close()


def test_benchmark_alignment_uses_only_covered_observations(tmp_path: Path) -> None:
    ctx = context()
    ctx["benchmark"] = {"instrument_id": "XNSE:MKT", "bars": _bars(40, instrument_index=3)}
    svc = service(tmp_path, ctx)
    try:
        result = svc.run(payload(benchmark_instrument_id="XNSE:MKT"))
        assert result["ok"] is True, result
        run = result["run"]
        assert run["benchmark"]["used"] is True
        assert run["benchmark"]["observations"] == 40
        assert run["benchmark"]["used_for_covariance"] is False
        assert run["covariance"]["method"] == "shrinkage"
        assert len(run["analysis_history"]["timestamps"]) == 40
        assert any(
            "aligned on 40 of 80 observations" in warning for warning in run["warnings"]
        )
    finally:
        svc.close()


def test_market_model_covariance_estimates_a_single_index_structure() -> None:
    rng = np.random.default_rng(11)
    market = rng.normal(0.0004, 0.011, 240)
    high_beta = 1.6 * market + rng.normal(0.0, 0.0008, 240)
    low_beta = 0.4 * market + rng.normal(0.0, 0.0008, 240)
    returns = np.column_stack([high_beta, low_beta])
    instruments = ["XNSE:HIGH", "XNSE:LOW"]
    result = estimate_covariance(
        returns,
        instruments,
        method="market_model",
        interval="1d",
        periods_per_year=252,
        market_returns=market,
    )
    assert result.method == "market_model"
    assert result.matrix[0][0] > result.matrix[1][1]
    assert result.correlation[0][1] > 0.9
    assert result.matrix[0][1] > 0
    with pytest.raises(OptimizationValidationError):
        estimate_covariance(
            returns,
            instruments,
            method="market_model",
            interval="1d",
            periods_per_year=252,
        )
    with pytest.raises(OptimizationValidationError):
        estimate_covariance(
            returns,
            instruments,
            method="market_model",
            interval="1d",
            periods_per_year=252,
            market_returns=market[:10],
        )


def test_persistence_versioning_and_idempotent_reuse(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        first = svc.create_config(payload())
        second = svc.create_config(payload())
        assert first["config"]["config_id"] == second["config"]["config_id"]
        one = svc.run(payload(), idempotency_key="same-key")
        two = svc.run(payload(), idempotency_key="same-key")
        assert two["idempotent_reuse"] is True
        assert one["run"]["run_id"] == two["run"]["run_id"]
        assert one["run"]["config_version"] == one["run"]["config_id"]
        assert svc.run_record(one["run"]["run_id"])["run"]["status"] in {
            "completed",
            "completed_with_warnings",
        }
    finally:
        svc.close()


def test_unknown_run_is_reported_not_invented(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        with pytest.raises(OptimizationNotFoundError):
            svc.run_record("por_missing")
    finally:
        svc.close()


def test_invalid_configuration_and_infeasible_limits_are_refused_explicitly(
    tmp_path: Path,
) -> None:
    svc = service(tmp_path)
    try:
        with pytest.raises(OptimizationValidationError):
            svc.create_config(payload(min_position_weight=0.6, max_instrument_weight=0.2))
        with pytest.raises(OptimizationValidationError):
            svc.run(payload(method="not-a-method"))
        assert svc.runs()["total"] == 0
    finally:
        svc.close()


def _solver_asset(
    instrument_id: str, sector: str, liquidity_cap_value: float | None
) -> object:
    return type(
        "Asset",
        (),
        {
            "instrument_id": instrument_id,
            "sector": sector,
            "liquidity_cap_value": liquidity_cap_value,
            "forecast": None,
        },
    )()


def test_an_allocation_with_no_allocatable_capacity_is_explicitly_infeasible() -> None:
    assets = [
        _solver_asset("XNSE:AAA", "technology", 0.0),
        _solver_asset("XNSE:BBB", "financials", 0.0),
    ]
    config = OptimizationConfig.from_payload(
        payload(instrument_ids=["XNSE:AAA", "XNSE:BBB"]), now=NOW
    )
    with pytest.raises(OptimizationInfeasibleError) as raised:
        solve_weights(
            assets,
            covariance=np.eye(2, dtype=float) * 0.01,
            config=config,
        )
    assert not isinstance(raised.value, OptimizationValidationError)
    assert "no allocation can satisfy them" in str(raised.value)


def test_a_sector_exclusion_that_empties_capacity_is_infeasible(tmp_path: Path) -> None:
    """A genuine config/universal conflict reaches the branch, nothing is patched.

    ``sector_limits`` may name a sector at 0.0 — a real "hold none of this"
    policy. When every eligible instrument belongs to an excluded sector the
    instrument and sector caps leave zero allocatable capacity, so the real
    solver raises its own infeasibility and the run reports it as a distinct
    outcome rather than an empty allocation or a solver failure.
    """
    excluded_sectors = {sector: 0.0 for sector in SECTORS}
    svc = service(tmp_path)
    try:
        result = svc.run(payload(sector_limits=excluded_sectors))
        run = result["run"]
        assert result["ok"] is False
        assert run["status"] == "infeasible_constraints"
        assert run["result_status"] == "infeasible_constraints"
        assert run["stage"] == "constrained_allocation"
        assert "no allocation can satisfy them" in run["error"]
        assert "allocations" not in run
        # The universe was eligible: this is a constraint conflict, not empty
        # or invalid input.
        assert len(run["eligible"]) == len(INSTRUMENTS)
        assert run["excluded"] == []
        stored = svc.run_record(run["run_id"])["run"]
        assert stored["status"] == "infeasible_constraints"
        status = svc.status()
        assert status["infeasible_constraint_runs"] == 1
        assert status["failed_runs"] == 0
        assert status["completed_runs"] == 0
        # Deterministic: the same conflict refuses again and is counted again.
        repeated = svc.run(payload(sector_limits=excluded_sectors))
        assert repeated["run"]["status"] == "infeasible_constraints"
        assert repeated["run"]["error"] == run["error"]
        assert svc.status()["infeasible_constraint_runs"] == 2
    finally:
        svc.close()


def test_solver_cancellation_and_context_failure_are_explicit(tmp_path: Path) -> None:
    class Cancelled:
        def is_set(self) -> bool:
            return True

    assets = [
        type(
            "Asset",
            (),
            {
                "instrument_id": "A",
                "sector": "tech",
                "liquidity_cap_value": None,
                "forecast": None,
            },
        )()
    ]
    config = OptimizationConfig.from_payload(payload(instrument_ids=["XNSE:A"]), now=NOW)
    with pytest.raises(OptimizationStateError):
        solve_weights(
            assets,
            covariance=np.array([[0.01]]),
            config=config,
            cancelled=Cancelled(),
        )

    failing = OptimizationService(
        tmp_path / "failing.sqlite3",
        context_loader=lambda request: (_ for _ in ()).throw(RuntimeError("context failed")),
        clock=lambda: NOW,
    )
    try:
        result = failing.run(payload())
        assert result["ok"] is False
        assert result["run"]["status"] == "failed"
        assert "context failed" in result["run"]["error"]
    finally:
        failing.close()


def test_high_transaction_costs_are_visible_in_rebalancing_and_backtests(
    tmp_path: Path,
) -> None:
    run = run_ok(
        tmp_path,
        rebalance_threshold=0.0,
        transaction_cost_bps=100.0,
        slippage_bps=100.0,
    )
    assert run["rebalancing"]["estimated_transaction_costs"] > 0
    assert run["rebalancing"]["thresholds"]["transaction_cost_bps"] == 100.0
    svc = service(tmp_path)
    try:
        backtest = svc.backtest(run["run_id"], {})["backtest"]
        assert all(method["trading_costs"] >= 0 for method in backtest["method_summary"])
    finally:
        svc.close()


def test_resource_limits_are_exposed_and_bounded(tmp_path: Path) -> None:
    svc = OptimizationService(
        tmp_path / "bounded.sqlite3",
        context_loader=lambda request: context(),
        clock=lambda: NOW,
        max_concurrent=99,
        run_timeout_seconds=10_000,
    )
    try:
        health = svc.health()
        assert health["store"]["backtests"] >= 0
        assert svc.max_concurrent <= 8
        assert svc.run_timeout_seconds <= 120
    finally:
        svc.close()


# -- integration, authority and API -------------------------------------------


def test_optimization_store_contains_only_isolated_research_tables(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        svc.run(payload())
        connection = sqlite3.connect(tmp_path / "optimization.sqlite3")
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        assert tables
        assert all(table.startswith("po_") for table in tables)
        assert not any(
            forbidden in table
            for table in tables
            for forbidden in ("portfolio", "ledger", "order", "fill")
        )
    finally:
        svc.close()


def test_optimizer_source_has_no_portfolio_or_execution_write_paths() -> None:
    root = Path(__file__).parents[1] / "src" / "novacontrol" / "novamarket" / "optimization"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for forbidden in (
        "import novacontrol.novamarket.portfolio",
        "import novacontrol.novamarket.execution",
        "import novacontrol.novamarket.risk",
        "PortfolioEngine",
        "ExecutionEngine",
        "RiskEngine",
        "submit_order(",
    ):
        assert forbidden not in source


def test_pi17_portfolio_agent_receives_advisory_optimization_context() -> None:
    message = AgentMessage(
        message_id="mam_" + "a" * 20,
        cycle_id="mac_" + "c" * 20,
        sender_agent_id="multiagent-coordinator",
        recipient_agent_id="fin-portfolio",
        task_id="mat_" + "t" * 20,
        message_type=MessageType.TASK_ASSIGNMENT,
        instrument_id="XNSE:AAA",
        exchange="XNSE",
        payload={"horizon": "1d"},
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        correlation_id="corr",
        config_version="1",
        source_references=("portfolio.read",),
    )
    request = AgentRequest(
        message=message,
        horizon="1d",
        as_of=NOW,
        context={
            "portfolio": {
                "as_of": NOW.isoformat(),
                "portfolio": {"portfolio_id": "pf_default"},
                "valuation": {"cash": "400000", "total_value": "1000000", "cash_weight": 0.4},
                "holdings": [{"instrument_id": "XNSE:AAA", "weight": 0.18}],
                "optimization": {
                    "run_id": "por_test",
                    "status": "completed",
                    "horizon": "1d",
                    "cash_reserve": 0.3,
                    "target_weights": {"XNSE:AAA": 0.2},
                },
            }
        },
    )
    response = PortfolioIntelligenceAgent().analyze(request, DEFINITION_BY_ID["fin-portfolio"])
    assert response.findings["state_mutated"] is False
    assert response.findings["optimization_context"]["run_id"] == "por_test"
    assert response.findings["optimization_context"]["production_authority"] is False


def test_api_routes_are_declared_once_with_non_render_consumers() -> None:
    routes = {(route.method, route.path) for route in ApiSurface.default().routes}
    expected = {
        ("GET", "/market/optimization/status"),
        ("GET", "/market/optimization/contracts"),
        ("GET", "/market/optimization/health"),
        ("POST", "/market/optimization/configs"),
        ("GET", "/market/optimization/configs"),
        ("GET", "/market/optimization/configs/{config_id}"),
        ("POST", "/market/optimization/validate"),
        ("POST", "/market/optimization/runs"),
        ("GET", "/market/optimization/runs"),
        ("GET", "/market/optimization/history"),
        ("GET", "/market/optimization/latest"),
        ("GET", "/market/optimization/runs/{run_id}"),
        ("GET", "/market/optimization/runs/{run_id}/allocations"),
        ("GET", "/market/optimization/runs/{run_id}/diversification"),
        ("GET", "/market/optimization/runs/{run_id}/risk"),
        ("GET", "/market/optimization/runs/{run_id}/liquidity"),
        ("GET", "/market/optimization/runs/{run_id}/rebalancing"),
        ("GET", "/market/optimization/runs/{run_id}/scenarios"),
        ("POST", "/market/optimization/runs/{run_id}/scenarios"),
        ("POST", "/market/optimization/runs/{run_id}/backtests"),
        ("GET", "/market/optimization/runs/{run_id}/handoff"),
        ("POST", "/market/optimization/runs/{run_id}/cancel"),
        ("GET", "/market/optimization/backtests"),
        ("GET", "/market/optimization/backtests/{backtest_id}"),
    }
    assert expected <= routes
    for route in expected:
        assert ROUTE_CONSUMERS[route] == "no-render"


def test_actual_application_registers_the_optimization_routes() -> None:
    app = create_app()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/market/optimization/status" in paths
    assert "/market/optimization/runs" in paths
    assert "/market/optimization/runs/{run_id}/backtests" in paths


def test_frontend_workspace_and_navigation_are_wired() -> None:
    web = Path(__file__).parents[1] / "web" / "src"
    page = (web / "pages" / "PortfolioOptimization.tsx").read_text(encoding="utf-8")
    app = (web / "App.tsx").read_text(encoding="utf-8")
    nav = (web / "nav.ts").read_text(encoding="utf-8")
    assert "/market/optimization/runs" in page
    assert 'case "optimization"' in app
    assert 'id: "optimization"' in nav


def test_scenario_position_contributions_reconcile_to_portfolio_change(tmp_path: Path) -> None:
    run = run_ok(tmp_path)
    for scenario in run["scenarios"]:
        contributions = sum(
            float(row.get("value_change") or 0.0)
            for row in scenario.get("position_contributions", [])
        )
        assert contributions == pytest.approx(float(scenario["portfolio_value_change"]), abs=1e-6)


def test_missing_expected_return_is_not_replaced_with_zero(tmp_path: Path) -> None:
    ctx = context()
    for rows in ctx["forecasts"].values():  # type: ignore[union-attr]
        rows[0].pop("expected_return")
    svc = service(tmp_path, ctx)
    try:
        result = svc.run(payload(objective="risk_adjusted"))
        assert result["ok"] is False
        assert result["run"]["status"] == "insufficient_data"
    finally:
        svc.close()


def test_forecast_quality_minimum_is_enforced(tmp_path: Path) -> None:
    ctx = context()
    for rows in ctx["forecasts"].values():  # type: ignore[union-attr]
        rows[0]["quality"] = "low"
    svc = service(tmp_path, ctx)
    try:
        result = svc.run(payload(min_forecast_quality="high"))
        assert result["ok"] is False
        assert result["run"]["status"] == "insufficient_data"
    finally:
        svc.close()


def test_inverse_volatility_does_not_require_a_forecast(tmp_path: Path) -> None:
    svc = service(tmp_path, context(include_forecasts=False))
    try:
        result = svc.run(payload(method="inverse_volatility"))
        assert result["ok"] is True
    finally:
        svc.close()


def test_truncated_universe_is_reported_instead_of_disappearing(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        result = svc.validate_inputs(payload(max_instruments=1), as_of=NOW)
        assert len(result["eligible"]) == 1
        assert any("max_instruments" in " ".join(row["reasons"]) for row in result["excluded"])
    finally:
        svc.close()


def test_explainability_reports_drivers_and_binding_constraints(tmp_path: Path) -> None:
    run = run_ok(tmp_path)
    explanation = run["explainability"]
    assert explanation["objective"] == "risk_adjusted"
    assert explanation["method"] == "mean_variance"
    assert explanation["position_count"] == len(explanation["positions"])
    assert explanation["positions"]
    assert all(item["summary"] for item in explanation["positions"])
    assert explanation["invested_weight"] == pytest.approx(
        sum(item["target_weight"] for item in explanation["positions"])
    )
    assert explanation["cash_weight"] >= 0.0
    assert "binding_constraints" in explanation
    assert explanation["not_claimed"].startswith("no causal claim")


def test_rebalancing_reports_expected_improvement_and_slippage(tmp_path: Path) -> None:
    run = run_ok(
        tmp_path,
        rebalance_threshold=0.0,
        transaction_cost_bps=0.0,
        slippage_bps=100.0,
    )
    rebalancing = run["rebalancing"]
    assert rebalancing["expected_improvement"] is not None
    assert rebalancing["thresholds"]["slippage_bps"] == 100.0
    assert rebalancing["estimated_transaction_costs"] > 0
    assert rebalancing["funding"]["status"] in {"funded", "unfunded"}


def test_backtest_controls_and_intraday_annualization(tmp_path: Path) -> None:
    svc = service(tmp_path, context(forecast_horizon="5m"))
    try:
        run = svc.run(payload(horizon="5m", max_forecast_age_hours=48, max_price_age_hours=48))
        assert run["ok"] is True
        backtest = svc.backtest(run["run"]["run_id"], {})["backtest"]
        assert backtest["look_ahead_controls"]["rebalance_period_return_excluded"] is True
        assert backtest["look_ahead_controls"]["future_prices_excluded"] is True
        for method in backtest["results"].values():
            assert method["metrics"]["periods_per_year"] == 19_656
    finally:
        svc.close()


def test_twenty_minute_horizon_uses_five_minute_bars_and_four_periods(tmp_path: Path) -> None:
    svc = service(tmp_path, context(forecast_horizon="20m"))
    try:
        result = svc.run(payload(horizon="20m", max_forecast_age_hours=48, max_price_age_hours=48))
        assert result["ok"] is True
        assert result["run"]["covariance"]["periods_per_year"] == 19_656
        assert result["run"]["horizon"] == "20m"
    finally:
        svc.close()


def test_handoff_rejects_an_incomplete_run(tmp_path: Path) -> None:
    failing = OptimizationService(
        tmp_path / "handoff.sqlite3",
        context_loader=lambda request: (_ for _ in ()).throw(RuntimeError("context failed")),
        clock=lambda: NOW,
    )
    try:
        result = failing.run(payload())
        with pytest.raises(OptimizationStateError):
            failing.handoff(result["run"]["run_id"])
    finally:
        failing.close()


try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional development dependency
    TestClient = None  # type: ignore[assignment]


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class TestOptimizationHttpSurface(unittest.TestCase):
    def test_real_application_serves_status_contract_and_health(self) -> None:
        with (
            TemporaryDirectory() as directory,
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=lambda **kwargs: NovaControlApplication(
                    **{**kwargs, "data_dir": directory}
                ),
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
            TestClient(create_app()) as client,
        ):
            status = client.get("/market/optimization/status")
            assert status.status_code == 200, status.text
            assert status.json()["production_authority"] is False
            contracts = client.get("/market/optimization/contracts")
            assert contracts.status_code == 200, contracts.text
            assert contracts.json()["authority"]["risk"] == "NM-08"
            health = client.get("/market/optimization/health")
            assert health.status_code == 200, health.text
            assert health.json()["authority_health"]["optimizer_can_mutate_portfolio"] is False
