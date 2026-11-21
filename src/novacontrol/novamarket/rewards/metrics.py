"""Policy-performance metrics for PI-14.

Financial and trade metrics reuse NM-11's ``calculate_metrics``.  Risk ratios
reuse NM-08's measured metric functions, including their explicit insufficient
sample handling.  This module only coordinates those existing measurements and
does not become a risk authority or a second backtesting engine.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.backtesting.core import Trade, calculate_metrics
from novacontrol.novamarket.risk.metrics import (
    annualized_volatility,
    max_drawdown,
    sharpe,
    sortino,
)

from .models import TransitionView
from .regimes import regime_breakdown


def _d(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _equity_curve(initial: float, transitions: Sequence[TransitionView]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if transitions:
        rows.append(
            {
                "at": transitions[0].timestamp.isoformat(),
                "equity": f"{initial:.2f}",
            }
        )
    for view in transitions:
        rows.append(
            {
                "at": view.next_observation.get("timestamp") or view.timestamp.isoformat(),
                "equity": f"{max(0.0, view.after_equity):.2f}",
            }
        )
    return rows


def _trade(view: TransitionView, backtest_id: str) -> Trade | None:
    if not view.executed or not view.is_trade:
        return None
    quantity = float(view._dec(view.execution.get("quantity")))
    price = _d(view.execution.get("execution_price"))
    reference = _d(view.execution.get("reference_price"))
    fees = max(0.0, _d(view.execution.get("fees")))
    # PI-13 stores total simulated slippage for the fill; NM-11's Trade
    # contract stores per-unit slippage and multiplies by quantity again.
    slippage = max(0.0, view.slippage_cost) / quantity
    if quantity <= 0 or price <= 0:
        return None
    return Trade(
        trade_id=f"tr_{view.transition_id}",
        backtest_id=backtest_id,
        order_id="",
        instrument_id=str(view.observation.get("instrument_id") or ""),
        side=view.action_type,
        order_type="market",
        decision_at=view.timestamp,
        executed_at=view.execution_at,
        quantity=view._dec(view.execution.get("quantity")),
        price=Decimal(str(price)),
        reference_price=Decimal(str(reference)),
        fees=Decimal(str(round(fees, 2))),
        slippage=Decimal(str(round(slippage, 6))),
        slippage_pct=Decimal(
            str(round((price - reference) / reference * 10_000, 6)) if reference else "0"
        ),
        cash_after=Decimal(str(round(view.after_cash, 2))),
        position_after=view.after_position_quantity,
        realized_pnl=Decimal(
            str(round(view.realized_delta if view.action_type == "sell" else 0.0, 2))
        ),
        reason="simulated PI-13 transition",
        partial=view.execution_status == "partial",
    )


def _risk_metrics(equity: Sequence[Mapping[str, Any]], *, minimum: int = 20) -> dict[str, Any]:
    closes = [_d(row.get("equity")) for row in equity]
    values = [value for value in closes if value > 0]
    if len(values) < 2:
        reason = "at least two positive equity observations are required"
        return {
            "available": False,
            "reason": reason,
            "annualized_volatility": None,
            "sharpe": None,
            "sortino": None,
            "max_drawdown": None,
            "calmar": None,
            "downside_deviation": None,
            "observations": len(values),
        }
    returns = [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]
    volatility = annualized_volatility(values, minimum=minimum)
    drawdown = max_drawdown(values, minimum=2)
    sharpe_result = sharpe(returns, minimum=minimum)
    sortino_result = sortino(returns, minimum=minimum)
    downside = [value for value in returns if value < 0]
    downside_deviation = (
        math.sqrt(statistics.fmean(value * value for value in downside)) if downside else None
    )
    annualized = None
    calmar = None
    if len(returns) >= minimum and drawdown.sufficient and drawdown.value is not None:
        total_return = values[-1] / values[0] - 1.0
        try:
            annualized = (1.0 + total_return) ** (252 / len(returns)) - 1.0
        except (OverflowError, ValueError):
            annualized = None
        if annualized is not None and drawdown.value and float(drawdown.value) > 0:
            calmar = annualized / (float(drawdown.value) / 100.0)
    return {
        "available": True,
        "observations": len(values),
        "annualized_volatility": volatility.to_dict(),
        "sharpe": sharpe_result.to_dict(),
        "sortino": sortino_result.to_dict(),
        "max_drawdown": drawdown.to_dict(),
        "calmar": None
        if calmar is None
        else {
            "value": round(calmar, 8),
            "sufficient": len(returns) >= minimum,
            "reason": ""
            if len(returns) >= minimum
            else f"{len(returns)} returns; at least {minimum} are required",
            "assumptions": ["annualized return divided by absolute maximum drawdown"],
        },
        "downside_deviation": None if downside_deviation is None else round(downside_deviation, 10),
        "annualization_periods": 252,
        "annualization_reason": (
            "252 daily periods are used only when the return sample meets the minimum"
        ),
        "risk_free_rate": 0.0,
        "insufficient_sample_count": max(0, minimum - len(returns)),
        "no_annualized_metrics_from_short_sample": len(returns) < minimum,
    }


def _action_distribution(transitions: Sequence[TransitionView]) -> dict[str, int]:
    distribution = {"buy": 0, "sell": 0, "hold": 0, "wait": 0, "no_action": 0}
    for view in transitions:
        action = view.action_type
        if action in distribution:
            distribution[action] += 1
    return distribution


def _reward_summary(rewards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = [_d(reward.get("total")) for reward in rewards]
    if not totals:
        return {
            "available": False,
            "reason": "no rewards were calculated",
            "total_reward": 0.0,
            "average_reward": None,
            "reward_stability": None,
            "distribution": {},
        }
    mean = statistics.fmean(totals)
    deviation = statistics.pstdev(totals) if len(totals) > 1 else 0.0
    ordered = sorted(totals)
    component_totals: dict[str, float] = {}
    penalty_totals: dict[str, float] = {}
    for reward in rewards:
        components = reward.get("components")
        if isinstance(components, Mapping):
            for name, component in components.items():
                if not isinstance(component, Mapping):
                    continue
                contribution = _d(component.get("contribution"))
                component_totals[str(name)] = round(
                    component_totals.get(str(name), 0.0) + contribution, 10
                )
        penalties = reward.get("penalties")
        if isinstance(penalties, Mapping):
            for name, value in penalties.items():
                penalty_totals[str(name)] = round(
                    penalty_totals.get(str(name), 0.0) + _d(value), 10
                )
    return {
        "available": True,
        "samples": len(totals),
        "total_reward": round(sum(totals), 10),
        "average_reward": round(mean, 10),
        "reward_stability": round(deviation, 10),
        "distribution": {
            "minimum": round(ordered[0], 10),
            "median": round(statistics.median(ordered), 10),
            "maximum": round(ordered[-1], 10),
            "p10": round(ordered[max(0, int((len(ordered) - 1) * 0.10))], 10),
            "p90": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.90))], 10),
        },
        "stability_definition": "population standard deviation of per-transition rewards",
        "component_totals": dict(sorted(component_totals.items())),
        "component_averages": {
            name: round(value / len(totals), 10) for name, value in sorted(component_totals.items())
        },
        "penalty_totals": dict(sorted(penalty_totals.items())),
    }


def policy_metrics(
    *,
    initial_capital: Decimal,
    transitions: Sequence[TransitionView],
    rewards: Sequence[Mapping[str, Any]],
    invalid_actions: int = 0,
    periods_per_year: int = 252,
) -> tuple[dict[str, Any], list[Trade]]:
    equity = _equity_curve(float(initial_capital), transitions)
    trades = [trade for view in transitions if (trade := _trade(view, "pi14-policy")) is not None]
    base = calculate_metrics(
        equity,
        trades,
        initial_capital=initial_capital,
        periods_per_year=periods_per_year,
    )
    risk = _risk_metrics(equity)
    if not base.get("available"):
        return (
            {
                "financial": {
                    **base,
                    "annualized_return": None,
                    "sharpe": None,
                    "sortino": None,
                    "calmar": None,
                    "volatility": None,
                },
                "risk": risk,
                "trading": {
                    "available": False,
                    "reason": "fewer than two equity observations",
                },
                "decision": _decision_metrics(transitions, rewards, invalid_actions),
                "learning": _reward_summary(rewards),
                "regimes": regime_breakdown(transitions=transitions, rewards=rewards),
                "validity": {"sufficient": False, "reason": base.get("reason", "")},
                "equity_curve": equity,
            },
            trades,
        )
    # Replace NM-11's short-sample annualized ratios with NM-08's explicit
    # minimum-observation results.  The base metric remains useful for returns,
    # trade statistics and the drawdown series, but it does not invent a Sharpe
    # ratio from two observations.
    base["annualized_return_raw"] = base.get("annualized_return")
    if risk["no_annualized_metrics_from_short_sample"]:
        # Do not expose even a labelled raw annualization from an
        # insufficient sample; a consumer can otherwise quote the number.
        base["annualized_return_raw"] = None
        base["annualized_return"] = None
        base["sharpe"] = None
        base["sortino"] = None
        base["calmar"] = None
        base["volatility"] = None
        base["risk_adjusted_reason"] = (
            f"{risk['insufficient_sample_count']} more return observation(s) required "
            "before annualized risk metrics are reported"
        )
    else:
        base["annualized_return"] = base.get("annualized_return_raw")
        base["volatility"] = (
            None
            if not risk.get("annualized_volatility", {}).get("sufficient")
            else _d(risk["annualized_volatility"].get("value")) / 100.0
        )
        base["sharpe"] = (
            None
            if not risk.get("sharpe", {}).get("sufficient")
            else _d(risk["sharpe"].get("value"))
        )
        base["sortino"] = (
            None
            if not risk.get("sortino", {}).get("sufficient")
            else _d(risk["sortino"].get("value"))
        )
        base["calmar"] = None if risk.get("calmar") is None else risk["calmar"].get("value")
    decision = _decision_metrics(transitions, rewards, invalid_actions)
    trading = {
        "available": True,
        "total_trades": base.get("trades", len(trades)),
        "winning_trades": base.get("wins"),
        "losing_trades": base.get("losses"),
        "win_rate": base.get("win_rate"),
        "average_win": base.get("average_win"),
        "average_loss": base.get("average_loss"),
        "profit_factor": base.get("profit_factor"),
        "average_holding_days": base.get("average_holding_days"),
        "turnover": base.get("turnover"),
        "total_costs": format(
            Decimal(str(base.get("total_fees") or "0"))
            + Decimal(str(base.get("total_slippage") or "0")),
            "f",
        ),
        "total_fees": base.get("total_fees"),
        "total_slippage": base.get("total_slippage"),
        "trade_frequency": base.get("trade_frequency"),
        "cost_basis": "NM-10 fee/slippage assumptions from the simulated PI-13 executions",
    }
    return (
        {
            "financial": {
                "observations": base.get("observations"),
                "initial_capital": base.get("initial_capital"),
                "final_value": base.get("final_value"),
                "total_return": base.get("total_return"),
                "annualized_return": base.get("annualized_return"),
                "annualized_return_raw": base.get("annualized_return_raw"),
                "sharpe": base.get("sharpe"),
                "sortino": base.get("sortino"),
                "calmar": base.get("calmar"),
                "volatility": base.get("volatility"),
                "cumulative_returns": base.get("cumulative_returns"),
                "periodic_returns": base.get("periodic_returns"),
                "realized_pnl": round(sum(view.realized_delta for view in transitions), 2),
                "unrealized_pnl": round(
                    _d(transitions[-1].after.get("unrealized_pnl")) if transitions else 0.0, 2
                ),
                "fees_paid": round(
                    _d(transitions[-1].after.get("fees_paid")) if transitions else 0.0, 2
                ),
            },
            "risk": risk,
            "trading": trading,
            "decision": decision,
            "learning": _reward_summary(rewards),
            "regimes": regime_breakdown(transitions=transitions, rewards=rewards),
            "validity": {
                "sufficient": bool(base.get("available")),
                "observations": len(equity),
                "return_observations": max(0, len(equity) - 1),
                "risk_ratios_require": 20,
                "caveat": "A short historical sample cannot establish generalization.",
            },
            "equity_curve": equity,
        },
        trades,
    )


def _decision_metrics(
    transitions: Sequence[TransitionView],
    rewards: Sequence[Mapping[str, Any]],
    invalid_actions: int,
) -> dict[str, Any]:
    distribution = _action_distribution(transitions)
    total = sum(distribution.values())
    forbidden = sum(1 for view in transitions if view.execution_status == "rejected" or view.error)
    unnecessary = sum(
        1
        for reward in rewards
        if any(
            key == "repeated_trade" and _d(penalty) > 0
            for component in reward.get("components", {}).values()
            if isinstance(component, Mapping)
            if isinstance(component.get("penalties"), Mapping)
            for key, penalty in (component.get("penalties", {}) or {}).items()
        )
    )
    return {
        "action_distribution": distribution,
        "action_frequencies": {
            key: round(value / total, 8) if total else None for key, value in distribution.items()
        },
        "invalid_actions": int(invalid_actions),
        "invalid_action_frequency": round(invalid_actions / (total + invalid_actions), 8)
        if total + invalid_actions
        else None,
        "unnecessary_trading": unnecessary,
        "constraint_violations": forbidden,
        "buy_frequency": round(distribution["buy"] / total, 8) if total else None,
        "sell_frequency": round(distribution["sell"] / total, 8) if total else None,
        "hold_frequency": round(distribution["hold"] / total, 8) if total else None,
        "wait_frequency": round(distribution["wait"] / total, 8) if total else None,
        "no_action_frequency": round(distribution["no_action"] / total, 8) if total else None,
    }


__all__ = ["policy_metrics"]
