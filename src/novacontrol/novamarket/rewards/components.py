"""Independent PI-14 reward components.

Each component evaluates only the decision-time evidence and the simulated
transition it is handed.  Components never write to the environment, the
portfolio, the ledger or an execution store.  They return a raw value; the
engine owns normalisation, weights and the total.
"""

from __future__ import annotations

from .models import ComponentResult, RewardContext, RewardValidationError, TransitionView

_COMPONENT_CATEGORIES = {
    "profitability": "financial",
    "risk": "risk",
    "transaction_cost": "execution",
    "decision_quality": "decision",
    "forecast_utilization": "forecast",
    "discipline": "discipline",
    "portfolio_management": "portfolio",
    "execution_quality": "execution",
}


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sign(value: float) -> float:
    return 1.0 if value > 0 else -1.0 if value < 0 else 0.0


def _uncertainty_factor(label: str) -> float:
    return {"low": 1.0, "medium": 0.65, "high": 0.25, "unknown": 0.35}.get(label, 0.35)


def _forecast_strength(view: TransitionView) -> float:
    expected = view.expected_return
    if expected is None:
        return 0.0
    return min(1.0, abs(expected) / 0.02)


def _forecast_quality(view: TransitionView) -> float:
    if not view.forecast_available:
        return 0.0
    disagreement = view.disagreement
    disagreement_penalty = 0.0 if disagreement is None else min(0.5, max(0.0, disagreement))
    return (
        _uncertainty_factor(view.uncertainty)
        * (1.0 - disagreement_penalty)
        * (1.0 if view.forecast_direction in {"up", "down"} else 0.4)
    )


def _forecast_alignment(view: TransitionView) -> float:
    expected = view.expected_return
    if expected is None:
        return 0.0
    if view.action_type == "buy":
        return _sign(expected)
    if view.action_type == "sell":
        return -_sign(expected)
    return 0.0


class RewardComponent:
    name: str
    category: str

    def evaluate(self, context: RewardContext) -> ComponentResult:
        raise NotImplementedError


class ProfitabilityComponent(RewardComponent):
    name = "profitability"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        gross = view.gross_return
        if gross is None:
            return ComponentResult(
                self.name,
                self.category,
                None,
                available=False,
                reason="previous portfolio equity was not positive",
            )
        # Gross return removes fees and explicit slippage; the transaction-cost
        # component owns those amounts exactly once.
        return ComponentResult(
            self.name,
            self.category,
            gross,
            metrics={
                "gross_return": round(gross, 10),
                "net_return": None if view.net_return is None else round(view.net_return, 10),
                "realized_pnl_delta": round(view.realized_delta, 2),
                "unrealized_pnl_delta": round(view.unrealized_delta, 2),
                "fees_delta": round(view.fees_delta, 2),
                "slippage_cost": round(view.slippage_cost, 2),
                "deployed_capital_return": (
                    round(gross / view.exposure_ratio, 10) if view.exposure_ratio > 0 else None
                ),
                "double_counting_guard": (
                    "profitability is gross of fees and explicit slippage; "
                    "transaction_cost consumes those costs once"
                ),
            },
        )


class RiskComponent(RewardComponent):
    name = "risk"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        if view.after_equity <= 0:
            return ComponentResult(
                self.name, self.category, None, available=False, reason="no positive equity"
            )
        exposure = max(0.0, view.exposure_ratio)
        concentration = max(0.0, view.concentration)
        drawdown = abs(min(0.0, view.drawdown))
        exposure_high = max(0.0001, context.thresholds["risk_exposure_high"])
        concentration_high = max(0.0001, context.thresholds["risk_concentration_high"])
        drawdown_high = max(0.0001, context.thresholds["risk_drawdown_high"])
        volatility_high = max(0.0001, context.thresholds["risk_volatility_high_pct"])
        exposure_excess = max(0.0, exposure - exposure_high) / exposure_high
        concentration_excess = max(0.0, concentration - concentration_high) / concentration_high
        drawdown_excess = drawdown / drawdown_high
        volatility = context.risk_evidence.get("annualized_volatility_pct")
        volatility = None if volatility is None else float(volatility)
        volatility_excess = (
            0.0 if volatility is None else max(0.0, volatility - volatility_high) / volatility_high
        )
        raw = -_clamp(
            0.35 * _clamp(exposure_excess)
            + 0.30 * _clamp(drawdown_excess)
            + 0.20 * _clamp(concentration_excess)
            + 0.15 * _clamp(volatility_excess)
        )
        return ComponentResult(
            self.name,
            self.category,
            raw,
            metrics={
                "exposure_ratio": round(exposure, 8),
                "concentration": round(concentration, 8),
                "drawdown": round(view.drawdown, 8),
                "risk_evidence": dict(context.risk_evidence),
                "authority": "diagnostic only; NM-08 remains the authoritative risk engine",
            },
        )


class TransactionCostComponent(RewardComponent):
    name = "transaction_cost"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        if view.before_equity <= 0:
            return ComponentResult(
                self.name, self.category, None, available=False, reason="no positive equity"
            )
        cost = view.explicit_cost
        raw = -min(1.0, cost / view.before_equity)
        return ComponentResult(
            self.name,
            self.category,
            raw,
            metrics={
                "fees": round(view.fees_delta, 4),
                "slippage": round(view.slippage_cost, 4),
                "total_cost": round(cost, 4),
                "cost_bps": round(cost / view.before_equity * 10_000, 6),
                "fee_components": dict(view.execution.get("fee_components") or {}),
                "cost_basis": "fees plus explicit simulated slippage; each is counted once",
            },
        )


class DecisionQualityComponent(RewardComponent):
    name = "decision_quality"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        action = view.action_type
        if action not in {"buy", "sell", "hold", "wait", "no_action"}:
            return ComponentResult(
                self.name, self.category, -1.0, metrics={"reason": "unknown action"}
            )
        if view.execution_status == "rejected":
            return ComponentResult(
                self.name,
                self.category,
                -1.0,
                metrics={"reason": view.execution.get("reason") or "execution refused"},
            )
        available = view.forecast_available
        strength = _forecast_strength(view)
        direction = _sign(view.expected_return or 0.0)
        confidence = _forecast_quality(view)
        evidence = strength * confidence
        drawdown = abs(min(0.0, view.drawdown))
        drawdown_high = max(0.0001, context.thresholds["risk_drawdown_high"])
        full_exposure = max(0.0001, context.thresholds["decision_full_exposure"])
        weak_forecast = max(0.0001, context.thresholds["decision_weak_forecast"])
        high_risk = _clamp(drawdown / drawdown_high)
        if action == "buy":
            if view.before_position_quantity > 0 and view.before_exposure >= full_exposure:
                raw = -0.6
            elif not available:
                raw = -0.35
            else:
                raw = _clamp(0.75 * evidence * direction + 0.15 * confidence)
        elif action == "sell":
            if view.before_position_quantity <= 0:
                return ComponentResult(
                    self.name,
                    self.category,
                    -1.0,
                    metrics={"reason": "sell requires an existing long position"},
                )
            elif not available:
                # An exit without valid forecast evidence is only mildly
                # reasonable when drawdown itself justifies de-risking.
                raw = _clamp(0.35 * high_risk - 0.15)
            else:
                raw = _clamp(0.70 * evidence * -direction + 0.20 * high_risk)
        elif action == "hold":
            if view.before_position_quantity > 0:
                raw = _clamp(0.45 * evidence * direction - 0.25 * high_risk)
            else:
                raw = 0.15 if not available or strength < weak_forecast else -0.10
        else:  # wait / no_action
            if view.before_position_quantity > 0:
                raw = _clamp(0.20 * confidence - 0.25 * high_risk)
            elif not available or strength < weak_forecast or view.uncertainty == "high":
                raw = 0.40
            else:
                raw = -0.20 * evidence
        return ComponentResult(
            self.name,
            self.category,
            raw,
            metrics={
                "action": action,
                "forecast_available": available,
                "forecast_strength": round(strength, 8),
                "forecast_uncertainty": view.uncertainty,
                "forecast_age_hours": (
                    None if view.forecast_age_hours is None else round(view.forecast_age_hours, 4)
                ),
                "position_side": view.system_position,
                "drawdown": round(view.drawdown, 8),
                "exit_evidence": (
                    "forecast deterioration or drawdown" if action == "sell" else "not applicable"
                ),
                "information_boundary": "decision-time observation only; next outcomes not used",
            },
        )


class ForecastUtilizationComponent(RewardComponent):
    name = "forecast_utilization"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        action = view.action_type
        strength = _forecast_strength(view)
        quality = _forecast_quality(view)
        if not view.forecast_available:
            if action in {"wait", "no_action", "hold"}:
                return ComponentResult(
                    self.name,
                    self.category,
                    0.35,
                    metrics={
                        "abstention": True,
                        "reason": "forecast unavailable or invalid; abstention is reasonable",
                    },
                )
            return ComponentResult(
                self.name,
                self.category,
                -0.35,
                metrics={"abstention": False, "reason": "trade used no valid forecast evidence"},
            )
        alignment = _forecast_alignment(view)
        if alignment:
            raw = _clamp(alignment * strength * quality)
        elif action == "hold" and view.before_position_quantity > 0:
            raw = _clamp(0.30 * strength * quality)
        elif action in {"wait", "no_action"}:
            weak_forecast = max(0.0001, context.thresholds["decision_weak_forecast"])
            raw = _clamp(
                0.20 * (1.0 - strength)
                + 0.15 * (1.0 - quality)
                + (0.10 if strength < weak_forecast else 0.0)
            )
        else:
            raw = 0.0
        return ComponentResult(
            self.name,
            self.category,
            raw,
            metrics={
                "alignment": round(alignment, 6),
                "forecast_strength": round(strength, 8),
                "forecast_quality": round(quality, 8),
                "uncertainty": view.uncertainty,
                "disagreement": None if view.disagreement is None else round(view.disagreement, 8),
                "horizon": view.forecast.get("horizon"),
                "prediction_interval": [
                    view.forecast.get("lower_return"),
                    view.forecast.get("upper_return"),
                ],
                "abstention": action in {"wait", "no_action", "hold"} and not alignment,
                "future_outcome_used": False,
            },
        )


class DisciplineComponent(RewardComponent):
    name = "discipline"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        recent = list(context.history[-20:])
        trades = [item for item in recent if item.is_trade]
        lookback = max(1, int(context.thresholds["discipline_lookback_trades"]))
        same_action = sum(1 for item in trades[-lookback:] if item.action_type == view.action_type)
        reversals = 0
        for previous in reversed(recent):
            if not previous.is_trade:
                continue
            if previous.action_type != view.action_type:
                reversals = 1
            break
        repeated = 0.0
        if view.is_trade:
            repeated = 0.25 * max(0, same_action - 1)
            repeated += 0.35 * reversals
        if view.execution_status == "rejected":
            repeated += 0.5
        raw = -_clamp(repeated)
        return ComponentResult(
            self.name,
            self.category,
            raw,
            metrics={
                "recent_trade_count": len(trades),
                "same_action_repeat": same_action,
                "reversal": bool(reversals),
                "rejected_execution": view.execution_status == "rejected",
                "basis": (
                    "repeated/reversed trades and refused orders; frequency alone is not penalised"
                ),
            },
            penalties={
                "constraint_violation": 1.0 if view.execution_status == "rejected" else 0.0,
                "repeated_trade": 1.0
                if repeated >= context.thresholds["discipline_repeated_penalty"]
                else 0.0,
            },
        )


class PortfolioManagementComponent(RewardComponent):
    name = "portfolio_management"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        if view.after_equity <= 0:
            return ComponentResult(
                self.name, self.category, None, available=False, reason="no positive equity"
            )
        cash_weight = view.cash_weight
        exposure = view.exposure_ratio
        drawdown = abs(min(0.0, view.drawdown))
        concentration = view.concentration
        change = abs(exposure - max(0.0, min(1.0, view.before_exposure)))
        drawdown_high = max(0.0001, context.thresholds["portfolio_drawdown_high"])
        concentration_high = max(0.0001, context.thresholds["portfolio_concentration_high"])
        change_high = max(0.0001, context.thresholds["portfolio_exposure_change_high"])
        # The component penalises concentration, drawdown and unstable exposure.
        # It deliberately gives no positive credit for deploying more capital.
        penalty = (
            0.45 * _clamp(drawdown / drawdown_high)
            + 0.30 * _clamp(max(0.0, concentration - concentration_high) / concentration_high)
            + 0.25 * _clamp(change / change_high)
        )
        return ComponentResult(
            self.name,
            self.category,
            -_clamp(penalty),
            metrics={
                "capital_utilization": round(exposure, 8),
                "cash_weight": None if cash_weight is None else round(cash_weight, 8),
                "concentration": round(concentration, 8),
                "drawdown": round(view.drawdown, 8),
                "exposure_change": round(change, 8),
                "deployment_is_not_intrinsically_rewarded": True,
            },
        )


class ExecutionQualityComponent(RewardComponent):
    name = "execution_quality"
    category = _COMPONENT_CATEGORIES[name]

    def evaluate(self, context: RewardContext) -> ComponentResult:
        view = context.transition
        status = view.execution_status
        if not view.is_trade:
            return ComponentResult(
                self.name,
                self.category,
                0.0,
                metrics={"status": "not_applicable", "fill_ratio": 0.0},
            )
        if status == "rejected":
            return ComponentResult(
                self.name,
                self.category,
                -1.0,
                metrics={"status": status, "reason": view.execution.get("reason") or ""},
            )
        quantity = max(0.0, float(view._dec(view.execution.get("quantity"))))
        fill_ratio = (
            1.0 if status == "filled" and quantity > 0 else 0.5 if status == "partial" else 0.0
        )
        next_volume = view.next_market.get("volume")
        participation = 0.0
        if next_volume is not None:
            try:
                volume = float(next_volume)
                if volume > 0:
                    participation = min(1.0, quantity / volume)
            except (TypeError, ValueError):
                participation = 0.0
        raw = 0.5 * fill_ratio + 0.5 * (1.0 if fill_ratio > 0 else 0.0)
        reference = float(view._dec(view.execution.get("reference_price")))
        price = float(view._dec(view.execution.get("execution_price")))
        slippage_bps = (
            round((price - reference) / reference * 10_000, 6)
            if reference > 0 and price > 0
            else None
        )
        return ComponentResult(
            self.name,
            self.category,
            _clamp(raw),
            metrics={
                "status": status,
                "fill_ratio": fill_ratio,
                "participation": round(participation, 8),
                "slippage_and_fees_owned_by": "transaction_cost",
                "execution_price": price,
                "reference_price": reference,
                "slippage_bps": slippage_bps,
                "future_price_used_to_judge_order": False,
            },
        )


COMPONENT_REGISTRY: dict[str, RewardComponent] = {
    "profitability": ProfitabilityComponent(),
    "risk": RiskComponent(),
    "transaction_cost": TransactionCostComponent(),
    "decision_quality": DecisionQualityComponent(),
    "forecast_utilization": ForecastUtilizationComponent(),
    "discipline": DisciplineComponent(),
    "portfolio_management": PortfolioManagementComponent(),
    "execution_quality": ExecutionQualityComponent(),
}


def validate_component_registry() -> None:
    expected = set(_COMPONENT_CATEGORIES)
    if set(COMPONENT_REGISTRY) != expected:
        raise RewardValidationError("reward component registry is incomplete")
    for name, component in COMPONENT_REGISTRY.items():
        if component.name != name:
            raise RewardValidationError(f"component registry key {name!r} does not match its name")


__all__ = [
    "COMPONENT_REGISTRY",
    "DecisionQualityComponent",
    "DisciplineComponent",
    "ExecutionQualityComponent",
    "ForecastUtilizationComponent",
    "PortfolioManagementComponent",
    "ProfitabilityComponent",
    "RewardComponent",
    "RiskComponent",
    "TransactionCostComponent",
    "validate_component_registry",
]
