"""Scenario forecasting for Phase 38.

Forecasts are estimates with explicit horizons, sources, assumptions, method,
confidence and limitations. A forecast is never posted to the ledger and never
counted as recognized revenue or an incurred expense.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from novacontrol.finops.analytics import monthly_actual_expenses, monthly_recognized_revenue

SCENARIO_FACTORS: dict[str, dict[str, float]] = {
    "conservative": {"revenue": 0.85, "expenses": 1.10, "customers": 0.90},
    "base": {"revenue": 1.00, "expenses": 1.00, "customers": 1.00},
    "optimistic": {"revenue": 1.15, "expenses": 0.95, "customers": 1.15},
    "custom": {"revenue": 1.00, "expenses": 1.00, "customers": 1.00},
}


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def average_observed(rows: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
    values = [float(row.get(key, 0) or 0) for row in rows if row.get("observed")]
    if len(values) < 2:
        return None, len(values)
    return sum(values) / len(values), len(values)


def build_forecast(
    venture: Mapping[str, Any],
    *,
    horizon_months: int,
    scenario: str,
    assumptions: Mapping[str, Any] | None,
    as_of: date,
    cash_balance: float,
) -> dict[str, Any]:
    scenario_key = str(scenario or "base").lower()
    if scenario_key not in SCENARIO_FACTORS:
        scenario_key = "base"
    horizon = max(1, min(int(horizon_months), 36))
    settings = dict(assumptions or {})
    revenue_history = monthly_recognized_revenue(venture, months=6, as_of=as_of)
    expense_history = monthly_actual_expenses(venture, months=6, as_of=as_of)
    base_revenue, revenue_months = average_observed(revenue_history, "recognized_revenue")
    base_expense, expense_months = average_observed(expense_history, "actual_expenses")
    factors = SCENARIO_FACTORS[scenario_key]
    revenue_growth = _float(settings.get("monthly_revenue_growth", 0.0), 0.0)
    expense_growth = _float(settings.get("monthly_expense_growth", 0.0), 0.0)
    customer_growth = _float(settings.get("monthly_customer_growth", 0.0), 0.0)
    if scenario_key == "custom":
        factors = {
            "revenue": _float(settings.get("revenue_factor", 1.0), 1.0),
            "expenses": _float(settings.get("expense_factor", 1.0), 1.0),
            "customers": _float(settings.get("customer_factor", 1.0), 1.0),
        }
    missing: list[str] = []
    if base_revenue is None:
        missing.append("at least two observed months of recognized revenue")
    if base_expense is None:
        missing.append("at least two observed months of approved actual expenses")
    rows: list[dict[str, Any]] = []
    cumulative_cash = float(cash_balance)
    runway_month: int | None = None
    if not missing:
        for month in range(1, horizon + 1):
            revenue = (base_revenue or 0.0) * factors["revenue"] * ((1 + revenue_growth) ** (month - 1))
            expenses = (base_expense or 0.0) * factors["expenses"] * ((1 + expense_growth) ** (month - 1))
            net = revenue - expenses
            cumulative_cash += net
            if cumulative_cash < 0 and runway_month is None:
                runway_month = month
            rows.append(
                {
                    "month": month,
                    "forecast_revenue": round(revenue, 2),
                    "forecast_expenses": round(expenses, 2),
                    "forecast_net_cash_flow": round(net, 2),
                    "forecast_cash_balance": round(cumulative_cash, 2),
                    "forecast_customers": None,
                    "kind": "forecast",
                }
            )
    available = bool(rows)
    confidence = "low"
    if revenue_months >= 3 and expense_months >= 3:
        confidence = "medium"
    if revenue_months >= 6 and expense_months >= 6:
        confidence = "medium-high"
    return {
        "available": available,
        "kind": "forecast",
        "scenario": scenario_key,
        "horizon_months": horizon,
        "as_of": as_of.isoformat(),
        "currency": venture.get("reporting_currency"),
        "data_sources": [
            f"last {revenue_months} observed month(s) of recognized revenue",
            f"last {expense_months} observed month(s) of approved actual expenses",
            "operator-supplied assumptions",
        ],
        "assumptions": {
            "monthly_revenue_growth": revenue_growth,
            "monthly_expense_growth": expense_growth,
            "monthly_customer_growth": customer_growth,
            "scenario_factors": factors,
            "starting_cash": round(cash_balance, 2),
        },
        "methodology": (
            "Each month starts from the mean of observed monthly recognized revenue "
            "and approved actual expenses, applies the scenario factor and the stated "
            "monthly growth rate, then adds net cash flow to starting cash."
        ),
        "confidence": confidence,
        "confidence_note": (
            "Confidence reflects only the amount of observed history, not measured "
            "predictive accuracy. No backtest accuracy is claimed."
        ),
        "limitations": [
            "Forecasts are estimates, not actual revenue, actual expenses or commitments.",
            "At least two observed months of both revenue and expenses are required.",
            "Seasonality, one-off events and future contracts are not inferred.",
            "Starting cash is the ledger cash balance at the forecast date." if cash_balance else "No starting cash balance was available.",
        ],
        "missing_inputs": missing,
        "rows": rows,
        "totals": {
            "forecast_revenue": round(sum(row["forecast_revenue"] for row in rows), 2),
            "forecast_expenses": round(sum(row["forecast_expenses"] for row in rows), 2),
            "forecast_net_cash_flow": round(sum(row["forecast_net_cash_flow"] for row in rows), 2),
            "ending_cash": round(cumulative_cash, 2),
            "cash_runway_months": runway_month,
            "exhausts_cash_in_horizon": runway_month is not None,
        },
    }


def compare_scenarios(
    venture: Mapping[str, Any],
    *,
    horizon_months: int,
    assumptions: Mapping[str, Any] | None,
    as_of: date,
    cash_balance: float,
    custom: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [
        build_forecast(
            venture,
            horizon_months=horizon_months,
            scenario=name,
            assumptions={**dict(assumptions or {}), **dict(custom or {})} if name == "custom" else assumptions,
            as_of=as_of,
            cash_balance=cash_balance,
        )
        for name in ("conservative", "base", "optimistic", "custom")
    ]
    return {
        "kind": "scenario_analysis",
        "as_of": as_of.isoformat(),
        "horizon_months": horizon_months,
        "scenarios": rows,
        "note": "All four rows are modelled estimates under their stated assumptions; they are not actuals or promised outcomes.",
    }


__all__ = ["build_forecast", "compare_scenarios"]
