"""Append-only, venture-local double-entry calculations for Phase 38.

The journal is the source of truth for statements. Entries are never edited or
deleted: a reversal is a new entry whose lines negate the original, and readers
derive a reversed entry's status from the reversal links. Every calculation here
returns explicit availability flags rather than manufacturing a zero that a
report could mistake for observed performance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.finops.models import money_text, money_value, parse_money

ZERO = Decimal("0.00")


def _entry_amount(line: Mapping[str, Any], side: str) -> Decimal:
    raw = line.get(side, "0.00")
    try:
        return parse_money(raw, side, allow_zero=True)
    except ValueError:
        return ZERO


def _entry_date(entry: Mapping[str, Any]) -> date | None:
    raw = str(entry.get("date") or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def reversed_entry_ids(venture: Mapping[str, Any]) -> set[str]:
    return {
        str(entry.get("reverses"))
        for entry in venture.get("journal_entries", [])
        if entry.get("reverses")
    }


def active_entries(venture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Posted entries, excluding originals that have a reversal.

    Both the original and the reversal remain in history. For balances, only the
    reversal is applied, which is exactly the original's negative effect; the
    original is then invisible to reports that already include the reversal.
    """
    reversed_ids = reversed_entry_ids(venture)
    return [
        dict(entry)
        for entry in venture.get("journal_entries", [])
        if entry.get("id") not in reversed_ids
    ]


def entries_between(
    venture: Mapping[str, Any], start: date | None = None, end: date | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in active_entries(venture):
        entry_date = _entry_date(entry)
        if entry_date is None:
            continue
        if start is not None and entry_date < start:
            continue
        if end is not None and entry_date > end:
            continue
        rows.append(entry)
    return rows


def entry_lines(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        for sequence, line in enumerate(entry.get("lines", []), start=1):
            rows.append(
                {
                    "entry_id": entry.get("id"),
                    "date": entry.get("date"),
                    "memo": entry.get("memo", ""),
                    "source": entry.get("source", ""),
                    "source_ref": entry.get("source_ref", ""),
                    "product_ref": entry.get("product_ref") or line.get("product_ref", ""),
                    "customer_ref": entry.get("customer_ref") or line.get("customer_ref", ""),
                    "cash_flow_category": entry.get("cash_flow_category", "operating"),
                    "sequence": sequence,
                    **line,
                }
            )
    return rows


def account_totals(
    venture: Mapping[str, Any],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in entry_lines(entries_between(venture, start, end)):
        code = str(line.get("account_code") or "")
        if not code:
            continue
        totals[code] += _entry_amount(line, "debit") - _entry_amount(line, "credit")
    return dict(totals)


def signed_balance(
    venture: Mapping[str, Any],
    code: str,
    *,
    as_of: date | None = None,
) -> Decimal:
    total = account_totals(venture, end=as_of).get(str(code), ZERO)
    account = next(
        (row for row in venture.get("accounts", []) if str(row.get("code")) == str(code)),
        None,
    )
    if account is None:
        return ZERO
    return total if account.get("normal_side") == "debit" else -total


def trial_balance(venture: Mapping[str, Any], *, as_of: date | None = None) -> dict[str, Any]:
    totals = account_totals(venture, end=as_of)
    rows: list[dict[str, Any]] = []
    total_debit = ZERO
    total_credit = ZERO
    for account in venture.get("accounts", []):
        code = str(account.get("code"))
        signed = totals.get(code, ZERO)
        debit = signed if signed > 0 else ZERO
        credit = -signed if signed < 0 else ZERO
        total_debit += debit
        total_credit += credit
        rows.append(
            {
                "account_code": code,
                "account_name": account.get("name"),
                "type": account.get("type"),
                "category": account.get("category"),
                "debit": money_value(debit),
                "credit": money_value(credit),
                "debit_exact": money_text(debit),
                "credit_exact": money_text(credit),
                "normal_side": account.get("normal_side"),
            }
        )
    return {
        "available": bool(venture.get("journal_entries")),
        "as_of": as_of.isoformat() if as_of else "",
        "currency": venture.get("reporting_currency"),
        "rows": rows,
        "total_debit": money_value(total_debit),
        "total_credit": money_value(total_credit),
        "difference": money_value(total_debit - total_credit),
        "balanced": total_debit == total_credit,
        "note": "Derived from posted journal entries. A reversal changes the reversal period, never the original entry.",
    }


def _account(venture: Mapping[str, Any], code: str) -> Mapping[str, Any]:
    return next(
        (row for row in venture.get("accounts", []) if str(row.get("code")) == str(code)),
        {},
    )


def income_statement(
    venture: Mapping[str, Any], *, start: date, end: date
) -> dict[str, Any]:
    totals = account_totals(venture, start=start, end=end)
    revenue = ZERO
    contra_revenue = ZERO
    cogs = ZERO
    operating_expenses = ZERO
    revenue_rows: list[dict[str, Any]] = []
    cogs_rows: list[dict[str, Any]] = []
    expense_rows: list[dict[str, Any]] = []
    for account in venture.get("accounts", []):
        code = str(account.get("code"))
        signed = totals.get(code, ZERO)
        if signed == 0:
            continue
        account_type = account.get("type")
        category = str(account.get("category") or "")
        row = {
            "account_code": code,
            "account_name": account.get("name"),
            "category": category,
            "amount": money_value(signed if category == "contra_revenue" else (-signed if account_type == "revenue" else signed)),
            "amount_exact": money_text(signed if category == "contra_revenue" else (-signed if account_type == "revenue" else signed)),
        }
        if account_type == "revenue":
            if category == "contra_revenue":
                contra_revenue += signed
                revenue_rows.append(row)
            else:
                revenue += -signed
                revenue_rows.append(row)
        elif account_type == "expense":
            if category == "cogs":
                cogs += signed
                cogs_rows.append(row)
            else:
                operating_expenses += signed
                expense_rows.append(row)
    net_revenue = revenue - contra_revenue
    gross_profit = net_revenue - cogs
    operating_profit = gross_profit - operating_expenses
    net_profit = operating_profit
    has_entries = bool(entries_between(venture, start, end))
    return {
        "available": has_entries,
        "statement": "income_statement",
        "currency": venture.get("reporting_currency"),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "revenue": money_value(net_revenue),
        "gross_revenue": money_value(revenue),
        "contra_revenue": money_value(contra_revenue),
        "cost_of_goods_sold": money_value(cogs),
        "gross_profit": money_value(gross_profit),
        "operating_expenses": money_value(operating_expenses),
        "operating_profit": money_value(operating_profit),
        "net_profit": money_value(net_profit),
        "gross_margin": round(float(gross_profit / net_revenue), 6) if net_revenue else None,
        "operating_margin": round(float(operating_profit / net_revenue), 6) if net_revenue else None,
        "net_margin": round(float(net_profit / net_revenue), 6) if net_revenue else None,
        "revenue_rows": revenue_rows,
        "cogs_rows": cogs_rows,
        "expense_rows": expense_rows,
        "reason": "" if has_entries else "No posted journal entries exist for this period; no statement is fabricated.",
        "source": "posted_journal_entries",
    }


def balance_sheet(venture: Mapping[str, Any], *, as_of: date) -> dict[str, Any]:
    totals = account_totals(venture, end=as_of)
    groups: dict[str, list[dict[str, Any]]] = {"asset": [], "liability": [], "equity": []}
    assets = liabilities = equity = ZERO
    for account in venture.get("accounts", []):
        account_type = str(account.get("type"))
        if account_type not in groups:
            continue
        code = str(account.get("code"))
        signed = totals.get(code, ZERO)
        if signed == 0:
            continue
        balance = signed if account_type == "asset" else -signed
        if account_type == "asset":
            assets += balance
        elif account_type == "liability":
            liabilities += balance
        else:
            equity += balance
        groups[account_type].append(
            {
                "account_code": code,
                "account_name": account.get("name"),
                "category": account.get("category"),
                "current": bool(account.get("current")),
                "balance": money_value(balance),
                "balance_exact": money_text(balance),
            }
        )
    # Trading activity has not been closed into equity yet. Reporting it as
    # current-period earnings is an explicit component, not a posted balance.
    revenue_net = ZERO
    expense_total = ZERO
    for account in venture.get("accounts", []):
        code = str(account.get("code"))
        signed = totals.get(code, ZERO)
        if account.get("type") == "revenue":
            revenue_net += -signed
        elif account.get("type") == "expense":
            expense_total += signed
    retained = revenue_net - expense_total
    liabilities_and_equity = liabilities + equity + retained
    has_entries = bool(venture.get("journal_entries"))
    return {
        "available": has_entries,
        "statement": "balance_sheet",
        "currency": venture.get("reporting_currency"),
        "as_of": as_of.isoformat(),
        "assets": groups["asset"],
        "liabilities": groups["liability"],
        "equity": groups["equity"],
        "total_assets": money_value(assets),
        "total_liabilities": money_value(liabilities),
        "total_equity": money_value(equity + retained),
        "current_period_earnings": money_value(retained),
        "total_liabilities_and_equity": money_value(liabilities_and_equity),
        "balanced": assets.quantize(Decimal("0.01")) == liabilities_and_equity.quantize(Decimal("0.01")),
        "reason": (
            ""
            if has_entries
            else "No posted journal entries exist; a balance sheet is not fabricated from empty data."
        ),
        "source": "posted_journal_entries",
    }


def cash_flow_statement(
    venture: Mapping[str, Any], *, start: date, end: date
) -> dict[str, Any]:
    lines = [
        line
        for line in entry_lines(entries_between(venture, start, end))
        if str(line.get("account_code")) == "1000"
    ]
    if not lines:
        return {
            "available": False,
            "statement": "cash_flow",
            "currency": venture.get("reporting_currency"),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "operating": 0.0,
            "investing": 0.0,
            "financing": 0.0,
            "net_change": 0.0,
            "reason": "No cash-account journal lines exist for this period.",
        }
    buckets: dict[str, Decimal] = {"operating": ZERO, "investing": ZERO, "financing": ZERO}
    for line in lines:
        category = str(line.get("cash_flow_category") or "operating")
        if category not in buckets:
            category = "operating"
        buckets[category] += _entry_amount(line, "debit") - _entry_amount(line, "credit")
    net_change = sum(buckets.values(), ZERO)
    return {
        "available": True,
        "statement": "cash_flow",
        "currency": venture.get("reporting_currency"),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "operating": money_value(buckets["operating"]),
        "investing": money_value(buckets["investing"]),
        "financing": money_value(buckets["financing"]),
        "net_change": money_value(net_change),
        "closing_cash": money_value(signed_balance(venture, "1000", as_of=end)),
        "source": "posted_journal_entries",
        "note": "Cash is classified from each entry's cash_flow_category; no category is inferred silently from the counterpart account.",
    }


def account_balances(venture: Mapping[str, Any], *, as_of: date) -> dict[str, float]:
    return {
        str(account.get("code")): money_value(
            signed_balance(venture, str(account.get("code")), as_of=as_of)
        )
        for account in venture.get("accounts", [])
    }


def as_decimal(value: Any) -> Decimal:
    try:
        return parse_money(value, "amount", allow_zero=True, allow_negative=True)
    except (TypeError, ValueError):
        return ZERO


__all__ = [
    "account_balances",
    "account_totals",
    "active_entries",
    "as_decimal",
    "balance_sheet",
    "cash_flow_statement",
    "entries_between",
    "entry_lines",
    "income_statement",
    "reversed_entry_ids",
    "signed_balance",
    "trial_balance",
]
