"""Point-in-time allocation-strategy backtesting for PI-18.

The loop only sees prices up to each rebalance date.  Forecasts are not
fabricated for history: trailing mean returns are used as an explicitly labelled
allocation input, and the result is a research comparison, not investment
advice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .analytics import backtest_metrics, estimate_covariance, log_returns
from .models import (
    HORIZONS,
    AllocationMethod,
    ExpectedReturn,
    OptimizationConfig,
    OptimizationValidationError,
)
from .solvers import solve_weights

NDArray = np.ndarray[Any, Any]


@dataclass(frozen=True, slots=True)
class _BacktestAsset:
    instrument_id: str
    sector: str
    current_value: float
    liquidity_cap_value: float | None
    forecast: ExpectedReturn


def run_strategy_backtest(
    *,
    basis: Mapping[str, Any],
    current_weights: Mapping[str, float],
    config: OptimizationConfig,
    initial_capital: float,
    methods: Sequence[str],
    rebalance_frequency: int = 5,
) -> dict[str, Any]:
    timestamps = list(basis.get("timestamps") or [])
    closes_raw = basis.get("closes") or {}
    if not isinstance(closes_raw, Mapping):
        raise OptimizationValidationError("backtest closes must be a mapping")
    instruments = [str(item) for item in closes_raw if isinstance(closes_raw[item], Sequence)]
    if not instruments:
        raise OptimizationValidationError("backtest has no close series")
    closes = {
        instrument: np.asarray([float(value) for value in closes_raw[instrument]], dtype=float)
        for instrument in instruments
    }
    length = min([len(values) for values in closes.values()] + [len(timestamps)])
    if length < config.min_history_observations + 3:
        raise OptimizationValidationError(
            "backtest needs more aligned observations than the minimum history window"
        )
    timestamps = timestamps[-length:]
    for instrument in instruments:
        closes[instrument] = closes[instrument][-length:]
        if np.any(closes[instrument] <= 0):
            raise OptimizationValidationError(
                f"backtest close series {instrument} contains a non-positive value"
            )
    frequency = max(1, min(int(rebalance_frequency), 60))
    initial = float(initial_capital)
    if initial <= 0:
        raise OptimizationValidationError("backtest initial capital must be positive")
    config = replace(
        config,
        instrument_ids=tuple(instruments),
        min_cash_reserve=config.min_cash_reserve,
        liquidity_policy="warn",
        method=AllocationMethod.MEAN_VARIANCE.value,
    )
    market_benchmark = _market_benchmark(basis, length, config, initial)
    benchmark_returns: NDArray | None = None
    if market_benchmark is not None:
        benchmark_returns = np.asarray(market_benchmark.pop("log_returns"), dtype=float)
    results: dict[str, dict[str, Any]] = {}
    for method in methods:
        try:
            results[method] = _run_one(
                timestamps=timestamps,
                closes=closes,
                instruments=instruments,
                current_weights=current_weights,
                config=config,
                initial_capital=initial,
                method=method,
                frequency=frequency,
                market_returns=benchmark_returns,
            )
        except Exception as exc:  # noqa: BLE001 - each strategy failure is explicit
            results[method] = {
                "method": method,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "metrics": {"sufficient": False},
            }
    benchmark_name = (
        config.benchmark if config.benchmark in results else AllocationMethod.EQUAL_WEIGHT.value
    )
    benchmark = results.get(benchmark_name)
    if market_benchmark is not None:
        for item in results.values():
            if item.get("status") == "completed":
                item["benchmark_comparison"] = _compare(
                    item["metrics"],
                    market_benchmark["metrics"],
                    benchmark=f"market_series:{market_benchmark['instrument_id']}",
                )
                item["benchmark_comparison"]["benchmark_kind"] = "market_series"
                item["benchmark_comparison"]["benchmark_instrument_id"] = market_benchmark[
                    "instrument_id"
                ]
    else:
        for item in results.values():
            if (
                item.get("status") == "completed"
                and benchmark
                and benchmark.get("status") == "completed"
            ):
                item["benchmark_comparison"] = _compare(
                    item["metrics"], benchmark["metrics"], benchmark=benchmark_name
                )
                item["benchmark_comparison"]["benchmark_kind"] = "strategy"
    completed = sum(1 for item in results.values() if item.get("status") == "completed")
    return {
        "status": "completed" if completed else "failed",
        "methods": list(methods),
        "rebalance_frequency": frequency,
        "initial_capital": initial,
        "observations": length,
        "instruments": instruments,
        "results": results,
        "method_summary": [
            {
                "method": item["method"],
                "status": item["status"],
                "total_return": item.get("metrics", {}).get("total_return"),
                "max_drawdown": item.get("metrics", {}).get("max_drawdown"),
                "sharpe_ratio": item.get("metrics", {}).get("sharpe_ratio"),
                "turnover": item.get("turnover"),
                "trading_costs": item.get("trading_costs"),
                "error": item.get("error", ""),
            }
            for item in results.values()
        ],
        "benchmark": market_benchmark
        if market_benchmark is not None
        else {
            "kind": "strategy",
            "instrument_id": benchmark_name,
            "source": "no market benchmark series is configured for this run",
            "buy_and_hold": False,
            "costs_applied": True,
            "metrics": dict(benchmark.get("metrics") or {}) if benchmark else {"sufficient": False},
        },
        "assumptions": [
            "only closes at or before each rebalance are used",
            "the current instrument universe is used; survivorship bias may remain",
            "transaction costs and slippage are charged on every rebalance",
            "trailing mean returns are an allocation input, not a forecast of realized returns",
            "a test result is not a guarantee of future performance",
            *(
                [
                    "the benchmark is a buy-and-hold market series from PI-02 market data; "
                    "no costs or slippage are charged to it"
                ]
                if market_benchmark is not None
                else [
                    "no market benchmark series was configured; the comparison baseline is "
                    f"the {benchmark_name} strategy"
                ]
            ),
        ],
        "look_ahead_controls": {
            "trailing_window_only": True,
            "rebalance_period_return_excluded": True,
            "future_prices_excluded": True,
            "point_in_time_cutoff_required": True,
            "survivorship_bias_resolved": False,
        },
        "is_forecast": False,
        "real_money": False,
    }


def _run_one(
    *,
    timestamps: Sequence[Any],
    closes: Mapping[str, NDArray],
    instruments: Sequence[str],
    current_weights: Mapping[str, float],
    config: OptimizationConfig,
    initial_capital: float,
    method: str,
    frequency: int,
    market_returns: NDArray | None = None,
) -> dict[str, Any]:
    if method not in {item.value for item in AllocationMethod}:
        raise OptimizationValidationError(f"unknown backtest method {method!r}")
    price_matrix = np.column_stack([closes[item] for item in instruments])
    returns, _ = log_returns(price_matrix)
    start = max(config.min_history_observations, 5)
    if len(returns) <= start + 1:
        raise OptimizationValidationError("not enough return observations for a backtest")
    current = np.array([float(current_weights.get(item, 0.0)) for item in instruments])
    if current.sum() <= 0:
        current = np.ones(len(instruments)) / len(instruments) * config.investable_fraction
    values = np.zeros(len(instruments), dtype=float)
    cash = float(initial_capital)
    values[:] = current / max(1e-12, current.sum()) * initial_capital * min(1.0, current.sum())
    cash = max(0.0, initial_capital - float(values.sum()))
    equity = [initial_capital]
    period_returns: list[float] = []
    turnover_total = 0.0
    costs_total = 0.0
    rebalances = 0
    target_weights: dict[str, float] | None = None
    if method == AllocationMethod.CURRENT.value:
        target_weights = {
            instrument: float(current[position])
            for position, instrument in enumerate(instruments)
            if current[position] > 0
        }
    equity_timestamps: list[str] = [str(timestamps[0])]
    for index in range(start, len(returns)):
        if (
            index == start or (index - start) % frequency == 0
        ) and method != AllocationMethod.CURRENT.value:
            # The rebalance at ``index`` may only see returns strictly before
            # the period it is about to earn.  Including ``returns[index]``
            # would let the solver trade on the close that produces that return.
            window = returns[:index]
            covariance = estimate_covariance(
                window,
                instruments,
                method=config.covariance_method,
                interval=str(HORIZONS[config.horizon]["interval"]),
                periods_per_year=int(HORIZONS[config.horizon]["periods_per_year"]),
                ewma_halflife=config.ewma_halflife,
                shrinkage=config.shrinkage,
                market_returns=(
                    None if market_returns is None else market_returns[:index].copy()
                ),
            )
            means = np.mean(window, axis=0)
            assets = [
                _BacktestAsset(
                    instrument_id=instrument,
                    sector="backtest",
                    current_value=0.0,
                    liquidity_cap_value=None,
                    forecast=ExpectedReturn(
                        instrument_id=instrument,
                        horizon=config.horizon,
                        expected_return=float(means[position]),
                        adjusted_return=float(means[position]),
                        uncertainty="medium",
                        interval_width=None,
                        forecast_ids=(),
                        model_versions=("trailing-mean",),
                        generated_at=datetime.now(UTC),
                        validity="valid",
                        calibrated_probability=None,
                        probability_is_calibrated=False,
                        disagreement=None,
                        quality="research",
                        warnings=("trailing mean is a backtest input",),
                    ),
                )
                for position, instrument in enumerate(instruments)
            ]
            solution = solve_weights(
                assets,
                covariance=np.asarray(covariance.matrix, dtype=float),
                config=replace(config, method=method),
                current_weights={
                    instrument: float(values[position] / max(1e-12, values.sum() + cash))
                    for position, instrument in enumerate(instruments)
                },
                total_value=max(1e-9, float(values.sum() + cash)),
                max_iterations=220,
            )
            new_weights = np.array(
                [float(solution["weights"].get(item, 0.0)) for item in instruments]
            )
            current_total = float(values.sum() + cash)
            desired_values = new_weights * current_total
            traded = float(np.sum(np.abs(desired_values - values)))
            cost = traded * (config.transaction_cost_bps + config.slippage_bps) / 10_000.0
            values = desired_values
            cash = max(0.0, current_total - float(values.sum()) - cost)
            if cost > 0 and cash < 1e-9:
                values *= max(0.0, 1.0 - cost / max(1e-9, float(values.sum())))
            turnover_total += traded / max(1e-12, current_total)
            costs_total += cost
            target_weights = {
                instrument: float(new_weights[position])
                for position, instrument in enumerate(instruments)
                if new_weights[position] > 0
            }
            rebalances += 1
        period = returns[index]
        previous_total = float(values.sum() + cash)
        values = values * (1.0 + period)
        current_total = float(values.sum() + cash)
        period_return = current_total / max(1e-12, previous_total) - 1.0
        period_returns.append(period_return)
        equity.append(current_total)
        equity_timestamps.append(str(timestamps[index + 1]))
    metrics = backtest_metrics(
        equity,
        period_returns,
        periods_per_year=int(HORIZONS[config.horizon]["periods_per_year"]),
    )
    final_total = equity[-1] if equity else initial_capital
    final_weights = {
        instrument: float(values[position] / max(1e-12, final_total))
        for position, instrument in enumerate(instruments)
        if values[position] > 0
    }
    hhi = sum(value**2 for value in final_weights.values())
    return {
        "method": method,
        "status": "completed",
        "equity_curve": [
            {"at": equity_timestamps[index], "value": float(value)}
            for index, value in enumerate(equity)
        ][-400:],
        "metrics": metrics,
        "turnover": turnover_total,
        "average_turnover": turnover_total / rebalances if rebalances else 0.0,
        "trading_costs": costs_total,
        "rebalances": rebalances,
        "final_weights": final_weights,
        "final_cash_weight": cash / max(1e-12, final_total),
        "final_concentration_hhi": hhi,
        "final_effective_holdings": 1.0 / hhi if hhi > 0 else 0.0,
        "target_weights": target_weights or {},
        "method_note": _method_note(method),
    }


def _market_benchmark(
    basis: Mapping[str, Any],
    length: int,
    config: OptimizationConfig,
    initial_capital: float,
) -> dict[str, Any] | None:
    """Buy-and-hold metrics for the run's real market benchmark series.

    The closes ride on the run's analysis history, already aligned to the same
    observation keys as the strategy returns, so the comparison window is the
    point-in-time window the strategies traded - no synthesised equal-weight
    proxy and no future prices.
    """
    raw = basis.get("benchmark")
    if not isinstance(raw, Mapping):
        return None
    instrument_id = str(raw.get("instrument_id") or "").strip()
    closes_raw = raw.get("closes")
    if (
        not instrument_id
        or not isinstance(closes_raw, Sequence)
        or isinstance(closes_raw, (str, bytes, bytearray))
    ):
        return None
    try:
        values = np.asarray([float(item) for item in closes_raw], dtype=float)
    except (TypeError, ValueError):
        return None
    if values.size < length:
        return None
    values = values[-length:]
    if np.any(values <= 0) or not np.all(np.isfinite(values)):
        return None
    start = max(config.min_history_observations, 5)
    changes = np.diff(np.log(values))
    if len(changes) <= start + 1:
        return None
    period_returns = [float(np.exp(value) - 1.0) for value in changes[start:]]
    equity = [float(initial_capital)]
    for value in period_returns:
        equity.append(equity[-1] * (1.0 + value))
    return {
        "kind": "market_series",
        "instrument_id": instrument_id,
        "source": "PI-02 market-data bars",
        "buy_and_hold": True,
        "costs_applied": False,
        "observations": len(period_returns),
        "metrics": backtest_metrics(
            equity,
            period_returns,
            periods_per_year=int(HORIZONS[config.horizon]["periods_per_year"]),
        ),
        "log_returns": changes,
    }


def _compare(
    metrics: Mapping[str, Any],
    benchmark_metrics: Mapping[str, Any],
    *,
    benchmark: str = AllocationMethod.EQUAL_WEIGHT.value,
) -> dict[str, Any]:
    def difference(key: str) -> float | None:
        left = metrics.get(key)
        right = benchmark_metrics.get(key)
        if left is None or right is None:
            return None
        try:
            return float(left) - float(right)
        except (TypeError, ValueError):
            return None

    return {
        "benchmark": benchmark,
        "excess_total_return": difference("total_return"),
        "excess_annualized_return": difference("annualized_return"),
        "excess_sharpe": difference("sharpe_ratio"),
        "drawdown_difference": difference("max_drawdown"),
        "same_period": True,
        "same_costs": True,
    }


def _method_note(method: str) -> str:
    return {
        AllocationMethod.EQUAL_WEIGHT.value: "equal-weight baseline",
        AllocationMethod.MIN_VARIANCE.value: "minimum-variance constrained solution",
        AllocationMethod.RISK_PARITY.value: "equal risk-contribution approximation",
        AllocationMethod.MEAN_VARIANCE.value: "mean-variance/trailing-return solution",
        AllocationMethod.INVERSE_VOLATILITY.value: "inverse-volatility heuristic",
        AllocationMethod.CURRENT.value: "current-book reference baseline",
    }.get(method, "allocation method")


__all__ = ["run_strategy_backtest"]
