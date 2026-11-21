"""Transparent relative and discounted-cash-flow research valuation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import (
    ValuationAssumptions,
    ValuationMethod,
    ValuationResult,
)

_MULTIPLE_TO_BASE = {
    "pe": "eps",
    "pb": "book_value_per_share",
    "ps": "revenue_per_share",
    "ev_ebitda": "ebitda",
    "ev_sales": "revenue",
    "fcf_yield": "free_cash_flow",
}


def _not_available(method: str, reason: str, assumptions: ValuationAssumptions) -> ValuationResult:
    return ValuationResult(
        method=method,
        status="insufficient_inputs",
        currency=assumptions.currency,
        assumptions=assumptions.to_dict(),
        warnings=(reason,),
        limitations=(
            "No valuation was estimated because the required reported facts or "
            "explicit assumptions are missing.",
            "A missing input was not replaced with a fabricated financial value.",
        ),
    )


def relative_valuation(
    assumptions: ValuationAssumptions,
    *,
    peer_medians: Mapping[str, float | None] | None = None,
    peer_min: Mapping[str, float | None] | None = None,
    peer_max: Mapping[str, float | None] | None = None,
) -> ValuationResult:
    method = ValuationMethod.RELATIVE.value
    if assumptions.method != method:
        return _not_available(method, "assumptions use a different valuation method", assumptions)
    base_name = _MULTIPLE_TO_BASE.get(assumptions.peer_metric, "")
    base = assumptions.base_metric
    if base is None and assumptions.peer_metric == "pe":
        base = assumptions.eps
    elif base is None and assumptions.peer_metric == "pb":
        base = assumptions.book_value
    elif base is None and assumptions.peer_metric == "ps":
        base = assumptions.revenue
    elif base is None and assumptions.peer_metric == "ev_ebitda":
        base = assumptions.ebitda
    multiple = assumptions.peer_multiple
    if multiple is None and peer_medians:
        multiple = peer_medians.get(assumptions.peer_metric)
    if base is None:
        return _not_available(
            method, f"relative valuation needs a target {base_name or 'base metric'}", assumptions
        )
    if multiple is None or multiple <= 0:
        return _not_available(
            method,
            f"relative valuation needs a positive peer {assumptions.peer_metric} multiple",
            assumptions,
        )
    estimate = float(base) * float(multiple)
    low = estimate
    high = estimate
    if peer_min:
        low_multiple = peer_min.get(assumptions.peer_metric)
        if low_multiple is not None and low_multiple > 0:
            low = min(low, float(base) * float(low_multiple))
    if peer_max:
        high_multiple = peer_max.get(assumptions.peer_metric)
        if high_multiple is not None and high_multiple > 0:
            high = max(high, float(base) * float(high_multiple))
    low, high = min(low, high), max(low, high)
    enterprise_metric = assumptions.peer_metric.startswith("ev_")
    equity_metric = assumptions.peer_metric == "fcf_yield"
    estimate_basis = (
        "per_share"
        if not enterprise_metric and not equity_metric
        else ("enterprise_value" if enterprise_metric else "equity_value")
    )
    if (enterprise_metric or equity_metric) and assumptions.shares_outstanding:
        net_debt = assumptions.net_debt if enterprise_metric else 0.0
        estimate = (estimate - net_debt) / assumptions.shares_outstanding
        low = (low - net_debt) / assumptions.shares_outstanding
        high = (high - net_debt) / assumptions.shares_outstanding
        estimate_basis = "per_share_after_net_debt"
    return ValuationResult(
        method=method,
        status="completed",
        currency=assumptions.currency,
        estimate=estimate,
        low=low,
        high=high,
        assumptions=assumptions.to_dict(),
        inputs={
            "base_metric": base_name,
            "base_value": float(base),
            "peer_metric": assumptions.peer_metric,
            "peer_multiple": float(multiple),
            "peer_min": peer_min.get(assumptions.peer_metric) if peer_min else None,
            "peer_max": peer_max.get(assumptions.peer_metric) if peer_max else None,
            "estimate_basis": estimate_basis,
        },
        warnings=(
            "Relative valuation assumes the selected peer multiple is appropriate "
            "for this business; it is not a verified intrinsic value.",
        ),
        limitations=(
            "Peers may differ in growth, capital intensity, accounting policy and risk.",
            "The range reflects the peer multiple range only, not every uncertainty.",
            "This is a derived research estimate, not an objectively verified market price.",
        ),
    )


def dcf_valuation(assumptions: ValuationAssumptions) -> ValuationResult:
    method = ValuationMethod.DCF.value
    if assumptions.method != method:
        return _not_available(method, "assumptions use a different valuation method", assumptions)
    if assumptions.revenue is None or assumptions.revenue <= 0:
        return _not_available(method, "DCF needs a positive starting revenue", assumptions)
    if assumptions.shares_outstanding is None or assumptions.shares_outstanding <= 0:
        return _not_available(method, "DCF needs positive shares outstanding", assumptions)
    growth = list(assumptions.revenue_growth)
    if not growth:
        return _not_available(method, "DCF needs explicit revenue-growth assumptions", assumptions)
    if len(growth) < assumptions.forecast_years:
        growth.extend([growth[-1]] * (assumptions.forecast_years - len(growth)))
    growth = growth[: assumptions.forecast_years]
    if any(rate <= -1.0 for rate in growth):
        return _not_available(method, "DCF revenue growth must be above -100%", assumptions)
    if assumptions.discount_rate <= assumptions.terminal_growth:
        return _not_available(method, "DCF discount rate must exceed terminal growth", assumptions)
    revenue = float(assumptions.revenue)
    rows: list[dict[str, Any]] = []
    present_value = 0.0
    last_fcff = 0.0
    for year, rate in enumerate(growth, start=1):
        revenue *= 1.0 + rate
        ebit = revenue * assumptions.operating_margin
        nopat = ebit * (1.0 - assumptions.tax_rate)
        capex = revenue * assumptions.capex_pct_revenue
        working_capital = revenue * assumptions.working_capital_pct_revenue
        # NOPAT less capital expenditure and incremental working capital.  This
        # deliberately does not add back depreciation because the caller did
        # not supply a separate depreciation schedule.
        fcff = nopat - capex - working_capital
        discount = (1.0 + assumptions.discount_rate) ** year
        value = fcff / discount
        present_value += value
        last_fcff = fcff
        rows.append(
            {
                "year": year,
                "revenue": revenue,
                "ebit": ebit,
                "nopat": nopat,
                "capex": capex,
                "working_capital_investment": working_capital,
                "fcff": fcff,
                "present_value": value,
            }
        )
    terminal_value = (
        last_fcff
        * (1.0 + assumptions.terminal_growth)
        / (assumptions.discount_rate - assumptions.terminal_growth)
    )
    terminal_present = terminal_value / (
        (1.0 + assumptions.discount_rate) ** assumptions.forecast_years
    )
    enterprise_value = present_value + terminal_present
    equity_value = enterprise_value - assumptions.net_debt
    per_share = equity_value / assumptions.shares_outstanding
    sensitivity: list[dict[str, Any]] = []
    for discount_delta in (-0.02, 0.0, 0.02):
        for growth_delta in (-0.01, 0.0, 0.01):
            discount = assumptions.discount_rate + discount_delta
            terminal = assumptions.terminal_growth + growth_delta
            sensitivity_value: float | None
            if discount <= terminal:
                sensitivity_value = None
            else:
                adjusted_terminal = (last_fcff * (1.0 + terminal) / (discount - terminal)) / (
                    (1.0 + discount) ** assumptions.forecast_years
                )
                adjusted_present = sum(
                    row["fcff"] / ((1.0 + discount) ** int(row["year"])) for row in rows
                )
                sensitivity_value = (
                    adjusted_present + adjusted_terminal - assumptions.net_debt
                ) / assumptions.shares_outstanding
            sensitivity.append(
                {
                    "discount_rate": discount,
                    "terminal_growth": terminal,
                    "value_per_share": sensitivity_value,
                }
            )
    finite = [
        item["value_per_share"] for item in sensitivity if item["value_per_share"] is not None
    ]
    return ValuationResult(
        method=method,
        status="completed",
        currency=assumptions.currency,
        estimate=per_share,
        low=min(finite) if finite else per_share,
        high=max(finite) if finite else per_share,
        assumptions=assumptions.to_dict(),
        inputs={
            "starting_revenue": assumptions.revenue,
            "shares_outstanding": assumptions.shares_outstanding,
            "net_debt": assumptions.net_debt,
            "enterprise_value": enterprise_value,
            "equity_value": equity_value,
            "projection": rows,
            "terminal_value": terminal_value,
            "terminal_present_value": terminal_present,
        },
        sensitivity=tuple(sensitivity),
        warnings=(
            "DCF output is an assumption-driven estimate; small changes in discount "
            "rate or terminal growth materially change it.",
        ),
        limitations=(
            "The projection uses explicit caller assumptions and does not forecast "
            "revenue or margins itself.",
            "The model does not establish whether the assumptions are achievable.",
            "The per-share output is not an objectively verified intrinsic value or "
            "a price target.",
        ),
    )
