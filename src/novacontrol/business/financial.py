"""Transparent business-economics calculations for Phase 35.

The engine does one thing the phase requires: show the arithmetic and the
missing inputs. It never fills a missing price, demand or cost with a plausible
number. A scenario that lacks a required input is returned with
``calculated=False`` and the missing keys named.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from novacontrol.business.models import (
    FinancialAnalysis,
    FinancialAssumptions,
    FinancialScenario,
    utcnow,
)

METHODOLOGY = (
    "monthly revenue = price × units_per_month",
    "contribution margin = price − variable cost per unit",
    "monthly gross profit = contribution margin × units_per_month",
    "operating expenses = fixed monthly + infrastructure monthly + marketing monthly (a missing optional cost is shown as assumed zero)",
    "monthly net profit = monthly gross profit − operating expenses",
    "break-even units = fixed monthly costs ÷ contribution margin",
    "break-even revenue = break-even units × price",
    "annual forecast revenue = monthly revenue × months_to_forecast",
    "customer lifetime value = contribution margin × customer lifetime months",
    "CAC payback months = customer acquisition cost ÷ contribution margin",
    "cash required = initial investment + development cost + one month of operating expenses",
)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _derive(base: FinancialAssumptions, *, factor: float, price_factor: float, cost_factor: float) -> FinancialAssumptions:
    """Derive a scenario only where the base input exists; never invent a base."""
    def scaled(value: float | None) -> float | None:
        return None if value is None else round(value * factor, 4)

    return FinancialAssumptions(
        price=round(base.price * price_factor, 4) if base.price is not None else None,
        units_per_month=scaled(base.units_per_month),
        variable_cost_per_unit=round(base.variable_cost_per_unit * cost_factor, 4)
        if base.variable_cost_per_unit is not None
        else None,
        fixed_monthly_costs=(
            round(base.fixed_monthly_costs * cost_factor, 4)
            if base.fixed_monthly_costs is not None
            else None
        ),
        infrastructure_monthly=(
            round(base.infrastructure_monthly * cost_factor, 4)
            if base.infrastructure_monthly is not None
            else None
        ),
        marketing_monthly=(
            round(base.marketing_monthly * cost_factor, 4)
            if base.marketing_monthly is not None
            else None
        ),
        customer_acquisition_cost=(
            round(base.customer_acquisition_cost * cost_factor, 4)
            if base.customer_acquisition_cost is not None
            else None
        ),
        customer_lifetime_months=base.customer_lifetime_months,
        initial_investment=base.initial_investment,
        development_cost=base.development_cost,
        months_to_forecast=base.months_to_forecast,
    )


def compute_scenario(name: str, assumptions: FinancialAssumptions) -> FinancialScenario:
    missing = list(assumptions.missing_required)
    if missing:
        return FinancialScenario(
            name=name,
            assumptions=assumptions.to_dict(),
            missing_inputs=tuple(missing),
            methodology=METHODOLOGY,
            calculated=False,
        )
    price = float(assumptions.price or 0.0)
    units = float(assumptions.units_per_month or 0.0)
    variable = float(assumptions.variable_cost_per_unit or 0.0)
    fixed = float(assumptions.fixed_monthly_costs or 0.0)
    infrastructure = float(assumptions.infrastructure_monthly or 0.0)
    marketing = float(assumptions.marketing_monthly or 0.0)
    contribution = price - variable
    revenue = price * units
    gross = contribution * units
    opex = fixed + infrastructure + marketing
    net = gross - opex
    break_even_units = (fixed / contribution) if contribution > 0 else None
    break_even_revenue = break_even_units * price if break_even_units is not None else None
    cash_parts = []
    if assumptions.initial_investment is not None:
        cash_parts.append(assumptions.initial_investment)
    else:
        missing.append("initial_investment")
    if assumptions.development_cost is not None:
        cash_parts.append(assumptions.development_cost)
    else:
        missing.append("development_cost")
    # A cash requirement with one half missing is not a cash requirement; it
    # would understate the figure while looking complete.
    cash_required = sum(cash_parts) + opex if len(cash_parts) == 2 else None
    ltv = (
        contribution * assumptions.customer_lifetime_months
        if assumptions.customer_lifetime_months is not None
        else None
    )
    payback = (
        assumptions.customer_acquisition_cost / contribution
        if assumptions.customer_acquisition_cost is not None and contribution > 0
        else None
    )
    return FinancialScenario(
        name=name,
        assumptions=assumptions.to_dict(),
        monthly_revenue=_round(revenue),
        monthly_gross_profit=_round(gross),
        contribution_margin=_round(contribution),
        contribution_margin_pct=_round((contribution / price) if price else None),
        monthly_operating_expenses=_round(opex),
        monthly_net_profit=_round(net),
        break_even_units=_round(break_even_units),
        break_even_revenue=_round(break_even_revenue),
        cash_required=_round(cash_required),
        customer_lifetime_value=_round(ltv),
        cac_payback_months=_round(payback),
        forecast_revenue=_round(revenue * assumptions.months_to_forecast),
        missing_inputs=tuple(dict.fromkeys(missing)),
        methodology=METHODOLOGY,
        calculated=True,
    )


def analyse_financials(payload: Mapping[str, Any] | None) -> FinancialAnalysis:
    """Build conservative/base/optimistic scenarios from explicit assumptions.

    A caller may supply all three. When only ``base`` is supplied the other two
    are DERIVED from it by documented multipliers and every derived field in
    the scenario's assumptions remains absent if the base value was absent.
    """
    data = dict(payload or {})
    currency = str(data.get("currency", "USD") or "USD").upper()[:8]
    base = FinancialAssumptions.from_mapping(
        data.get("base") if isinstance(data.get("base"), Mapping) else data
    )
    conservative_raw = data.get("conservative")
    optimistic_raw = data.get("optimistic")
    conservative = (
        FinancialAssumptions.from_mapping(conservative_raw)
        if isinstance(conservative_raw, Mapping)
        else _derive(base, factor=0.6, price_factor=0.9, cost_factor=1.15)
    )
    optimistic = (
        FinancialAssumptions.from_mapping(optimistic_raw)
        if isinstance(optimistic_raw, Mapping)
        else _derive(base, factor=1.4, price_factor=1.05, cost_factor=0.9)
    )
    scenarios = (
        compute_scenario("conservative", conservative),
        compute_scenario("base", base),
        compute_scenario("optimistic", optimistic),
    )
    missing = tuple(
        dict.fromkeys(
            item
            for scenario in scenarios
            for item in scenario.missing_inputs
        )
    )
    return FinancialAnalysis(
        currency=currency,
        scenarios=scenarios,
        missing_inputs=missing,
        methodology=METHODOLOGY,
        assumptions_note=(
            "All figures are modelled estimates from supplied assumptions, not actual revenue "
            "or guaranteed profit. A scenario supplied explicitly is used as supplied; otherwise "
            "conservative is derived from base at 0.6× units, 0.9× price and 1.15× costs, and "
            "optimistic at 1.4× units, 1.05× price and 0.9× costs. A missing base input stays "
            "missing; optional infrastructure/marketing costs are shown as assumed zero only in "
            "the calculation, and remain null in the exposed assumptions."
        ),
        computed_at=utcnow(),
    )


def financial_feasibility_score(analysis: FinancialAnalysis | None) -> tuple[float | None, str]:
    """One 0–1 reading from the BASE scenario, or a missing-data explanation."""
    if analysis is None:
        return None, "no financial analysis has been computed"
    base = next((row for row in analysis.scenarios if row.name == "base"), None)
    if base is None or not base.calculated:
        return None, "base financial scenario is missing required assumptions"
    if base.contribution_margin is None or base.contribution_margin <= 0:
        return 0.0, "contribution margin is zero or negative"
    margin_pct = float(base.contribution_margin_pct or 0.0)
    if not math.isfinite(margin_pct):
        return None, "base scenario margin is not a finite number"
    margin_pct = max(0.0, min(1.0, margin_pct))
    profit_signal = 1.0 if (base.monthly_net_profit or 0.0) > 0 else 0.25
    break_even_signal = 1.0 if base.break_even_units is not None else 0.2
    score = (margin_pct * 0.55) + (profit_signal * 0.3) + (break_even_signal * 0.15)
    return max(0.0, min(1.0, score)), "base scenario contribution and break-even"


__all__ = [
    "METHODOLOGY",
    "analyse_financials",
    "compute_scenario",
    "financial_feasibility_score",
]
