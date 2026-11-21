"""Constraint-aware allocation solvers for PI-18.

The implementation is deliberately dependency-light: numpy plus a deterministic
projected-gradient/projection loop.  Every method returns an explicit status;
an infeasible or non-finite solve is never silently replaced by a different
strategy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .models import (
    SOLVER_VERSION,
    AllocationMethod,
    OptimizationConfig,
    OptimizationInfeasibleError,
    OptimizationObjective,
    OptimizationStateError,
    OptimizationValidationError,
)

NDArray = np.ndarray[Any, Any]


def _project_box_sum(
    values: NDArray,
    *,
    upper: NDArray,
    total: float,
) -> NDArray:
    """Euclidean projection onto 0 <= w <= upper and sum(w) <= total."""
    if total <= 0:
        return np.zeros_like(values)
    if float(np.clip(values, 0.0, upper).sum()) <= total:
        return np.clip(values, 0.0, upper)
    low = float(np.min(values - upper))
    high = float(np.max(values))
    for _ in range(80):
        middle = (low + high) / 2.0
        candidate = np.clip(values - middle, 0.0, upper)
        if float(candidate.sum()) > total:
            low = middle
        else:
            high = middle
    return np.clip(values - high, 0.0, upper)


def _project_sector(
    values: NDArray,
    *,
    sectors: Sequence[str],
    caps: Mapping[str, float],
) -> NDArray:
    candidate = np.clip(values, 0.0, None)
    for sector, cap in caps.items():
        indexes = [index for index, item in enumerate(sectors) if item == sector]
        if not indexes:
            continue
        amount = float(candidate[indexes].sum())
        if amount > cap and amount > 0:
            candidate[indexes] *= cap / amount
    return candidate


def project_feasible(
    values: NDArray,
    *,
    upper: NDArray,
    total: float,
    sectors: Sequence[str],
    sector_caps: Mapping[str, float],
    iterations: int = 120,
) -> NDArray:
    """Dykstra-style projection onto box/sum and sector-cap halfspaces."""
    candidate = np.clip(values, 0.0, upper)
    correction_sum = np.zeros_like(candidate)
    correction_sector = np.zeros_like(candidate)
    for _ in range(max(10, iterations)):
        previous = candidate.copy()
        shifted = candidate + correction_sum
        projected_sum = _project_box_sum(shifted, upper=upper, total=total)
        correction_sum = shifted - projected_sum
        shifted = projected_sum + correction_sector
        projected_sector = _project_sector(shifted, sectors=sectors, caps=sector_caps)
        correction_sector = shifted - projected_sector
        candidate = projected_sector
        if float(np.max(np.abs(candidate - previous))) < 1e-10:
            break
    # The final halfspace is applied last so an ill-conditioned correction can
    # never return a sector-over-cap vector.
    candidate = _project_box_sum(candidate, upper=upper, total=total)
    candidate = _project_sector(candidate, sectors=sectors, caps=sector_caps)
    if float(candidate.sum()) > total + 1e-8:
        candidate = _project_box_sum(candidate, upper=upper, total=total)
    return np.clip(candidate, 0.0, upper)


def _allocatable_capacity(
    upper: NDArray,
    sectors: Sequence[str],
    sector_caps: Mapping[str, float],
) -> float:
    """The largest weight total the instrument and sector caps permit."""
    capacity = float(np.sum(upper))
    sector_upper: dict[str, float] = {}
    for index, sector in enumerate(sectors):
        sector_upper[sector] = sector_upper.get(sector, 0.0) + float(upper[index])
    for sector, available in sector_upper.items():
        cap = sector_caps.get(sector)
        if cap is not None and cap < available:
            capacity -= available - max(0.0, float(cap))
    return max(0.0, capacity)


def _asset_mu(assets: Sequence[Any]) -> NDArray:
    values: list[float] = []
    for asset in assets:
        forecast = getattr(asset, "forecast", None)
        values.append(float(forecast.adjusted_return) if forecast is not None else 0.0)
    return np.array(values, dtype=float)


def _objective_gradient(
    weights: NDArray,
    *,
    objective: str,
    covariance: NDArray,
    mu: NDArray,
    current: NDArray,
    sectors: Sequence[str],
    config: OptimizationConfig,
    downside: NDArray | None,
) -> NDArray:
    variance = float(weights @ covariance @ weights)
    if objective == OptimizationObjective.MIN_VARIANCE.value:
        result: NDArray = 2.0 * covariance @ weights
        return result
    if objective == OptimizationObjective.MAX_EXPECTED_RETURN.value:
        return -mu
    if objective == OptimizationObjective.RISK_ADJUSTED.value:
        result = -mu + 2.0 * config.risk_aversion * covariance @ weights
        return result
    if objective == OptimizationObjective.MAX_SHARPE.value:
        volatility = math.sqrt(max(variance, 1e-16))
        numerator = float(mu @ weights)
        return mu / volatility - numerator * (covariance @ weights) / (volatility**3)
    if objective == OptimizationObjective.MIN_DOWNSIDE_RISK.value:
        matrix = downside if downside is not None else covariance
        return 2.0 * matrix @ weights - 0.25 * mu
    if objective == OptimizationObjective.DIVERSIFICATION.value:
        count = max(1, len(weights))
        concentration = 2.0 * (weights - 1.0 / count)
        result = covariance @ weights + 0.15 * concentration
        return result
    # Multi-objective: each term is normalized to a comparable scale.
    count = max(1, len(weights))
    return_gradient = -mu / max(1e-8, float(np.max(np.abs(mu)) or 1.0))
    risk_scale = max(1e-12, float(np.max(np.abs(covariance))))
    risk_gradient = (covariance @ weights) / risk_scale
    concentration = 2.0 * (weights - 1.0 / count)
    turnover = weights - current
    liquidity = np.maximum(0.0, weights - _liquidity_upper(config, assets_count=count))
    weights_config = config.objective_weights
    result = (
        float(weights_config.get("return", 0.0)) * return_gradient
        + float(weights_config.get("risk", 0.0)) * risk_gradient
        + float(weights_config.get("diversification", 0.0)) * concentration
        + float(weights_config.get("turnover", 0.0)) * turnover
        + float(weights_config.get("liquidity", 0.0)) * liquidity
    )
    return result


def _liquidity_upper(config: OptimizationConfig, *, assets_count: int) -> NDArray:
    return np.full(assets_count, config.max_instrument_weight, dtype=float)


def _cap_turnover(
    target: NDArray,
    current: NDArray,
    *,
    maximum: float,
) -> tuple[NDArray, float]:
    turnover = 0.5 * float(np.sum(np.abs(target - current)))
    if turnover <= maximum or turnover <= 0:
        return target, turnover
    scale = maximum / turnover
    return current + (target - current) * scale, maximum


def _drop_small(weights: NDArray, minimum: float) -> tuple[NDArray, bool]:
    if minimum <= 0:
        return weights, False
    result = weights.copy()
    dropped = False
    for index, value in enumerate(result):
        if 0 < value < minimum:
            result[index] = 0.0
            dropped = True
    return result, dropped


def solve_weights(
    assets: Sequence[Any],
    *,
    covariance: NDArray,
    config: OptimizationConfig,
    current_weights: Mapping[str, float] | None = None,
    downside: NDArray | None = None,
    total_value: float = 1.0,
    investable_override: float | None = None,
    max_iterations: int = 600,
    cancelled: Any | None = None,
) -> dict[str, Any]:
    if not assets:
        raise OptimizationValidationError("no eligible assets were supplied to the solver")
    count = len(assets)
    if covariance.shape != (count, count):
        raise OptimizationValidationError("covariance dimensions do not match eligible assets")
    upper = np.full(count, config.max_instrument_weight, dtype=float)
    for index, asset in enumerate(assets):
        cap = getattr(asset, "liquidity_cap_value", None)
        if cap is not None and cap > 0:
            upper[index] = min(upper[index], cap / max(1e-9, total_value))
        elif cap is None and config.liquidity_policy == "warn":
            upper[index] = min(upper[index], config.max_instrument_weight)
        else:
            upper[index] = 0.0
    total = (
        config.investable_fraction
        if investable_override is None
        else max(0.0, min(config.investable_fraction, investable_override))
    )
    sectors = [getattr(asset, "sector", "unknown") for asset in assets]
    sector_caps = {
        sector: min(
            config.max_sector_weight,
            float(config.sector_limits.get(sector, config.max_sector_weight)),
        )
        for sector in set(sectors)
    }
    capacity = _allocatable_capacity(upper, sectors, sector_caps)
    if total > 0 and capacity <= 1e-9:
        raise OptimizationInfeasibleError(
            "every eligible instrument is capped at zero weight by the configured "
            "instrument, sector or liquidity constraints; no allocation can "
            "satisfy them"
        )
    current = np.array(
        [float((current_weights or {}).get(asset.instrument_id, 0.0)) for asset in assets],
        dtype=float,
    )
    mu = _asset_mu(assets)
    method = config.method
    objective = config.objective
    warnings: list[str] = []
    if method == AllocationMethod.CURRENT.value:
        target = current.copy()
        if target.sum() > total:
            target *= total / target.sum()
            warnings.append("current allocation was scaled to the investable ceiling")
        target = project_feasible(
            target,
            upper=upper,
            total=total,
            sectors=sectors,
            sector_caps=sector_caps,
        )
        return {
            "status": "completed",
            "method": method,
            "objective": objective,
            "weights": _weights_dict(assets, target),
            "iterations": 0,
            "solver_version": SOLVER_VERSION,
            "warnings": tuple(warnings + ["current allocation is a reference baseline"]),
            "fallback_used": False,
        }
    if method == AllocationMethod.EQUAL_WEIGHT.value:
        seed = np.ones(count, dtype=float)
    elif method == AllocationMethod.INVERSE_VOLATILITY.value:
        variance = np.clip(np.diag(covariance), 1e-16, None)
        seed = 1.0 / np.sqrt(variance)
    else:
        seed = np.ones(count, dtype=float) / max(1, count)
    target = project_feasible(
        seed,
        upper=upper,
        total=total,
        sectors=sectors,
        sector_caps=sector_caps,
    )
    if method in {
        AllocationMethod.EQUAL_WEIGHT.value,
        AllocationMethod.INVERSE_VOLATILITY.value,
    }:
        iterations = 0
    elif method == AllocationMethod.RISK_PARITY.value:
        target = _risk_parity(
            covariance,
            target,
            upper=upper,
            total=total,
            sectors=sectors,
            sector_caps=sector_caps,
            iterations=max_iterations,
            cancelled=cancelled,
        )
        iterations = max_iterations
    elif method in {AllocationMethod.MEAN_VARIANCE.value, AllocationMethod.MIN_VARIANCE.value}:
        if method == AllocationMethod.MIN_VARIANCE.value:
            objective = OptimizationObjective.MIN_VARIANCE.value
        if objective == OptimizationObjective.MAX_EXPECTED_RETURN.value and not np.any(mu):
            raise OptimizationValidationError(
                "maximum expected return needs at least one nonzero expected return"
            )
        target, iterations = _gradient_solve(
            target,
            covariance=covariance,
            mu=mu,
            current=current,
            objective=objective,
            sectors=sectors,
            sector_caps=sector_caps,
            upper=upper,
            total=total,
            config=config,
            downside=downside,
            iterations=max_iterations,
            cancelled=cancelled,
        )
    else:
        raise OptimizationValidationError(f"unsupported allocation method {method!r}")
    if float(np.max(np.abs(target))) > 1.0 + 1e-8 or float(target.sum()) > total + 1e-8:
        raise OptimizationInfeasibleError(
            "the solver could not satisfy the configured weight and sector caps; "
            f"the allocation would exceed {total:.4f} of the portfolio"
        )
    if np.any(target < -1e-9):
        raise OptimizationValidationError("solver returned a negative weight")
    target = np.clip(target, 0.0, upper)
    target, dropped = _drop_small(target, config.min_position_weight)
    if dropped:
        target = project_feasible(
            target,
            upper=upper,
            total=total,
            sectors=sectors,
            sector_caps=sector_caps,
        )
        warnings.append("positions below the minimum weight were left in cash")
    pre_cap_turnover = 0.5 * float(np.sum(np.abs(target - current)))
    target, turnover = _cap_turnover(target, current, maximum=config.max_turnover)
    if pre_cap_turnover > config.max_turnover + 1e-12:
        warnings.append("turnover cap applied by blending toward the current book")
    if config.max_positions and int(np.count_nonzero(target > 0)) > config.max_positions:
        order = np.argsort(-target)
        keep = {int(index) for index in order[: config.max_positions]}
        target = np.array([value if index in keep else 0.0 for index, value in enumerate(target)])
        warnings.append("max_positions applied; remaining capital stays in cash")
    target = project_feasible(
        target,
        upper=upper,
        total=total,
        sectors=sectors,
        sector_caps=sector_caps,
    )
    target = np.clip(target, 0.0, 1.0)
    target[target < 1e-10] = 0.0
    if not np.all(np.isfinite(target)):
        raise OptimizationValidationError("solver returned non-finite weights")
    residual_turnover = 0.5 * float(np.sum(np.abs(target - current)))
    if residual_turnover > config.max_turnover + 1e-8:
        warnings.append(
            "turnover residual remains above the configured cap after projection; "
            "the allocation reports it rather than hiding the breach"
        )
    if config.max_positions and int(np.count_nonzero(target > 0)) > config.max_positions:
        warnings.append("position-count residual remains above the configured cap after projection")
    if config.min_position_weight > 0 and np.any(
        (target > 0) & (target < config.min_position_weight - 1e-12)
    ):
        warnings.append("some projected positions remain below the configured minimum weight")
    return {
        "status": "completed",
        "method": method,
        "objective": objective,
        "weights": _weights_dict(assets, target),
        "iterations": iterations,
        "solver_version": SOLVER_VERSION,
        "warnings": tuple(warnings),
        "fallback_used": False,
    }


def _risk_parity(
    covariance: NDArray,
    seed: NDArray,
    *,
    upper: NDArray,
    total: float,
    sectors: Sequence[str],
    sector_caps: Mapping[str, float],
    iterations: int,
    cancelled: Any | None,
) -> NDArray:
    weights = project_feasible(
        seed,
        upper=upper,
        total=total,
        sectors=sectors,
        sector_caps=sector_caps,
    )
    for step in range(max(20, iterations)):
        if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
            raise OptimizationStateError("optimization was cancelled")
        variance = float(weights @ covariance @ weights)
        marginal = covariance @ weights
        contributions = weights * marginal
        target = variance / max(1, len(weights))
        adjusted = weights.copy()
        for index, contribution in enumerate(contributions):
            if weights[index] <= 0:
                continue
            if contribution <= 1e-15:
                adjusted[index] = min(upper[index], weights[index] * 1.05)
            else:
                adjusted[index] = weights[index] * (target / contribution) ** 0.5
        candidate = project_feasible(
            adjusted,
            upper=upper,
            total=total,
            sectors=sectors,
            sector_caps=sector_caps,
        )
        if float(np.max(np.abs(candidate - weights))) < 1e-10:
            return candidate
        weights = 0.5 * weights + 0.5 * candidate if step < 20 else candidate
    return weights


def _gradient_solve(
    seed: NDArray,
    *,
    covariance: NDArray,
    mu: NDArray,
    current: NDArray,
    objective: str,
    sectors: Sequence[str],
    sector_caps: Mapping[str, float],
    upper: NDArray,
    total: float,
    config: OptimizationConfig,
    downside: NDArray | None,
    iterations: int,
    cancelled: Any | None,
) -> tuple[NDArray, int]:
    weights = project_feasible(
        seed,
        upper=upper,
        total=total,
        sectors=sectors,
        sector_caps=sector_caps,
    )
    best = weights.copy()
    best_score = float("inf")
    scale = max(
        1e-9,
        float(np.max(np.abs(covariance))),
        float(np.max(np.abs(mu))),
    )
    used = 0
    for step in range(max(30, iterations)):
        used = step + 1
        if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
            raise OptimizationStateError("optimization was cancelled")
        gradient = _objective_gradient(
            weights,
            objective=objective,
            covariance=covariance,
            mu=mu,
            current=current,
            sectors=sectors,
            config=config,
            downside=downside,
        )
        gradient = gradient / scale
        if not np.all(np.isfinite(gradient)):
            raise OptimizationValidationError("objective gradient became non-finite")
        rate = 0.35 / (1.0 + step / 80.0)
        candidate = project_feasible(
            weights - rate * gradient,
            upper=upper,
            total=total,
            sectors=sectors,
            sector_caps=sector_caps,
        )
        score = _objective_value(
            candidate,
            objective=objective,
            covariance=covariance,
            mu=mu,
            config=config,
            downside=downside,
        )
        if score < best_score:
            best_score = score
            best = candidate.copy()
        if float(np.max(np.abs(candidate - weights))) < 1e-10:
            weights = candidate
            break
        weights = candidate
    return best, used


def _objective_value(
    weights: NDArray,
    *,
    objective: str,
    covariance: NDArray,
    mu: NDArray,
    config: OptimizationConfig,
    downside: NDArray | None,
) -> float:
    variance = float(weights @ covariance @ weights)
    if objective == OptimizationObjective.MIN_VARIANCE.value:
        return variance
    if objective == OptimizationObjective.MAX_EXPECTED_RETURN.value:
        return -float(mu @ weights)
    if objective == OptimizationObjective.RISK_ADJUSTED.value:
        return config.risk_aversion * variance - float(mu @ weights)
    if objective == OptimizationObjective.MAX_SHARPE.value:
        return -float(mu @ weights) / math.sqrt(max(variance, 1e-16))
    if objective == OptimizationObjective.MIN_DOWNSIDE_RISK.value:
        matrix = downside if downside is not None else covariance
        return float(weights @ matrix @ weights) - 0.25 * float(mu @ weights)
    if objective == OptimizationObjective.DIVERSIFICATION.value:
        return variance + 0.15 * float(np.sum((weights - 1 / max(1, len(weights))) ** 2))
    return variance - float(mu @ weights) / max(1e-9, float(np.max(np.abs(mu)) or 1.0))


def _weights_dict(assets: Sequence[Any], weights: NDArray) -> dict[str, float]:
    return {
        asset.instrument_id: round(float(weight), 10)
        for asset, weight in zip(assets, weights, strict=False)
        if weight > 1e-10
    }


__all__ = ["project_feasible", "solve_weights"]
