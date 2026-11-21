"""Numerical analytics for PI-18.

Everything here is a *research estimate* over aligned historical observations.
The covariance matrix, risk metrics and scenario losses are not NM-08 approvals
and are never written back to a portfolio or ledger.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .models import (
    CovarianceMethod,
    CovarianceResult,
    OptimizationValidationError,
    PriceSeries,
)

NDArray = np.ndarray[Any, Any]


def _keys(series: PriceSeries) -> list[object]:
    if series.interval in {"1d", "1wk", "1mo"}:
        return [item.date() for item in series.observed_at]
    return [item.isoformat() for item in series.observed_at]


def aligned_price_matrix(
    series_by_instrument: Mapping[str, PriceSeries],
    instruments: Sequence[str],
    *,
    minimum_observations: int,
) -> tuple[list[str], list[object], NDArray, tuple[str, ...]]:
    """Align closes on identical observation keys and return a price matrix.

    Intersection-only alignment is deliberate: filling one instrument's missing
    bar from a neighbouring session invents a synchronised return.  If one
    instrument cannot span the common window, it is excluded and the reason is
    retained instead of fabricating history.
    """
    selected = [item for item in instruments if item in series_by_instrument]
    warnings: list[str] = []
    while selected:
        maps: dict[str, dict[object, float]] = {}
        for instrument_id in selected:
            series = series_by_instrument[instrument_id]
            keys = _keys(series)
            maps[instrument_id] = {
                key: float(price)
                for key, price in zip(keys, series.close, strict=False)
                if price > 0
            }
        common = set(maps[selected[0]])
        for instrument_id in selected[1:]:
            common &= set(maps[instrument_id])
        ordered = sorted(common, key=str)
        if len(ordered) >= minimum_observations + 1:
            matrix = np.array(
                [[maps[instrument_id][key] for instrument_id in selected] for key in ordered],
                dtype=float,
            )
            return list(selected), ordered, matrix, tuple(warnings)
        counts = {
            instrument_id: len(common & set(maps[instrument_id])) for instrument_id in selected
        }
        minimum = min(counts, key=lambda key: (counts[key], key))
        warnings.append(
            f"{minimum}: insufficient common history "
            f"({counts[minimum]} observations; need {minimum_observations + 1})"
        )
        selected.remove(minimum)
    return [], [], np.empty((0, 0)), tuple(warnings)


def align_benchmark(
    timestamps: Sequence[object],
    prices: NDArray,
    *,
    observed_at: Sequence[object],
    close: Sequence[float],
    minimum_observations: int,
) -> tuple[list[object], NDArray, NDArray, tuple[str, ...]]:
    """Restrict a run's aligned window to observations the benchmark also covers.

    The benchmark is a real market series (PI-02 bars), never a synthesised
    equal-weight average.  Intersection-only alignment is used for the same
    reason as instrument alignment: borrowing a neighbouring session's close
    would invent a synchronous market return.  When the benchmark cannot cover
    enough of the window an empty array is returned and the caller must report
    that the benchmark was not used rather than silently substituting one.
    """
    if len(observed_at) != len(close):
        raise OptimizationValidationError("benchmark series is not aligned with its closes")
    by_time: dict[str, float] = {}
    by_date: dict[str, float] = {}
    ambiguous: set[str] = set()
    for moment, value in zip(observed_at, close, strict=False):
        if value is None or float(value) <= 0:
            continue
        price = float(value)
        by_time[str(moment)] = price
        day = str(moment)[:10]
        if day in by_date and abs(by_date[day] - price) > 1e-12:
            # Two benchmark observations on one session make a date-only match
            # ambiguous; those rows are left unmatched instead of guessed.
            ambiguous.add(day)
        by_date[day] = price

    def close_for(key: object) -> float | None:
        text = str(key)
        if text in by_time:
            return by_time[text]
        day = text[:10]
        if day in by_date and day not in ambiguous:
            return by_date[day]
        return None

    keep = [index for index, item in enumerate(timestamps) if close_for(item) is not None]
    empty = np.empty(0, dtype=float)
    if len(keep) < minimum_observations + 1:
        return (
            list(timestamps),
            prices,
            empty,
            (
                f"benchmark series covers only {len(keep)} of {len(timestamps)} observations "
                f"(need {minimum_observations + 1}); market benchmark not used",
            ),
        )
    warnings: list[str] = []
    if len(keep) < len(timestamps):
        warnings.append(
            f"analysis window aligned on {len(keep)} of {len(timestamps)} observations "
            "shared with the market benchmark"
        )
    aligned_prices = np.asarray(prices, dtype=float)[keep]
    matched = [close_for(timestamps[index]) for index in keep]
    aligned_benchmark = np.array(
        [0.0 if value is None else float(value) for value in matched], dtype=float
    )
    return (
        [timestamps[index] for index in keep],
        aligned_prices,
        aligned_benchmark,
        tuple(warnings),
    )


def _market_model_covariance(
    returns: NDArray, market_returns: Sequence[float] | NDArray | None
) -> tuple[NDArray, list[str]]:
    """Single-index covariance: beta_i*beta_j*var_market plus residual variance."""
    if market_returns is None:
        raise OptimizationValidationError(
            "market_model covariance requires aligned market benchmark returns"
        )
    market = np.asarray(market_returns, dtype=float).reshape(-1)
    if market.shape[0] != returns.shape[0]:
        raise OptimizationValidationError(
            "market benchmark returns are not aligned with the return matrix"
        )
    if market.shape[0] < 3:
        raise OptimizationValidationError(
            "market_model covariance needs at least three aligned benchmark observations"
        )
    market_variance = float(np.var(market, ddof=1))
    if not np.isfinite(market_variance) or market_variance <= 0:
        raise OptimizationValidationError("market benchmark returns have no usable variance")
    centered = market - float(np.mean(market))
    betas = np.array(
        [
            float(np.dot(returns[:, position] - float(np.mean(returns[:, position])), centered))
            / ((market.shape[0] - 1) * market_variance)
            for position in range(returns.shape[1])
        ],
        dtype=float,
    )
    residual = np.var(returns, axis=0, ddof=1) - (betas**2) * market_variance
    warnings = ["covariance estimated with the market-model (single-index) method"]
    floor = 1e-12
    if np.any(residual < floor):
        residual = np.clip(residual, floor, None)
        warnings.append("negative market-model residual variance floored to keep the matrix usable")
    matrix = np.outer(betas, betas) * market_variance + np.diag(residual)
    return matrix, warnings


def log_returns(
    prices: NDArray,
    *,
    winsorize: bool = True,
) -> tuple[NDArray, tuple[str, ...]]:
    if prices.ndim != 2 or prices.shape[0] < 2 or prices.shape[1] == 0:
        raise OptimizationValidationError("at least two aligned price observations are required")
    if np.any(prices <= 0) or not np.all(np.isfinite(prices)):
        raise OptimizationValidationError(
            "aligned prices contain a non-positive or non-finite value"
        )
    returns = np.diff(np.log(prices), axis=0)
    warnings: list[str] = []
    if winsorize and returns.shape[0] >= 20:
        lower = np.percentile(returns, 0.5, axis=0)
        upper = np.percentile(returns, 99.5, axis=0)
        clipped = np.clip(returns, lower, upper)
        if not np.array_equal(clipped, returns):
            warnings.append("returns winsorized at the 0.5th/99.5th percentiles")
        returns = clipped
    if not np.all(np.isfinite(returns)):
        raise OptimizationValidationError("return matrix contains a non-finite value")
    return returns, tuple(warnings)


def estimate_covariance(
    returns: NDArray,
    instruments: Sequence[str],
    *,
    method: str,
    interval: str,
    periods_per_year: int,
    ewma_halflife: int = 20,
    shrinkage: float = 0.10,
    market_returns: Sequence[float] | NDArray | None = None,
) -> CovarianceResult:
    """Estimate a period covariance matrix and validate it for the solver.

    ``market_returns`` is the aligned return series of the configured market
    benchmark (an index series from PI-02 market data).  It is required by the
    ``market_model`` method - a single-index covariance - and ignored by the
    sample/EWMA/shrinkage estimators, so a benchmark can ride along without
    changing their behaviour.
    """
    if returns.shape[1] != len(instruments) or returns.shape[0] < 2:
        raise OptimizationValidationError("covariance inputs are not a complete return matrix")
    warnings: list[str] = []
    if method == CovarianceMethod.MARKET_MODEL.value:
        matrix, model_warnings = _market_model_covariance(returns, market_returns)
        warnings.extend(model_warnings)
    elif method == CovarianceMethod.EWMA.value:
        matrix = _ewma_covariance(returns, ewma_halflife)
    else:
        matrix = np.cov(returns, rowvar=False, ddof=1)
        matrix = np.atleast_2d(matrix)
        if method == CovarianceMethod.SHRINKAGE.value:
            diagonal = np.diag(np.diag(matrix))
            matrix = (1.0 - shrinkage) * matrix + shrinkage * diagonal
    matrix = (matrix + matrix.T) / 2.0
    if not np.all(np.isfinite(matrix)):
        raise OptimizationValidationError("covariance matrix contains a non-finite value")
    eigenvalues = np.linalg.eigvalsh(matrix)
    minimum = float(eigenvalues[0]) if eigenvalues.size else 0.0
    if minimum < 1e-10:
        ridge = max(1e-8, abs(minimum) + 1e-8)
        matrix = matrix + np.eye(matrix.shape[0]) * ridge
        warnings.append(f"covariance was singular or near-singular; ridge {ridge:.2e} applied")
    eigenvalues = np.linalg.eigvalsh(matrix)
    maximum = float(eigenvalues[-1]) if eigenvalues.size else 0.0
    minimum = float(eigenvalues[0]) if eigenvalues.size else 0.0
    condition = maximum / minimum if minimum > 0 else float("inf")
    if not math.isfinite(condition) or condition > 1e14:
        raise OptimizationValidationError("covariance matrix is numerically unusable")
    diagonal = np.sqrt(np.clip(np.diag(matrix), 0.0, None))
    denominator = np.outer(diagonal, diagonal)
    with np.errstate(divide="ignore", invalid="ignore"):
        correlation = np.divide(
            matrix,
            denominator,
            out=np.zeros_like(matrix),
            where=denominator > 0,
        )
    correlation = np.clip((correlation + correlation.T) / 2.0, -1.0, 1.0)
    np.fill_diagonal(correlation, 1.0)
    if condition > 1e10:
        warnings.append(f"covariance is ill-conditioned (condition {condition:.2e})")
    return CovarianceResult(
        instruments=tuple(instruments),
        matrix=tuple(tuple(float(value) for value in row) for row in matrix),
        correlation=tuple(tuple(float(value) for value in row) for row in correlation),
        observations=int(returns.shape[0]),
        method=method,
        interval=interval,
        periods_per_year=periods_per_year,
        condition_number=float(condition),
        warnings=tuple(warnings),
    )


def _ewma_covariance(returns: NDArray, halflife: int) -> NDArray:
    count = returns.shape[0]
    decay = 0.5 ** (1.0 / max(2, halflife))
    weights = np.array([decay ** (count - 1 - index) for index in range(count)], dtype=float)
    weights /= weights.sum()
    mean = np.average(returns, axis=0, weights=weights)
    centered = returns - mean
    denominator = 1.0 - float(np.sum(weights**2))
    if denominator <= 0:
        denominator = 1.0
    weighted = centered * np.sqrt(weights)[:, None]
    result: NDArray = (weighted.T @ weighted) / denominator
    return result


def correlation_stability(returns: NDArray) -> dict[str, Any]:
    count, assets = returns.shape
    if assets < 2 or count < 6:
        return {
            "available": False,
            "reason": "at least two assets and six aligned observations are required",
        }
    split = count // 2
    first = np.corrcoef(returns[:split], rowvar=False)
    second = np.corrcoef(returns[split:], rowvar=False)
    if np.any(np.isnan(first)) or np.any(np.isnan(second)):
        return {"available": False, "reason": "a split-window correlation is undefined"}
    mask = ~np.eye(assets, dtype=bool)
    changes = np.abs(first - second)[mask]
    return {
        "available": True,
        "mean_absolute_change": float(np.mean(changes)),
        "max_absolute_change": float(np.max(changes)),
        "first_half_observations": int(split),
        "second_half_observations": int(count - split),
        "definition": "absolute change in pairwise correlation between equal halves",
    }


def portfolio_return_series(returns: NDArray, weights: Sequence[float] | NDArray) -> NDArray:
    vector = np.asarray(weights, dtype=float)
    if vector.size != returns.shape[1]:
        raise OptimizationValidationError("weight vector does not match the return matrix")
    result: NDArray = returns @ vector
    return result


def risk_metrics(
    *,
    weights: Mapping[str, float],
    covariance: CovarianceResult,
    return_matrix: NDArray,
    expected_returns: Mapping[str, float | None],
    horizon_periods: int,
    risk_free_rate: float,
    sectors: Mapping[str, str],
) -> dict[str, Any]:
    instruments = covariance.instruments
    vector = np.array([float(weights.get(item, 0.0)) for item in instruments])
    covariance_array = np.asarray(covariance.matrix, dtype=float)
    variance = float(vector @ covariance_array @ vector)
    period_vol = math.sqrt(max(variance, 0.0))
    horizon_vol = period_vol * math.sqrt(max(1, horizon_periods))
    annual_vol = period_vol * math.sqrt(covariance.periods_per_year)
    expected = sum(
        float(weights.get(item, 0.0)) * float(expected_returns.get(item) or 0.0)
        for item in instruments
    )
    years = max(1, horizon_periods) / covariance.periods_per_year
    risk_free = risk_free_rate * years
    sharpe = (expected - risk_free) / horizon_vol if horizon_vol > 0 else None
    series = portfolio_return_series(return_matrix, vector)
    equity = np.cumprod(1.0 + series)
    peak = np.maximum.accumulate(equity)
    drawdowns = equity / peak - 1.0
    max_drawdown = float(drawdowns.min()) if drawdowns.size else 0.0
    annual_return = float(np.mean(series)) * covariance.periods_per_year if series.size else 0.0
    downside = series[series < 0]
    downside_dev = (
        float(np.sqrt(np.mean(downside**2)) * math.sqrt(covariance.periods_per_year))
        if downside.size
        else None
    )
    sortino = (
        (annual_return - risk_free_rate) / downside_dev
        if downside_dev and downside_dev > 0
        else None
    )
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else None
    var_95 = (
        float(np.percentile(series, 5)) * math.sqrt(max(1, horizon_periods))
        if series.size >= 20
        else None
    )
    tail = series[series <= np.percentile(series, 5)] if series.size >= 20 else np.array([])
    cvar_95 = float(np.mean(tail)) * math.sqrt(max(1, horizon_periods)) if tail.size else None
    marginal = covariance_array @ vector
    contributions: dict[str, float] = {}
    for index, instrument_id in enumerate(instruments):
        contribution = float(vector[index] * marginal[index] / variance) if variance > 0 else 0.0
        contributions[instrument_id] = contribution
    sector_weights: dict[str, float] = {}
    for instrument_id, weight in weights.items():
        sector = sectors.get(instrument_id, "unknown")
        sector_weights[sector] = sector_weights.get(sector, 0.0) + float(weight)
    return {
        "expected_return": expected,
        "portfolio_volatility_period": period_vol,
        "portfolio_volatility_horizon": horizon_vol,
        "annualized_volatility": annual_vol,
        "risk_free_rate": risk_free_rate,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "annualized_return_estimate": annual_return,
        "max_drawdown": max_drawdown,
        "historical_observations": int(series.size),
        "var_95": var_95,
        "cvar_95": cvar_95,
        "downside_deviation": downside_dev,
        "risk_contributions": contributions,
        "sector_weights": sector_weights,
        "assumptions": [
            "risk metrics are sample estimates, not guarantees against future loss",
            "VaR/CVaR use the historical aligned return window only",
            "annualization uses the selected observation interval",
        ],
    }


def diversification_metrics(
    *,
    weights: Mapping[str, float],
    covariance: CovarianceResult,
    sectors: Mapping[str, str],
    max_sector_weight: float,
) -> dict[str, Any]:
    clean = {key: max(0.0, float(value)) for key, value in weights.items() if value > 0}
    total = sum(clean.values())
    if total <= 0:
        return {
            "holdings": 0,
            "hhi": 0.0,
            "effective_holdings": 0.0,
            "top_weight": 0.0,
            "sector_hhi": 0.0,
            "effective_sectors": 0.0,
            "average_correlation": None,
            "diversification_ratio": None,
            "warnings": ["no invested allocation"],
            "definition": "weights are target invested weights, cash is excluded",
        }
    normalized = {key: value / total for key, value in clean.items()}
    hhi = sum(value**2 for value in normalized.values())
    effective = 1.0 / hhi if hhi > 0 else 0.0
    sector_weights: dict[str, float] = {}
    for instrument_id, weight in normalized.items():
        sector = sectors.get(instrument_id, "unknown")
        sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
    # Sector caps are configured against total portfolio value, so the cap check
    # uses absolute exposures rather than invested-only shares; otherwise any
    # single-holding book reports 100% in one sector and warns regardless of cap.
    sector_exposure: dict[str, float] = {}
    for instrument_id, weight in clean.items():
        sector = sectors.get(instrument_id, "unknown")
        sector_exposure[sector] = sector_exposure.get(sector, 0.0) + weight
    sector_hhi = sum(value**2 for value in sector_weights.values())
    effective_sectors = 1.0 / sector_hhi if sector_hhi > 0 else 0.0
    instruments = [item for item in covariance.instruments if item in normalized]
    average_correlation: float | None = None
    if len(instruments) >= 2:
        indexes = [covariance.instruments.index(item) for item in instruments]
        corr = np.asarray(covariance.correlation, dtype=float)[np.ix_(indexes, indexes)]
        mask = ~np.eye(len(instruments), dtype=bool)
        values = corr[mask]
        average_correlation = float(np.mean(values)) if values.size else None
    weighted_vol = sum(
        normalized.get(item, 0.0)
        * math.sqrt(
            max(
                float(
                    covariance.matrix[covariance.instruments.index(item)][
                        covariance.instruments.index(item)
                    ]
                ),
                0.0,
            )
        )
        for item in instruments
    )
    variance = 0.0
    for left in instruments:
        for right in instruments:
            i = covariance.instruments.index(left)
            j = covariance.instruments.index(right)
            variance += (
                normalized.get(left, 0.0)
                * normalized.get(right, 0.0)
                * float(covariance.matrix[i][j])
            )
    portfolio_vol = math.sqrt(max(variance, 0.0))
    diversification_ratio = weighted_vol / portfolio_vol if portfolio_vol > 0 else None
    warnings: list[str] = []
    if average_correlation is not None and average_correlation > 0.65:
        warnings.append("average pairwise correlation is high; holdings may not diversify")
    if effective < 3 and len(normalized) >= 3:
        warnings.append("effective number of holdings is below three")
    if max(sector_exposure.values(), default=0.0) > max_sector_weight + 1e-9:
        warnings.append("target sector weight exceeds the configured cap")
    if diversification_ratio is not None and diversification_ratio < 1.1 and len(normalized) > 2:
        warnings.append("diversification ratio is close to one")
    return {
        "holdings": len(normalized),
        "hhi": hhi,
        "effective_holdings": effective,
        "top_weight": max(normalized.values()),
        "sector_weights": sector_weights,
        "sector_hhi": sector_hhi,
        "effective_sectors": effective_sectors,
        "average_correlation": average_correlation,
        "diversification_ratio": diversification_ratio,
        "warnings": warnings,
        "definition": (
            "HHI is the sum of squared invested weights; effective holdings is 1/HHI; "
            "diversification ratio is weighted volatility divided by portfolio volatility"
        ),
    }


def liquidity_assessment(
    *,
    weights: Mapping[str, float],
    instruments: Sequence[str],
    series: Mapping[str, PriceSeries],
    total_value: float,
    max_participation_rate: float,
    policy: str,
    bars_per_day: float = 1.0,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for instrument_id in instruments:
        weight = float(weights.get(instrument_id, 0.0))
        target_value = total_value * weight
        prices = series.get(instrument_id)
        if prices is None or not prices.volume:
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "target_value": target_value,
                    "average_daily_notional": None,
                    "participation_cap": None,
                    "status": "missing",
                    "policy": policy,
                    "reasons": ["no volume observations; liquidity cannot be confirmed"],
                }
            )
            continue
        notional = [
            float(volume) * price
            for volume, price in zip(prices.volume, prices.close, strict=False)
            if volume is not None and price > 0
        ]
        average_bar = sum(notional) / len(notional) if notional else None
        average = average_bar * max(1.0, float(bars_per_day)) if average_bar is not None else None
        cap = average * max_participation_rate if average is not None else None
        status = "estimated" if average is not None else "missing"
        reasons: list[str] = []
        if cap is not None and target_value > cap:
            reasons.append("target value exceeds the configured participation cap")
            status = "constrained"
        rows.append(
            {
                "instrument_id": instrument_id,
                "target_value": target_value,
                "average_daily_notional": average,
                "participation_cap": cap,
                "status": status,
                "policy": policy,
                "reasons": reasons,
            }
        )
    return {
        "rows": rows,
        "participation_rate": max_participation_rate,
        "definition": (
            "average daily notional is estimated from stored bar volume x close; "
            "this is not confirmed executable capacity"
        ),
        "assumptions": [
            "historical volume is used as a conservative liquidity proxy",
            "participation cap is a fraction of average daily notional",
        ],
    }


def scenario_analysis(
    *,
    weights: Mapping[str, float],
    covariance: CovarianceResult,
    return_matrix: NDArray,
    total_value: float,
    sectors: Mapping[str, str],
    horizon_periods: int,
    expected_returns: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    instruments = list(covariance.instruments)
    vector = np.array([float(weights.get(item, 0.0)) for item in instruments])
    rows: list[dict[str, Any]] = []
    if vector.sum() > 0 and return_matrix.size:
        series = portfolio_return_series(return_matrix, vector)
        window = max(1, min(horizon_periods, series.size))
        worst: tuple[float, int] | None = None
        for index in range(0, max(1, series.size - window + 1)):
            cumulative = float(np.prod(1.0 + series[index : index + window]) - 1.0)
            if worst is None or cumulative < worst[0]:
                worst = (cumulative, index)
        if worst is not None:
            rows.append(
                _scenario_payload(
                    name="historical_worst_window",
                    kind="historical",
                    description=f"worst aligned {window}-observation portfolio window",
                    change=total_value * worst[0],
                    total=total_value,
                    weights=weights,
                    sectors=sectors,
                    assumptions=["historical replay over the selected aligned window"],
                )
            )
        market_shock = -0.10
        rows.append(
            _shock_scenario(
                name="market_decline_10pct",
                description="broad market decline applied to all invested positions",
                shock=market_shock,
                weights=weights,
                total=total_value,
                sectors=sectors,
            )
        )
        sector_shock = -0.15
        sector_rows = _sector_shock(weights, sectors, sector_shock, total_value)
        rows.append(
            {
                **_scenario_base(
                    name="worst_sector_decline_15pct",
                    kind="hypothetical",
                    description="the most exposed sector falls 15%",
                ),
                "portfolio_value_change": sector_rows[0],
                "portfolio_value_change_pct": sector_rows[0] / total_value if total_value else 0.0,
                "position_contributions": sector_rows[1],
                "sector_contributions": sector_rows[2],
                "assumptions": ["sector shock is simultaneous and ignores correlations"],
            }
        )
        correlation_increase = _correlation_increase(
            vector, covariance, total_value, weights, sectors
        )
        rows.append(correlation_increase)
        rows.append(
            _shock_scenario(
                name="liquidity_deterioration",
                description="half of each position is assumed to face a 5% exit discount",
                shock=-0.05,
                weights={key: value * 0.5 for key, value in weights.items()},
                total=total_value,
                sectors=sectors,
            )
        )
        if weights:
            largest = max(weights, key=lambda item: float(weights[item]))
            rows.append(
                _shock_scenario(
                    name="largest_position_shock_20pct",
                    description=f"the largest target position ({largest}) falls 20%",
                    shock=-0.20,
                    weights={largest: float(weights[largest])},
                    total=total_value,
                    sectors=sectors,
                )
            )
        rows.append(
            _risk_scenario(
                name="volatility_increase_25pct",
                description="all instrument volatilities rise 25% with correlations unchanged",
                vector=vector,
                covariance=covariance,
                total=total_value,
                weights=weights,
                sectors=sectors,
                scale=1.25,
            )
        )
        expected = expected_returns or {}
        forecast_change = sum(
            float(weight) * float(expected.get(item, 0.0) or 0.0)
            for item, weight in weights.items()
        )
        if forecast_change:
            rows.append(
                _scenario_payload(
                    name="forecast_deterioration",
                    kind="hypothetical",
                    description="the portfolio's forecast-return contribution deteriorates by half",
                    change=-0.5 * total_value * abs(forecast_change),
                    total=total_value,
                    weights=weights,
                    sectors=sectors,
                    assumptions=[
                        "forecast deterioration is a hypothetical expected-return shock; "
                        "it is not a forecast"
                    ],
                )
            )
        rows.append(
            _scenario_payload(
                name="regime_transition",
                kind="hypothetical",
                description="hypothetical regime transition: broad 8% decline with no trading",
                change=-0.08 * total_value * sum(float(value) for value in weights.values()),
                total=total_value,
                weights=weights,
                sectors=sectors,
                assumptions=[
                    "regime transition is a combined hypothetical market shock; "
                    "it is not a regime forecast"
                ],
            )
        )
    return rows


def _scenario_base(*, name: str, kind: str, description: str) -> dict[str, Any]:
    return {
        "scenario_id": "scn_" + name,
        "name": name,
        "kind": kind,
        "description": description,
        "portfolio_value_change": 0.0,
        "portfolio_value_change_pct": 0.0,
        "position_contributions": [],
        "sector_contributions": [],
        "warnings": [],
        "assumptions": [],
        "is_forecast": False,
        "is_hypothetical": kind == "hypothetical",
    }


def _scenario_payload(
    *,
    name: str,
    kind: str,
    description: str,
    change: float,
    total: float,
    weights: Mapping[str, float],
    sectors: Mapping[str, str],
    assumptions: Sequence[str],
) -> dict[str, Any]:
    invested = sum(float(value) for value in weights.values())
    return {
        **_scenario_base(name=name, kind=kind, description=description),
        "portfolio_value_change": change,
        "portfolio_value_change_pct": change / total if total else 0.0,
        "position_contributions": [
            {
                "instrument_id": item,
                "weight": float(weight),
                # ``change`` is already the total portfolio impact.  Allocate it
                # by each position's share of invested capital; multiplying the
                # change by the raw portfolio weight would double-apply cash and
                # understate the contributions of an invested book.
                "value_change": change * float(weight) / invested if invested > 0 else 0.0,
            }
            for item, weight in weights.items()
        ],
        "sector_contributions": _sector_contributions(weights, sectors, change),
        "assumptions": list(assumptions),
    }


def _shock_scenario(
    *,
    name: str,
    description: str,
    shock: float,
    weights: Mapping[str, float],
    total: float,
    sectors: Mapping[str, str],
) -> dict[str, Any]:
    change = total * sum(float(value) for value in weights.values()) * shock
    return _scenario_payload(
        name=name,
        kind="hypothetical",
        description=description,
        change=change,
        total=total,
        weights=weights,
        sectors=sectors,
        assumptions=["simultaneous shock with no trading during the scenario"],
    )


def _sector_contributions(
    weights: Mapping[str, float], sectors: Mapping[str, str], total_change: float
) -> list[dict[str, Any]]:
    grouped: dict[str, float] = {}
    for instrument_id, weight in weights.items():
        sector = sectors.get(instrument_id, "unknown")
        grouped[sector] = grouped.get(sector, 0.0) + float(weight)
    total = sum(grouped.values())
    return [
        {
            "sector": sector,
            "weight": weight,
            "value_change": total_change * (weight / total) if total > 0 else 0.0,
        }
        for sector, weight in sorted(grouped.items())
    ]


def _sector_shock(
    weights: Mapping[str, float],
    sectors: Mapping[str, str],
    shock: float,
    total: float,
) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, float] = {}
    for instrument_id, weight in weights.items():
        sector = sectors.get(instrument_id, "unknown")
        grouped[sector] = grouped.get(sector, 0.0) + float(weight)
    if not grouped:
        return 0.0, [], []
    worst_sector = min(grouped, key=lambda key: (grouped[key], key))
    change = total * grouped[worst_sector] * shock
    contributions = [
        {
            "instrument_id": instrument_id,
            "sector": sectors.get(instrument_id, "unknown"),
            "weight": float(weight),
            "value_change": total * float(weight) * shock
            if sectors.get(instrument_id, "unknown") == worst_sector
            else 0.0,
        }
        for instrument_id, weight in weights.items()
    ]
    sector_rows = [
        {
            "sector": sector,
            "weight": weight,
            "value_change": change if sector == worst_sector else 0.0,
        }
        for sector, weight in sorted(grouped.items())
    ]
    return change, contributions, sector_rows


def _risk_scenario(
    *,
    name: str,
    description: str,
    vector: NDArray,
    covariance: CovarianceResult,
    total: float,
    weights: Mapping[str, float],
    sectors: Mapping[str, str],
    scale: float,
) -> dict[str, Any]:
    # Scaling variances by scale**2 scales volatilities by ``scale`` while
    # leaving the implied correlation matrix unchanged.
    matrix = np.asarray(covariance.matrix, dtype=float) * (scale**2)
    base = math.sqrt(max(float(vector @ np.asarray(covariance.matrix, dtype=float) @ vector), 0.0))
    stressed = math.sqrt(max(float(vector @ matrix @ vector), 0.0))
    return {
        **_scenario_base(name=name, kind="hypothetical", description=description),
        "portfolio_value_change": 0.0,
        "portfolio_value_change_pct": 0.0,
        "base_portfolio_volatility": base,
        "stressed_portfolio_volatility": stressed,
        "volatility_change": stressed - base,
        "position_contributions": [
            {"instrument_id": item, "weight": float(weight), "value_change": 0.0}
            for item, weight in weights.items()
        ],
        "sector_contributions": _sector_contributions(weights, sectors, 0.0),
        "warnings": ["volatility shock changes risk; no deterministic value change is assumed"],
        "assumptions": ["instrument volatilities scale together; correlations are unchanged"],
    }


def _correlation_increase(
    vector: NDArray,
    covariance: CovarianceResult,
    total: float,
    weights: Mapping[str, float],
    sectors: Mapping[str, str],
) -> dict[str, Any]:
    corr = np.asarray(covariance.correlation, dtype=float)
    stress = np.where(np.eye(corr.shape[0], dtype=bool), 1.0, np.maximum(corr, 0.80))
    diagonal = np.sqrt(np.clip(np.diag(np.asarray(covariance.matrix, dtype=float)), 0.0, None))
    stressed = stress * np.outer(diagonal, diagonal)
    base_variance = float(vector @ np.asarray(covariance.matrix, dtype=float) @ vector)
    stressed_variance = float(vector @ stressed @ vector)
    base_vol = math.sqrt(max(base_variance, 0.0))
    stressed_vol = math.sqrt(max(stressed_variance, 0.0))
    return {
        **_scenario_base(
            name="correlation_increase",
            kind="hypothetical",
            description="all positive pairwise correlations rise to at least 0.80",
        ),
        # A pure correlation change has no deterministic price effect.  Report
        # the risk increase explicitly instead of inventing a portfolio loss.
        "portfolio_value_change": 0.0,
        "portfolio_value_change_pct": 0.0,
        "base_portfolio_volatility": base_vol,
        "stressed_portfolio_volatility": stressed_vol,
        "volatility_change": stressed_vol - base_vol,
        "position_contributions": [
            {"instrument_id": item, "weight": float(weight), "value_change": 0.0}
            for item, weight in weights.items()
        ],
        "sector_contributions": _sector_contributions(weights, sectors, 0.0),
        "warnings": ["correlation change affects risk, not a deterministic value change"],
        "assumptions": ["volatility is held at its historical estimate; only correlation changes"],
    }


def backtest_metrics(
    equity: Sequence[float],
    returns: Sequence[float],
    *,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    if len(equity) < 2:
        return {
            "observations": len(equity),
            "sufficient": False,
            "reason": "fewer than two equity observations",
        }
    periods = max(1, int(periods_per_year))
    values = np.asarray(equity, dtype=float)
    series = np.asarray(returns, dtype=float)
    peak = np.maximum.accumulate(values)
    drawdowns = values / peak - 1.0
    total_return = values[-1] / values[0] - 1.0
    annualized = (
        (1.0 + total_return) ** (periods / max(1, len(values) - 1)) - 1.0
        if total_return > -1
        else None
    )
    volatility = float(np.std(series, ddof=1) * math.sqrt(periods)) if series.size > 1 else None
    downside = series[series < 0]
    downside_dev = (
        float(np.sqrt(np.mean(downside**2)) * math.sqrt(periods)) if downside.size else None
    )
    sharpe = (
        (float(np.mean(series)) * periods) / volatility if volatility and volatility > 0 else None
    )
    sortino = (
        (float(np.mean(series)) * periods) / downside_dev
        if downside_dev and downside_dev > 0
        else None
    )
    max_drawdown = float(drawdowns.min()) if drawdowns.size else 0.0
    calmar = annualized / abs(max_drawdown) if annualized is not None and max_drawdown < 0 else None
    return {
        "observations": int(len(values)),
        "sufficient": True,
        "total_return": float(total_return),
        "annualized_return": annualized,
        "volatility": volatility,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "max_drawdown": max_drawdown,
        "periods_per_year": periods,
        "equity_start": float(values[0]),
        "equity_end": float(values[-1]),
        "definition": "equity is marked at each observation after costs and slippage",
    }


__all__ = [
    "align_benchmark",
    "aligned_price_matrix",
    "backtest_metrics",
    "correlation_stability",
    "diversification_metrics",
    "estimate_covariance",
    "liquidity_assessment",
    "log_returns",
    "portfolio_return_series",
    "risk_metrics",
    "scenario_analysis",
]
