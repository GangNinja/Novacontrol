"""Point-in-time statement normalization, ratio analysis and peer comparison."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from statistics import median
from typing import Any

from .models import (
    FinancialAnalysis,
    FinancialStatement,
    NormalizedStatement,
    PeerRecord,
    PeriodType,
    RatioResult,
    StatementBasis,
    clean,
)

UNIT_MULTIPLIERS: dict[str, float] = {
    "absolute": 1.0,
    "unit": 1.0,
    "units": 1.0,
    "inr": 1.0,
    "rupee": 1.0,
    "rupees": 1.0,
    "thousand": 1_000.0,
    "thousands": 1_000.0,
    "k": 1_000.0,
    "lakh": 100_000.0,
    "lakhs": 100_000.0,
    "lac": 100_000.0,
    "crore": 10_000_000.0,
    "crores": 10_000_000.0,
    "cr": 10_000_000.0,
    "million": 1_000_000.0,
    "millions": 1_000_000.0,
    "mn": 1_000_000.0,
    "billion": 1_000_000_000.0,
    "billions": 1_000_000_000.0,
    "bn": 1_000_000_000.0,
}

ALIASES: dict[str, str] = {
    "revenue": "revenue",
    "total_revenue": "revenue",
    "sales": "revenue",
    "net_sales": "revenue",
    "revenue_from_operations": "revenue",
    "cost_of_goods_sold": "cogs",
    "cost_of_revenue": "cogs",
    "cogs": "cogs",
    "gross_profit": "gross_profit",
    "operating_expenses": "operating_expenses",
    "opex": "operating_expenses",
    "operating_profit": "operating_profit",
    "operating_income": "operating_profit",
    "ebit": "operating_profit",
    "ebitda": "ebitda",
    "depreciation": "depreciation",
    "depreciation_amortization": "depreciation",
    "net_income": "net_income",
    "net_profit": "net_income",
    "profit_after_tax": "net_income",
    "pat": "net_income",
    "eps": "eps",
    "earnings_per_share": "eps",
    "diluted_eps": "eps",
    "interest_expense": "interest_expense",
    "finance_cost": "interest_expense",
    "tax_expense": "tax_expense",
    "income_tax": "tax_expense",
    "pretax_income": "pretax_income",
    "profit_before_tax": "pretax_income",
    "pbt": "pretax_income",
    "total_assets": "total_assets",
    "assets": "total_assets",
    "current_assets": "current_assets",
    "inventory": "inventory",
    "inventories": "inventory",
    "cash": "cash",
    "cash_and_equivalents": "cash",
    "cash_and_cash_equivalents": "cash",
    "total_liabilities": "total_liabilities",
    "current_liabilities": "current_liabilities",
    "total_debt": "total_debt",
    "debt": "total_debt",
    "borrowings": "total_debt",
    "shareholders_equity": "equity",
    "total_equity": "equity",
    "equity": "equity",
    "shares_outstanding": "shares_outstanding",
    "shares": "shares_outstanding",
    "operating_cash_flow": "operating_cash_flow",
    "cash_from_operations": "operating_cash_flow",
    "ocf": "operating_cash_flow",
    "investing_cash_flow": "investing_cash_flow",
    "financing_cash_flow": "financing_cash_flow",
    "capital_expenditure": "capex",
    "capex": "capex",
    "dividends_paid": "dividends_paid",
    "free_cash_flow": "free_cash_flow",
    "fcf": "free_cash_flow",
    "dividend_per_share": "dividend_per_share",
    "dps": "dividend_per_share",
    "tax_rate": "tax_rate",
}

_PER_SHARE_ITEMS = {"eps", "dividend_per_share"}

_FLOW_ITEMS = {
    "revenue",
    "cogs",
    "gross_profit",
    "operating_expenses",
    "operating_profit",
    "ebitda",
    "depreciation",
    "net_income",
    "eps",
    "interest_expense",
    "tax_expense",
    "pretax_income",
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "capex",
    "dividends_paid",
    "dividend_per_share",
}


def _canonical_items(values: Mapping[str, Any]) -> tuple[dict[str, float | None], list[str]]:
    result: dict[str, float | None] = {}
    warnings: list[str] = []
    for raw_key, raw_value in values.items():
        key = clean(raw_key, limit=80).lower().replace(" ", "_").replace("-", "_")
        canonical = ALIASES.get(key, key)
        if raw_value is None or raw_value == "":
            result[canonical] = None
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            warnings.append(f"{canonical}: reported value is not numeric and was omitted")
            result[canonical] = None
            continue
        if not math.isfinite(value):
            warnings.append(f"{canonical}: reported value is not finite and was omitted")
            result[canonical] = None
            continue
        if canonical in result and result[canonical] is not None and result[canonical] != value:
            warnings.append(f"{canonical}: duplicate line items disagreed; the first was retained")
            continue
        result[canonical] = value
    return result, warnings


def normalize_statement(
    statement: FinancialStatement,
    *,
    target_currency: str = "INR",
    as_of: datetime,
) -> NormalizedStatement:
    values, warnings = _canonical_items(statement.values)
    unit = statement.unit.lower()
    unit_multiplier = UNIT_MULTIPLIERS.get(unit)
    if unit_multiplier is None:
        warnings.append(f"unknown unit {statement.unit!r}; raw scale was used")
        unit_multiplier = 1.0
    multiplier = statement.scale * unit_multiplier
    target = clean(target_currency, limit=3).upper() or "INR"
    source_currency = statement.currency.upper()
    normalized_currency = source_currency
    fx_multiplier = 1.0
    if source_currency != target:
        if (
            statement.fx_rate is not None
            and statement.fx_as_of is not None
            and statement.fx_as_of <= as_of
            and statement.fx_as_of.date() >= statement.period_end.date()
        ):
            fx_multiplier = statement.fx_rate
            normalized_currency = target
        else:
            warnings.append(
                f"{source_currency} statement was not converted to {target}: "
                "no timestamped FX rate at or after the reporting period"
            )
    normalized = {
        key: (
            None
            if value is None
            else value
            * (fx_multiplier if key in _PER_SHARE_ITEMS else multiplier * fx_multiplier)
        )
        for key, value in values.items()
    }
    if statement.published_at is None:
        warnings.append("statement has no publication timestamp and is not point-in-time safe")
    if statement.shares_outstanding is None and normalized.get("shares_outstanding"):
        normalized["shares_outstanding"] = normalized["shares_outstanding"]
    return NormalizedStatement(
        statement_id=statement.statement_id,
        statement_type=statement.statement_type,
        period_type=statement.period_type,
        basis=statement.basis,
        period_end=statement.period_end,
        published_at=statement.published_at,
        fiscal_year=statement.fiscal_year,
        fiscal_quarter=statement.fiscal_quarter,
        currency=source_currency,
        normalized_currency=normalized_currency,
        multiplier=multiplier * fx_multiplier,
        values=normalized,
        original_values=dict(statement.values),
        shares_outstanding=statement.shares_outstanding
        or (
            None
            if normalized.get("shares_outstanding") is None
            else normalized["shares_outstanding"]
        ),
        restatement_version=statement.restatement_version,
        evidence_id=statement.evidence_id or statement.statement_id,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _period_key(statement: NormalizedStatement) -> tuple[str, str, str, datetime]:
    """The identity of one reporting period.

    ``period_type`` belongs here: a fourth-quarter statement and the annual
    statement that ends on the same date are two different reports, not a
    restatement of each other. Without it the later publication silently
    replaced the annual one and FY figures disappeared from the report.
    """

    return (
        statement.statement_type,
        statement.period_type,
        statement.basis,
        statement.period_end,
    )


def _published(statement: NormalizedStatement) -> datetime:
    return statement.published_at or statement.period_end


def select_point_in_time(
    statements: Sequence[FinancialStatement],
    *,
    as_of: datetime,
    cutoff: datetime,
    max_age_hours: float,
) -> tuple[list[NormalizedStatement], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    normalized: list[NormalizedStatement] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    for statement in statements:
        if statement.published_at is None:
            skipped.append(
                {
                    "statement_id": statement.statement_id,
                    "reason": "missing publication timestamp",
                }
            )
            continue
        if statement.published_at > cutoff:
            skipped.append(
                {
                    "statement_id": statement.statement_id,
                    "reason": "publication is after the research cutoff",
                    "published_at": statement.published_at.isoformat(),
                }
            )
            continue
        if statement.period_end > cutoff:
            skipped.append(
                {
                    "statement_id": statement.statement_id,
                    "reason": "reporting period ends after the research cutoff",
                }
            )
            continue
        item = normalize_statement(statement, as_of=as_of)
        age_hours = max(0.0, (as_of - statement.published_at).total_seconds() / 3600.0)
        if age_hours > max_age_hours:
            warnings.append(
                f"{statement.statement_id}: statement publication is "
                f"{age_hours:.1f}h old (configured limit {max_age_hours:.1f}h)"
            )
            item = replace(
                item,
                warnings=tuple(item.warnings)
                + (f"publication age {age_hours:.1f}h exceeds the evidence limit",),
            )
        normalized.append(item)
    grouped: dict[tuple[str, str, str, datetime], list[NormalizedStatement]] = {}
    for item in normalized:
        grouped.setdefault(_period_key(item), []).append(item)
    selected: list[NormalizedStatement] = []
    restatements: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        rows.sort(
            key=lambda item: (
                item.published_at or item.period_end,
                item.restatement_version,
                item.statement_id,
            )
        )
        chosen = rows[-1]
        selected.append(chosen)
        for previous in rows[:-1]:
            restatements.append(
                {
                    "period_key": [key[0], key[1], key[2], key[3].isoformat()],
                    "superseded_statement_id": previous.statement_id,
                    "selected_statement_id": chosen.statement_id,
                    "previous_version": previous.restatement_version,
                    "selected_version": chosen.restatement_version,
                    "previous_published_at": (
                        previous.published_at.isoformat() if previous.published_at else None
                    ),
                    "selected_published_at": (
                        chosen.published_at.isoformat() if chosen.published_at else None
                    ),
                    "reason": "later publication replaces the earlier record for this period",
                }
            )
    selected.sort(key=lambda item: (item.period_end, item.statement_type, item.basis))
    return selected, skipped, restatements, warnings


def _latest(
    statements: Sequence[NormalizedStatement],
    statement_type: str,
    *,
    period_type: str | None = None,
    basis: str | None = None,
) -> NormalizedStatement | None:
    rows = [
        item
        for item in statements
        if item.statement_type == statement_type
        and (period_type is None or item.period_type == period_type)
        and (basis is None or item.basis == basis)
    ]
    return max(rows, key=lambda item: item.period_end) if rows else None


def _contiguous_quarters(
    statements: Sequence[NormalizedStatement],
    statement_type: str,
    *,
    basis: str,
) -> list[NormalizedStatement]:
    rows = [
        item
        for item in statements
        if item.statement_type == statement_type
        and item.period_type == PeriodType.QUARTERLY.value
        and item.basis == basis
    ]
    rows.sort(key=lambda item: item.period_end)
    if len(rows) < 4:
        return []
    last_four = rows[-4:]
    for index in range(1, 4):
        gap = (last_four[index].period_end - last_four[index - 1].period_end).days
        if gap < 60 or gap > 130:
            return []
    return last_four


def _ttm(
    statements: Sequence[NormalizedStatement],
    *,
    basis: str,
) -> dict[str, Any] | None:
    quarterly_income = _contiguous_quarters(statements, "income", basis=basis)
    quarterly_cash = _contiguous_quarters(statements, "cash_flow", basis=basis)
    if not quarterly_income:
        return None
    currencies = {item.normalized_currency for item in quarterly_income}
    if len(currencies) != 1:
        return None
    income_values: dict[str, float | None] = {}
    for key in _FLOW_ITEMS:
        values = [item.values.get(key) for item in quarterly_income]
        if any(value is None for value in values):
            income_values[key] = None
        else:
            income_values[key] = sum(float(value) for value in values if value is not None)
    cash_values: dict[str, float | None] = {}
    if quarterly_cash:
        for key in _FLOW_ITEMS:
            values = [item.values.get(key) for item in quarterly_cash]
            if all(value is not None for value in values):
                cash_values[key] = sum(float(value) for value in values if value is not None)
    balance = _latest(statements, "balance_sheet", basis=basis)
    return {
        "period": "ttm",
        "basis": basis,
        "currency": next(iter(currencies)),
        "period_end": quarterly_income[-1].period_end.isoformat(),
        "income": income_values,
        "cash_flow": cash_values,
        "balance": dict(balance.values) if balance else {},
        "balance_period_end": balance.period_end.isoformat() if balance else None,
        "quarters": [item.statement_id for item in quarterly_income],
        "warnings": (
            []
            if quarterly_cash
            else ["TTM cash-flow values are unavailable; four contiguous quarters were not found"]
        ),
    }


def _first(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous - 1.0


def _safe_positive_div(numerator: float | None, denominator: float | None) -> float | None:
    """A ratio whose denominator is zero or negative is economically undefined."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _values_for(
    statements: Sequence[NormalizedStatement],
    statement_type: str,
    *,
    basis: str,
) -> dict[str, float | None]:
    item = _latest(statements, statement_type, basis=basis)
    return dict(item.values) if item else {}


def compute_ratios(
    *,
    statements: Sequence[NormalizedStatement],
    basis: str,
    price: float | None,
    price_currency: str,
    shares_outstanding: float | None,
    prior_balance: Mapping[str, float | None] | None = None,
    period_label: str = "",
) -> RatioResult:
    warnings: list[str] = []
    currencies = {item.normalized_currency for item in statements}
    if len(currencies) > 1:
        latest_item = max(statements, key=lambda item: item.period_end)
        chosen_currency = latest_item.normalized_currency
        statements = [item for item in statements if item.normalized_currency == chosen_currency]
        warnings.append(
            f"statements in {', '.join(sorted(currencies - {chosen_currency}))} were "
            f"excluded; the analysis is in {chosen_currency}"
        )
    income = _values_for(statements, "income", basis=basis)
    balance = _values_for(statements, "balance_sheet", basis=basis)
    cash_flow = _values_for(statements, "cash_flow", basis=basis)
    ttm = _ttm(statements, basis=basis)
    if ttm:
        income = {**income, **{k: v for k, v in ttm["income"].items() if v is not None}}
        cash_flow = {**cash_flow, **{k: v for k, v in ttm["cash_flow"].items() if v is not None}}
        balance = {**balance, **{k: v for k, v in ttm["balance"].items() if v is not None}}
        period_label = period_label or "ttm"
    values: dict[str, float | None] = {}
    statuses: dict[str, str] = {}
    formulas: dict[str, str] = {}
    assumptions: list[str] = []
    inputs: dict[str, Any] = {
        "basis": basis,
        "price": price,
        "price_currency": price_currency,
        "shares_outstanding": shares_outstanding,
    }

    def put(name: str, value: float | None, formula: str, *, missing: str = "") -> None:
        formulas[name] = formula
        values[name] = value
        statuses[name] = "value" if value is not None and math.isfinite(value) else "unavailable"
        if value is None:
            warnings.append(
                missing
                or f"{name.replace('_', ' ')} is unavailable because its inputs are incomplete"
            )

    revenue = income.get("revenue")
    cogs = income.get("cogs")
    gross = _first(
        income.get("gross_profit"),
        None if revenue is None or cogs is None else revenue - cogs,
    )
    operating = income.get("operating_profit")
    net = income.get("net_income")
    put("gross_margin", _safe_div(gross, revenue), "gross_profit / revenue")
    put("operating_margin", _safe_div(operating, revenue), "operating_profit / revenue")
    put("net_profit_margin", _safe_div(net, revenue), "net_income / revenue")
    equity = balance.get("equity")
    assets = balance.get("total_assets")
    prior_equity = (prior_balance or {}).get("equity")
    prior_assets = (prior_balance or {}).get("total_assets")
    average_equity = (
        (equity + prior_equity) / 2.0 if equity is not None and prior_equity is not None else equity
    )
    average_assets = (
        (assets + prior_assets) / 2.0 if assets is not None and prior_assets is not None else assets
    )
    if prior_equity is None or prior_assets is None:
        assumptions.append("ROE/ROA use ending equity/assets when a prior balance is unavailable")
    put("return_on_equity", _safe_div(net, average_equity), "net_income / average_equity")
    put("return_on_assets", _safe_div(net, average_assets), "net_income / average_assets")
    debt = balance.get("total_debt")
    cash = balance.get("cash")
    net_debt = None if debt is None or cash is None else debt - cash
    invested_capital = (
        None if debt is None or equity is None or cash is None else debt + equity - cash
    )
    tax_rate = income.get("tax_rate")
    if tax_rate is None:
        pretax = income.get("pretax_income")
        tax = income.get("tax_expense")
        if pretax is not None and tax is not None and pretax > 0:
            tax_rate = tax / pretax
    nopat = (
        None
        if operating is None or tax_rate is None
        else operating * (1.0 - max(0.0, min(1.0, tax_rate)))
    )
    if tax_rate is None:
        assumptions.append("ROIC is unavailable without an effective tax rate")
    put(
        "return_on_invested_capital", _safe_div(nopat, invested_capital), "nopat / invested_capital"
    )
    current_assets = balance.get("current_assets")
    current_liabilities = balance.get("current_liabilities")
    inventory = balance.get("inventory")
    put(
        "current_ratio",
        _safe_div(current_assets, current_liabilities),
        "current_assets / current_liabilities",
    )
    put(
        "quick_ratio",
        _safe_div(
            None if current_assets is None or inventory is None else current_assets - inventory,
            current_liabilities,
        ),
        "(current_assets - inventory) / current_liabilities",
    )
    put("cash_ratio", _safe_div(cash, current_liabilities), "cash / current_liabilities")
    put("debt_to_equity", _safe_div(debt, equity), "total_debt / equity")
    put("debt_to_assets", _safe_div(debt, assets), "total_debt / total_assets")
    put(
        "interest_coverage",
        _safe_div(operating, income.get("interest_expense")),
        "operating_profit / interest_expense",
    )
    put("net_debt", net_debt, "total_debt - cash")
    market_cap = None if price is None or shares_outstanding is None else price * shares_outstanding
    inputs["market_cap"] = market_cap
    inputs["working_capital"] = (
        None
        if current_assets is None or current_liabilities is None
        else current_assets - current_liabilities
    )
    eps = income.get("eps")
    if eps is None and net is not None and shares_outstanding:
        eps = net / shares_outstanding
    pe_currency_ok = (price_currency or "INR").upper() == (
        ttm["currency"] if ttm else price_currency or "INR"
    ).upper()
    put(
        "pe",
        _safe_positive_div(price, eps) if pe_currency_ok else None,
        "price / eps (undefined for non-positive EPS)",
    )
    book_per_share = _safe_div(equity, shares_outstanding)
    put(
        "pb",
        _safe_positive_div(price, book_per_share) if pe_currency_ok else None,
        "price / (equity / shares) (undefined for non-positive book value)",
    )
    put(
        "ps",
        _safe_positive_div(market_cap, revenue) if pe_currency_ok else None,
        "market_cap / revenue (undefined for non-positive revenue)",
    )
    ebitda = income.get("ebitda")
    enterprise_value = (
        None if market_cap is None or debt is None or cash is None else market_cap + debt - cash
    )
    put(
        "ev_ebitda",
        _safe_positive_div(enterprise_value, ebitda) if pe_currency_ok else None,
        "enterprise_value / ebitda (undefined for non-positive EBITDA)",
    )
    dps = income.get("dividend_per_share")
    put(
        "dividend_yield",
        _safe_positive_div(dps, price) if pe_currency_ok else None,
        "dividend_per_share / price (undefined for non-positive price)",
    )
    operating_cash = cash_flow.get("operating_cash_flow")
    capex = cash_flow.get("capex")
    calculated_fcf = (
        operating_cash - abs(capex) if operating_cash is not None and capex is not None else None
    )
    fcf = _first(calculated_fcf, cash_flow.get("free_cash_flow"))
    put("free_cash_flow", fcf, "operating_cash_flow - capex")
    put(
        "free_cash_flow_yield",
        _safe_positive_div(fcf, market_cap) if pe_currency_ok else None,
        "free_cash_flow / market_cap (undefined for non-positive market cap)",
    )
    put(
        "cash_conversion",
        _safe_positive_div(cash_flow.get("operating_cash_flow"), net),
        "operating_cash_flow / net_income (undefined for non-positive net income)",
    )
    annual = [
        item
        for item in statements
        if item.statement_type == "income"
        and item.period_type == PeriodType.ANNUAL.value
        and item.basis == basis
    ]
    annual.sort(key=lambda item: item.period_end)
    latest = annual[-1].values if annual else {}
    prior = annual[-2].values if len(annual) >= 2 else {}
    annual_cash = [
        item
        for item in statements
        if item.statement_type == "cash_flow"
        and item.period_type == PeriodType.ANNUAL.value
        and item.basis == basis
    ]
    annual_cash.sort(key=lambda item: item.period_end)
    latest_cash = annual_cash[-1].values if annual_cash else {}
    prior_cash = annual_cash[-2].values if len(annual_cash) >= 2 else {}
    put(
        "revenue_growth",
        _growth(latest.get("revenue"), prior.get("revenue")),
        "revenue_t / revenue_t-1 - 1",
    )
    put("eps_growth", _growth(latest.get("eps"), prior.get("eps")), "eps_t / eps_t-1 - 1")
    put(
        "earnings_growth",
        _growth(latest.get("net_income"), prior.get("net_income")),
        "net_income_t / net_income_t-1 - 1",
    )
    put(
        "cash_flow_growth",
        _growth(latest_cash.get("operating_cash_flow"), prior_cash.get("operating_cash_flow")),
        "operating_cash_flow_t / operating_cash_flow_t-1 - 1",
    )
    if not annual:
        warnings.append("no annual income statements are available for growth calculations")
    return RatioResult(
        values=values,
        statuses=statuses,
        formulas=formulas,
        inputs=inputs,
        warnings=tuple(dict.fromkeys(warnings)),
        assumptions=tuple(assumptions),
        period=period_label or "latest_available",
    )


def analyze_financials(
    statements: Sequence[FinancialStatement],
    *,
    as_of: datetime,
    cutoff: datetime,
    max_age_hours: float,
    price: float | None,
    price_currency: str,
    shares_outstanding: float | None,
) -> FinancialAnalysis:
    selected, skipped, restatements, warnings = select_point_in_time(
        statements,
        as_of=as_of,
        cutoff=cutoff,
        max_age_hours=max_age_hours,
    )
    bases = sorted({item.basis for item in selected})
    currencies = sorted({item.normalized_currency for item in selected})
    if len(bases) > 1:
        warnings.append(
            "consolidated and standalone statements are both present; the analysis uses "
            "one basis at a time and does not mix them"
        )
    if len(currencies) > 1:
        warnings.append(
            "mixed statement currencies are present; ratios are not compared across them"
        )
    basis = (
        StatementBasis.CONSOLIDATED.value
        if StatementBasis.CONSOLIDATED.value in bases
        else (bases[0] if bases else "")
    )
    if not basis:
        basis = StatementBasis.CONSOLIDATED.value
    if selected:
        chosen_currency = max(selected, key=lambda item: item.period_end).normalized_currency
        selected = [item for item in selected if item.normalized_currency == chosen_currency]
    balance = _latest(selected, "balance_sheet", basis=basis)
    prior_balances = [
        item for item in selected if item.statement_type == "balance_sheet" and item.basis == basis
    ]
    prior_balances.sort(key=lambda item: item.period_end)
    prior_balance = dict(prior_balances[-2].values) if len(prior_balances) >= 2 else None
    ttm = _ttm(selected, basis=basis)
    ratios = compute_ratios(
        statements=selected,
        basis=basis,
        price=price,
        price_currency=price_currency,
        shares_outstanding=shares_outstanding or (balance.shares_outstanding if balance else None),
        prior_balance=prior_balance,
        period_label="ttm" if ttm else "annual_or_latest",
    )
    annual_income = [
        item
        for item in selected
        if item.statement_type == "income"
        and item.period_type == PeriodType.ANNUAL.value
        and item.basis == basis
    ]
    annual_income.sort(key=lambda item: item.period_end)
    quarterly_income = [
        item
        for item in selected
        if item.statement_type == "income"
        and item.period_type == PeriodType.QUARTERLY.value
        and item.basis == basis
    ]
    quarterly_income.sort(key=lambda item: item.period_end)

    def comparison(rows: Sequence[NormalizedStatement]) -> dict[str, Any]:
        if len(rows) < 2:
            return {"available": False, "rows": [item.to_dict() for item in rows]}
        current, previous = rows[-1], rows[-2]
        return {
            "available": True,
            "current_period_end": current.period_end.isoformat(),
            "previous_period_end": previous.period_end.isoformat(),
            "revenue_change": _growth(
                current.values.get("revenue"), previous.values.get("revenue")
            ),
            "net_income_change": _growth(
                current.values.get("net_income"), previous.values.get("net_income")
            ),
            "eps_change": _growth(current.values.get("eps"), previous.values.get("eps")),
            "operating_cash_flow_change": _growth(
                current.values.get("operating_cash_flow"),
                previous.values.get("operating_cash_flow"),
            ),
            "rows": [item.to_dict() for item in rows],
        }

    comparisons = {
        "annual": comparison(annual_income),
        "quarterly": comparison(quarterly_income),
        "method": "same statement type, basis, period type and currency only",
    }
    missing = []
    for key in ("revenue", "net_income", "total_assets", "equity"):
        if not any(item.values.get(key) is not None for item in selected):
            missing.append(key)
    if not selected:
        warnings.append("no point-in-time statement is usable")
    return FinancialAnalysis(
        statements=tuple(item.to_dict() for item in selected),
        normalized=tuple(item.to_dict() for item in selected),
        ttm=ttm,
        comparisons=comparisons,
        ratios=ratios,
        restatements=tuple(dict(item) for item in restatements),
        missing=tuple(missing),
        warnings=tuple(dict.fromkeys(warnings + skipped_reasons(skipped))),
    )


def skipped_reasons(skipped: Sequence[Mapping[str, Any]]) -> list[str]:
    return [f"{item.get('statement_id')}: {item.get('reason')}" for item in skipped]


def peer_comparison(
    *,
    target: Mapping[str, float | None],
    industry: str,
    currency: str,
    peers: Sequence[PeerRecord],
    as_of: datetime,
    cutoff: datetime,
    allow_cross_industry: bool = False,
    market_cap: float | None = None,
    market_cap_band: float = 0.5,
) -> dict[str, Any]:
    chosen: list[PeerRecord] = []
    excluded: list[dict[str, Any]] = []
    target_industry = clean(industry, limit=120).lower()
    for peer in peers:
        reasons: list[str] = []
        if peer.published_at is None:
            reasons.append("missing publication timestamp")
        elif peer.published_at > cutoff:
            reasons.append("published after the research cutoff")
        if peer.currency.upper() != currency.upper():
            reasons.append(f"currency {peer.currency} differs from target {currency}")
        if not allow_cross_industry and target_industry and peer.industry != target_industry:
            reasons.append("industry differs from the target; peer is not comparable")
        if market_cap is not None and peer.market_cap is not None and market_cap > 0:
            ratio = peer.market_cap / market_cap
            if ratio < 1.0 - market_cap_band or ratio > 1.0 + market_cap_band:
                reasons.append("market-cap grouping is outside the configured band")
        if reasons:
            excluded.append({"instrument_id": peer.instrument_id, "reasons": reasons})
        else:
            chosen.append(peer)
    metric_names = sorted(
        {key for peer in chosen for key in peer.metrics if peer.metrics[key] is not None}
    )
    rows: list[dict[str, Any]] = []
    medians: dict[str, float | None] = {}
    ranges: dict[str, dict[str, float | None]] = {}
    for metric in metric_names:
        values: list[float] = []
        for peer in chosen:
            metric_value = peer.metrics.get(metric)
            if metric_value is not None:
                values.append(float(metric_value))
        medians[metric] = median(values) if values else None
        ranges[metric] = {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    for peer in chosen:
        row: dict[str, Any] = {
            "instrument_id": peer.instrument_id,
            "name": peer.name,
            "metrics": {},
        }
        for metric in metric_names:
            peer_value = peer.metrics.get(metric)
            target_value = target.get(metric)
            row["metrics"][metric] = {
                "peer": peer_value,
                "target": target_value,
                "difference": (
                    None
                    if peer_value is None or target_value is None
                    else float(peer_value) - float(target_value)
                ),
                "missing": peer_value is None or target_value is None,
            }
        rows.append(row)
    return {
        "criteria": {
            "industry_match": not allow_cross_industry,
            "currency_match": True,
            "market_cap_band": market_cap_band if market_cap is not None else None,
            "period": "each peer's published period; no silent period mixing",
        },
        "target": dict(target),
        "target_industry": target_industry,
        "peers": rows,
        "medians": medians,
        "ranges": ranges,
        "selected_count": len(chosen),
        "excluded": excluded,
        "warnings": []
        if chosen
        else ["no point-in-time peer passed the documented selection criteria"],
    }
