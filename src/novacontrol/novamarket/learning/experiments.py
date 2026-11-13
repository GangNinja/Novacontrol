"""NM-13 candidate generation and reproducible evaluation.

Candidates come from a bounded grid or a seeded random search over declared
parameter bounds.  Evaluation reuses NM-11's real replay engine on a training
window and a separate validation window, applies costs through that engine, and
compares the candidate with the approved baseline.  A candidate cannot become
eligible on return alone: out-of-sample behaviour, drawdown, sensitivity and
risk-policy compatibility are all required.
"""

from __future__ import annotations

import random
import statistics
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from novacontrol.novamarket.backtesting import (
    BacktestConfig,
    BacktestEngine,
    BacktestError,
    HistoricalBar,
    SignalRecord,
    Trade,
    calculate_metrics,
    regime_analysis,
)
from novacontrol.novamarket.learning.models import digest
from novacontrol.novamarket.risk.models import RiskPolicy
from novacontrol.novamarket.strategies.catalog import (
    STRATEGIES,
    validate_config,
)

#: Declared bounds.  A parameter not listed is never perturbed.
PARAMETER_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "trend_sma": {
        "fast_period": (2, 50),
        "slow_period": (5, 200),
        "horizon_days": (1, 20),
    },
    "mean_reversion_rsi": {
        "rsi_period": (5, 30),
        "bb_period": (10, 50),
        "bb_k": (1.5, 3.0),
        "oversold": (20, 40),
        "overbought": (60, 80),
        "horizon_days": (1, 20),
    },
    "momentum_breakout": {
        "lookback": (5, 60),
        "volume_factor": (1.0, 2.5),
        "exit_period": (3, 30),
        "horizon_days": (1, 20),
    },
    "volatility_breakout": {
        "bb_period": (10, 50),
        "lookback": (5, 60),
        "contraction": (0.5, 0.95),
        "horizon_days": (1, 20),
    },
    "event_driven": {
        "trend_period": (5, 60),
        "horizon_days": (1, 20),
    },
    "forecast_assisted": {
        "trend_period": (5, 60),
        "min_return": (0.0, 0.05),
        "min_probability": (0.5, 0.75),
        "horizon_days": (1, 20),
    },
}

METHODS = ("grid", "random", "walk_forward")


def bounds_for(catalog_id: str, baseline: Mapping[str, Any]) -> dict[str, tuple[float, float]]:
    declared = dict(PARAMETER_BOUNDS.get(str(catalog_id), {}))
    if declared:
        return declared
    # An unknown catalog still has explicit bounds: the baseline value ±20%.
    derived: dict[str, tuple[float, float]] = {}
    for key, value in baseline.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        low = float(value) * 0.8
        high = float(value) * 1.2
        if isinstance(value, int):
            low = float(max(1, int(low)))
            high = float(max(low + 1, int(high)))
        derived[key] = (low, high)
    return derived


def _coerce(value: float, original: Any, low: float, high: float) -> Any:
    clipped = min(max(value, low), high)
    if isinstance(original, int) and not isinstance(original, bool):
        return int(round(clipped))
    return round(clipped, 6)


def _valid(catalog_id: str, config: Mapping[str, Any]) -> bool:
    try:
        validate_config(catalog_id, dict(config))
    except Exception:
        return False
    if catalog_id == "trend_sma":
        fast = config.get("fast_period")
        slow = config.get("slow_period")
        if isinstance(fast, int) and isinstance(slow, int) and fast >= slow:
            return False
    return True


def generate_candidates(
    catalog_id: str,
    baseline: Mapping[str, Any],
    *,
    count: int,
    method: str,
    seed: int,
) -> list[dict[str, Any]]:
    """Bounded, deterministic candidate configurations."""
    if method not in METHODS:
        raise ValueError("method must be one of " + ", ".join(METHODS))
    bounds = bounds_for(catalog_id, baseline)
    if not bounds:
        raise ValueError("no numeric strategy parameters can be varied")
    wanted = max(1, min(int(count), 25))
    rng = random.Random(int(seed))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = {digest(dict(baseline))}

    def add(config: dict[str, Any]) -> None:
        if not _valid(catalog_id, config):
            return
        key = digest(config)
        if key in seen:
            return
        seen.add(key)
        candidates.append(config)

    keys = sorted(bounds)
    if method == "grid":
        # A small deterministic lattice: baseline, low, high, midpoint per key.
        for _ in range(4):
            for key in keys:
                low, high = bounds[key]
                original = baseline.get(key)
                if not isinstance(original, (int, float)):
                    continue
                for fraction in (0.0, 0.5, 1.0):
                    config = dict(baseline)
                    config[key] = _coerce(
                        low + (high - low) * fraction, original, low, high
                    )
                    add(config)
                    if len(candidates) >= wanted:
                        return candidates
    else:
        attempts = 0
        while len(candidates) < wanted and attempts < wanted * 40:
            attempts += 1
            config = dict(baseline)
            for key in keys:
                original = baseline.get(key)
                if not isinstance(original, (int, float)):
                    continue
                low, high = bounds[key]
                if method == "walk_forward":
                    # Walk-forward varies one parameter at a time so each
                    # candidate has an interpretable sensitivity trail.
                    if key != keys[attempts % len(keys)]:
                        continue
                config[key] = _coerce(
                    rng.uniform(low, high), original, low, high
                )
            add(config)
    return candidates


def _config(
    *,
    name: str,
    catalog_id: str,
    version: str,
    config: Mapping[str, Any],
    instruments: Sequence[str],
    start: datetime,
    end: datetime,
    risk_policy: Mapping[str, Any],
    initial_capital: Decimal,
) -> BacktestConfig:
    return BacktestConfig(
        name=name,
        strategy_catalog_id=catalog_id,
        strategy_version=version,
        strategy_config=dict(config),
        instruments=tuple(instruments),
        start=start,
        end=end,
        initial_capital=initial_capital,
        risk_policy=dict(risk_policy),
    )


def _metrics(result: Any) -> dict[str, Any]:
    return dict(getattr(result, "metrics", {}) or {})


def _risk_compliance(
    config: Mapping[str, Any],
    policy: RiskPolicy,
    instruments: Sequence[str],
) -> dict[str, Any]:
    position = Decimal(str(config.get("max_position_weight", "0.25")))
    # The strategy configuration does not carry an exposure limit; the
    # backtest derives it from this same risk policy. Comparing against a
    # hard-coded 1.00 rejected every candidate under a 0.90 policy even though
    # the replay itself never exceeded the policy.
    gross = Decimal(
        str(config.get("max_gross_exposure", policy.max_invested_fraction))
    )
    restricted = sorted(set(instruments) & set(policy.restricted_instruments))
    reasons: list[str] = []
    if position > policy.max_position_weight:
        reasons.append(
            f"max_position_weight {position} exceeds policy "
            f"{policy.max_position_weight}"
        )
    if gross > policy.max_invested_fraction:
        reasons.append(
            f"max_gross_exposure {gross} exceeds policy "
            f"{policy.max_invested_fraction}"
        )
    if restricted:
        reasons.append("restricted instruments: " + ", ".join(restricted))
    return {
        "compliant": not reasons,
        "reasons": reasons,
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "limits": {
            "max_position_weight": str(policy.max_position_weight),
            "max_invested_fraction": str(policy.max_invested_fraction),
        },
    }


def _sensitivity(
    engine: BacktestEngine,
    config: Mapping[str, Any],
    base: BacktestConfig,
    bars: Sequence[HistoricalBar],
    bounds: Mapping[str, tuple[float, float]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for key, (low, high) in sorted(bounds.items()):
        original = config.get(key)
        if not isinstance(original, (int, float)):
            continue
        for label, factor in (("down", 0.9), ("up", 1.1)):
            variant = dict(config)
            variant[key] = _coerce(
                float(original) * factor, original, low, high
            )
            if variant == dict(config) or not _valid(base.strategy_catalog_id, variant):
                continue
            try:
                variant_config = BacktestConfig(
                    name=f"{base.name} sensitivity {key} {label}",
                    strategy_catalog_id=base.strategy_catalog_id,
                    strategy_version=base.strategy_version,
                    strategy_config=variant,
                    instruments=base.instruments,
                    start=base.start,
                    end=base.end,
                    initial_capital=base.initial_capital,
                    risk_policy=dict(base.risk_policy),
                )
                result = engine.run(variant_config, bars, force=True)
                rows.append(
                    {
                        "parameter": key,
                        "direction": label,
                        "value": variant[key],
                        "total_return": result.metrics.get("total_return"),
                        "max_drawdown": result.metrics.get("max_drawdown"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one variant never fails the run
                rows.append(
                    {
                        "parameter": key,
                        "direction": label,
                        "value": variant[key],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    returns = [
        float(row["total_return"])
        for row in rows
        if isinstance(row.get("total_return"), (int, float))
    ]
    return {
        "available": bool(rows),
        "variants": rows,
        "return_std": round(statistics.pstdev(returns), 8) if len(returns) > 1 else None,
        "return_range": (
            round(max(returns) - min(returns), 8) if len(returns) > 1 else None
        ),
        "warning": (
            "sensitivity variants share the validation window; they are not "
            "independent out-of-sample evidence"
        ),
    }


def evaluate_candidate(
    *,
    candidate_config: Mapping[str, Any],
    baseline_config: Mapping[str, Any],
    catalog_id: str,
    strategy_id: str,
    baseline_version: str,
    instruments: Sequence[str],
    train_start: datetime,
    train_end: datetime,
    validation_start: datetime,
    validation_end: datetime,
    bars: Sequence[HistoricalBar],
    engine: BacktestEngine,
    policy: RiskPolicy,
    risk_policy_snapshot: Mapping[str, Any],
    min_trades: int,
    min_observations: int,
    initial_capital: Decimal = Decimal("100000.00"),
) -> dict[str, Any]:
    """Run one candidate through training, validation and risk gates."""
    reasons: list[str] = []
    try:
        train_config = _config(
            name=f"learning {strategy_id} train",
            catalog_id=catalog_id,
            version=baseline_version,
            config=candidate_config,
            instruments=instruments,
            start=train_start,
            end=train_end,
            risk_policy=risk_policy_snapshot,
            initial_capital=initial_capital,
        )
        validation_config = _config(
            name=f"learning {strategy_id} validation",
            catalog_id=catalog_id,
            version=baseline_version,
            config=candidate_config,
            instruments=instruments,
            start=validation_start,
            end=validation_end,
            risk_policy=risk_policy_snapshot,
            initial_capital=initial_capital,
        )
        baseline_train_config = _config(
            name=f"baseline {strategy_id} train",
            catalog_id=catalog_id,
            version=baseline_version,
            config=baseline_config,
            instruments=instruments,
            start=train_start,
            end=train_end,
            risk_policy=risk_policy_snapshot,
            initial_capital=initial_capital,
        )
        baseline_validation_config = _config(
            name=f"baseline {strategy_id} validation",
            catalog_id=catalog_id,
            version=baseline_version,
            config=baseline_config,
            instruments=instruments,
            start=validation_start,
            end=validation_end,
            risk_policy=risk_policy_snapshot,
            initial_capital=initial_capital,
        )
        train = engine.run(train_config, bars, force=True)
        validation = engine.run(validation_config, bars, force=True)
        baseline_train = engine.run(baseline_train_config, bars, force=True)
        baseline_validation = engine.run(baseline_validation_config, bars, force=True)
    except Exception as exc:  # noqa: BLE001 - an evaluation failure is a record
        return {
            "ok": False,
            "eligible": False,
            "status": "evaluation_failed",
            "reasons": [f"{type(exc).__name__}: {exc}"],
            "train": {},
            "validation": {},
            "baseline": {},
        }

    train_metrics = _metrics(train)
    validation_metrics = _metrics(validation)
    baseline_metrics = _metrics(baseline_validation)
    baseline_train_metrics = _metrics(baseline_train)
    observations = int(validation_metrics.get("observations") or 0)
    trades = int(validation_metrics.get("trades") or 0)

    if observations < int(min_observations):
        reasons.append(
            f"validation has {observations} observation(s); "
            f"{min_observations} are required"
        )
    if trades < int(min_trades):
        reasons.append(
            f"validation has {trades} trade(s); {min_trades} are required"
        )

    candidate_return = validation_metrics.get("total_return")
    baseline_return = baseline_metrics.get("total_return")
    if not isinstance(candidate_return, (int, float)):
        reasons.append("validation return is unavailable")
        candidate_return = None
    if not isinstance(baseline_return, (int, float)):
        reasons.append("baseline validation return is unavailable")
        baseline_return = None

    train_return = train_metrics.get("total_return")
    gap = (
        float(train_return) - float(candidate_return)
        if isinstance(train_return, (int, float)) and isinstance(candidate_return, (int, float))
        else None
    )
    overfitting = {
        "train_return": train_return,
        "validation_return": candidate_return,
        "gap": None if gap is None else round(gap, 8),
        "flagged": bool(
            gap is not None
            and (
                gap > 0.15
                or (
                    isinstance(train_return, (int, float))
                    and train_return > 0.02
                    and isinstance(candidate_return, (int, float))
                    and candidate_return <= 0
                )
            )
        ),
    }
    if overfitting["flagged"]:
        reasons.append(
            "overfitting: training return exceeds validation by a material margin"
        )

    candidate_dd = validation_metrics.get("max_drawdown")
    baseline_dd = baseline_metrics.get("max_drawdown")
    candidate_sharpe = validation_metrics.get("sharpe")
    baseline_sharpe = baseline_metrics.get("sharpe")
    if (
        isinstance(candidate_return, (int, float))
        and isinstance(baseline_return, (int, float))
        and candidate_return < baseline_return + 0.002
    ):
        reasons.append(
            f"validation return {candidate_return:.4f} does not improve on "
            f"baseline {baseline_return:.4f} by 0.0020"
        )
    if (
        isinstance(candidate_dd, (int, float))
        and isinstance(baseline_dd, (int, float))
        and candidate_dd < baseline_dd - 0.05
        and isinstance(candidate_return, (int, float))
        and isinstance(baseline_return, (int, float))
        and candidate_return - baseline_return < 0.02
    ):
        reasons.append(
            "candidate takes materially more drawdown without a proportional return"
        )
    if (
        isinstance(candidate_sharpe, (int, float))
        and isinstance(baseline_sharpe, (int, float))
        and candidate_sharpe < baseline_sharpe - 0.25
    ):
        reasons.append(
            f"risk-adjusted return worsens Sharpe {baseline_sharpe:.3f} -> "
            f"{candidate_sharpe:.3f}"
        )

    risk = _risk_compliance(candidate_config, policy, instruments)
    if not risk["compliant"]:
        reasons.extend(risk["reasons"])

    try:
        # A five-session test slice is shorter than the strategy's own signal
        # horizon and cannot contain a complete entry/exit cycle.  Ten-session
        # slices make the consistency check measure behaviour rather than the
        # phase of a single crossover.
        walk_forward = engine.walk_forward(
            validation_config, bars, train_bars=20, test_bars=10, max_folds=5
        )
    except Exception as exc:  # noqa: BLE001 - insufficient data is reported
        walk_forward = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "count": 0,
        }
    if walk_forward.get("count"):
        consistency = walk_forward.get("consistency")
        if isinstance(consistency, (int, float)) and consistency < 0.5:
            reasons.append(
                f"walk-forward consistency {consistency:.2f} is below 0.50"
            )
    else:
        reasons.append("walk-forward validation produced no measured folds")

    sensitivity = _sensitivity(
        engine,
        candidate_config,
        validation_config,
        bars,
        bounds_for(catalog_id, baseline_config),
    )
    if sensitivity.get("available") and isinstance(sensitivity.get("return_range"), float):
        if float(sensitivity["return_range"]) > 0.20:
            reasons.append(
                "sensitivity range exceeds 0.20; the result is not stable"
            )

    regimes = regime_analysis(validation.equity)
    if not regimes.get("available"):
        reasons.append("market-regime analysis is unavailable")

    eligible = not reasons
    score = None
    if isinstance(candidate_return, (int, float)):
        score = candidate_return
        if isinstance(candidate_sharpe, (int, float)):
            score += 0.1 * candidate_sharpe
        if isinstance(candidate_dd, (int, float)):
            score -= 0.5 * abs(float(candidate_dd))
    return {
        "ok": True,
        "eligible": eligible,
        "status": "pending_approval" if eligible else "rejected",
        "reasons": reasons,
        "train": train_metrics,
        "validation": validation_metrics,
        "baseline": {
            "train": baseline_train_metrics,
            "validation": baseline_metrics,
        },
        "overfitting": overfitting,
        "walk_forward": walk_forward,
        "sensitivity": sensitivity,
        "regime": regimes,
        "risk": risk,
        "score": None if score is None else round(score, 8),
        "out_of_sample": True,
        "costs_included": {
            "total_fees": validation_metrics.get("total_fees"),
            "total_slippage": validation_metrics.get("total_slippage"),
        },
    }


__all__ = [
    "METHODS",
    "PARAMETER_BOUNDS",
    "bounds_for",
    "evaluate_candidate",
    "generate_candidates",
]
