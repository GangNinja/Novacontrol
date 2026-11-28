"""Revenue, expense, cash, working-capital and profitability analytics.

Every metric is labelled ``actual``, ``estimate`` or ``not_calculable`` and
carries the period, source and method used. Missing inputs never become zero:
the metric reports ``NOT_CALCULABLE`` with the exact reason.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from novacontrol.finops.ledger import (
    balance_sheet,
    cash_flow_statement,
    income_statement,
    signed_balance,
)
from novacontrol.finops.models import money_value

NOT_CALCULABLE = "NOT_CALCULABLE"


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def unavailable(reason: str, *, method: str = "", period: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "status": NOT_CALCULABLE,
        "value": None,
        "reason": reason,
        "method": method,
        "period": period,
        "kind": "not_calculable",
    }


def metric(
    value: float | int | None,
    *,
    kind: str = "actual",
    method: str = "",
    period: str = "",
    source: str = "",
    note: str = "",
) -> dict[str, Any]:
    if value is None:
        return unavailable(note or "The required inputs were not recorded.", method=method, period=period)
    return {
        "available": True,
        "status": "AVAILABLE",
        "value": round(float(value), 4),
        "reason": "",
        "method": method,
        "period": period,
        "source": source,
        "kind": kind,
        "note": note,
    }


def recurring_revenue(records: Sequence[Mapping[str, Any]]) -> float:
    return round(
        sum(float(row.get("reporting_net", 0) or 0) for row in records if row.get("recurring")),
        2,
    )


def revenue_summary(venture: Mapping[str, Any], *, as_of: date) -> dict[str, Any]:
    records = [row for row in venture.get("revenue_records", []) if row.get("status") != "cancelled"]
    booked = invoice = recognized = collected = refunds = taxes = ZERO = Decimal("0.00")
    booked_records: list[Mapping[str, Any]] = []
    invoice_records: list[Mapping[str, Any]] = []
    recognized_records: list[Mapping[str, Any]] = []
    collected_records: list[Mapping[str, Any]] = []
    product: dict[str, float] = defaultdict(float)
    customer: dict[str, float] = defaultdict(float)
    overdue = Decimal("0.00")
    for row in records:
        net = Decimal(str(row.get("reporting_net", "0.00")))
        booked_date = _date(row.get("booked_date"))
        if booked_date and booked_date <= as_of:
            booked += net
            booked_records.append(row)
        invoice_date = _date(row.get("invoice_date"))
        if invoice_date and invoice_date <= as_of:
            invoice += net
            invoice_records.append(row)
            due = _date(row.get("due_date"))
            if row.get("collection_date") in (None, "") and due and due < as_of:
                overdue += net
        recognition_date = _date(row.get("recognition_date"))
        if recognition_date and recognition_date <= as_of:
            recognized += net
            recognized_records.append(row)
            product[str(row.get("product_ref") or "unassigned")] += float(net)
            customer[str(row.get("customer_ref") or "unassigned")] += float(net)
        collection_date = _date(row.get("collection_date"))
        if collection_date and collection_date <= as_of:
            collected += net
            collected_records.append(row)
        refund_events = row.get("refund_events")
        if isinstance(refund_events, list) and refund_events:
            for event in refund_events:
                if not isinstance(event, Mapping):
                    continue
                event_date = _date(event.get("date"))
                if event_date is None or event_date <= as_of:
                    refunds += Decimal(str(event.get("amount", "0.00")))
        else:
            # Legacy records have only the running total, with no refund date.
            refunds += Decimal(str(row.get("reporting_refund", "0.00")))
        if invoice_date and invoice_date <= as_of:
            taxes += Decimal(str(row.get("reporting_tax", "0.00")))
    deferred = max(invoice - recognized, Decimal("0.00"))
    outstanding = max(invoice - collected, Decimal("0.00"))
    return {
        "available": bool(records),
        "as_of": as_of.isoformat(),
        "currency": venture.get("reporting_currency"),
        "booked_revenue": money_value(booked),
        "invoiced_revenue": money_value(invoice),
        "recognized_revenue": money_value(recognized),
        "collected_cash": money_value(collected),
        "deferred_revenue": money_value(deferred),
        "outstanding_invoices": money_value(outstanding),
        "overdue_receivables": money_value(overdue),
        "refunds": money_value(refunds),
        "taxes": money_value(taxes),
        "recurring_recognized_revenue": recurring_revenue(recognized_records),
        "counts": {
            "booked": len(booked_records),
            "invoiced": len(invoice_records),
            "recognized": len(recognized_records),
            "collected": len(collected_records),
        },
        "by_product": [
            {"product_ref": key, "recognized_revenue": round(value, 2)}
            for key, value in sorted(product.items(), key=lambda item: item[1], reverse=True)
        ],
        "by_customer": [
            {"customer_ref": key, "recognized_revenue": round(value, 2)}
            for key, value in sorted(customer.items(), key=lambda item: item[1], reverse=True)
        ],
        "note": (
            "Booked, invoiced, recognized and collected are separate observations. "
            "Deferred revenue is invoiced net revenue not yet recognized; an outstanding "
            "invoice is not cash. A pipeline deal or forecast is never included."
        ),
    }


def expense_summary(venture: Mapping[str, Any], *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    buckets: dict[str, Decimal] = {
        "actual": Decimal("0.00"),
        "estimated": Decimal("0.00"),
        "committed": Decimal("0.00"),
        "projected": Decimal("0.00"),
    }
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    unapproved_actual = Decimal("0.00")
    count = 0
    for row in venture.get("expense_records", []):
        row_date = _date(row.get("transaction_date"))
        if start and (row_date is None or row_date < start):
            continue
        if end and (row_date is None or row_date > end):
            continue
        amount = Decimal(str(row.get("reporting_amount", "0.00")))
        status = str(row.get("status") or "actual")
        if status not in buckets:
            continue
        if status == "actual" and row.get("approval_status") != "approved":
            unapproved_actual += amount
            continue
        buckets[status] += amount
        if status == "actual":
            categories[str(row.get("category") or "other")] += amount
            count += 1
    return {
        "available": any(buckets.values()) or bool(venture.get("expense_records")),
        "currency": venture.get("reporting_currency"),
        "period": {"start": start.isoformat() if start else "", "end": end.isoformat() if end else ""},
        "actual": money_value(buckets["actual"]),
        "estimated": money_value(buckets["estimated"]),
        "committed": money_value(buckets["committed"]),
        "projected": money_value(buckets["projected"]),
        "unapproved_actual": money_value(unapproved_actual),
        "by_category": [
            {"category": key, "actual": money_value(value)}
            for key, value in sorted(categories.items(), key=lambda item: item[1], reverse=True)
        ],
        "actual_count": count,
        "note": (
            "Only approved actual expenses are financial actuals. Estimated, committed and "
            "projected expenses are shown separately and are never posted as incurred costs."
        ),
    }


def monthly_recognized_revenue(venture: Mapping[str, Any], months: int = 6, *, as_of: date | None = None) -> list[dict[str, Any]]:
    end = as_of or date.today()
    rows: list[dict[str, Any]] = []
    for offset in range(months - 1, -1, -1):
        month_end = (end.replace(day=1) - timedelta(days=1))
        for _ in range(offset):
            month_end = (month_end.replace(day=1) - timedelta(days=1))
        month_start = month_end.replace(day=1)
        total = Decimal("0.00")
        count = 0
        for record in venture.get("revenue_records", []):
            recognized = _date(record.get("recognition_date"))
            if recognized and month_start <= recognized <= month_end and record.get("status") != "cancelled":
                total += Decimal(str(record.get("reporting_net", "0.00")))
                count += 1
        rows.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "recognized_revenue": money_value(total),
                "records": count,
                "observed": count > 0,
            }
        )
    return rows


def monthly_actual_expenses(venture: Mapping[str, Any], months: int = 6, *, as_of: date | None = None) -> list[dict[str, Any]]:
    end = as_of or date.today()
    rows: list[dict[str, Any]] = []
    for offset in range(months - 1, -1, -1):
        month_end = end.replace(day=1) - timedelta(days=1)
        for _ in range(offset):
            month_end = month_end.replace(day=1) - timedelta(days=1)
        month_start = month_end.replace(day=1)
        total = Decimal("0.00")
        count = 0
        for record in venture.get("expense_records", []):
            if record.get("status") != "actual" or record.get("approval_status") != "approved":
                continue
            transaction = _date(record.get("transaction_date"))
            if transaction and month_start <= transaction <= month_end:
                total += Decimal(str(record.get("reporting_amount", "0.00")))
                count += 1
        rows.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "actual_expenses": money_value(total),
                "records": count,
                "observed": count > 0,
            }
        )
    return rows


def working_capital(
    venture: Mapping[str, Any],
    *,
    as_of: date,
    lookback_days: int = 90,
) -> dict[str, Any]:
    start = as_of - timedelta(days=lookback_days)
    statement = income_statement(venture, start=start, end=as_of)
    balances = {
        "cash": money_value(signed_balance(venture, "1000", as_of=as_of)),
        "accounts_receivable": money_value(signed_balance(venture, "1100", as_of=as_of)),
        "inventory": money_value(signed_balance(venture, "1200", as_of=as_of)),
        "prepaid": money_value(signed_balance(venture, "1300", as_of=as_of)),
        "accounts_payable": money_value(signed_balance(venture, "2000", as_of=as_of)),
        "deferred_revenue": money_value(signed_balance(venture, "2100", as_of=as_of)),
        "tax_payable": money_value(signed_balance(venture, "2200", as_of=as_of)),
    }
    current_assets = balances["cash"] + balances["accounts_receivable"] + balances["inventory"] + balances["prepaid"]
    current_liabilities = (
        balances["accounts_payable"] + balances["deferred_revenue"] + balances["tax_payable"]
    )
    current_ratio = current_assets / current_liabilities if current_liabilities > 0 else None
    quick_ratio = (
        (current_assets - balances["inventory"]) / current_liabilities
        if current_liabilities > 0
        else None
    )
    revenue = float(statement.get("revenue") or 0.0)
    cogs = float(statement.get("cost_of_goods_sold") or 0.0)
    ar = balances["accounts_receivable"]
    ap = balances["accounts_payable"]
    inventory = balances["inventory"]
    dso = (ar / revenue * lookback_days) if revenue > 0 and ar >= 0 else None
    dpo = (ap / cogs * lookback_days) if cogs > 0 and ap >= 0 else None
    dio = (inventory / cogs * lookback_days) if cogs > 0 and inventory >= 0 else None
    ccc = (dso + dio - dpo) if dso is not None and dio is not None and dpo is not None else None
    ar_turnover = (revenue / ar) if ar > 0 else None
    ap_turnover = (cogs / ap) if ap > 0 and cogs > 0 else None
    inventory_turnover = (cogs / inventory) if inventory > 0 and cogs > 0 else None
    cash = balances["cash"]
    burn_rows = []
    for offset in range(3):
        month_end = as_of.replace(day=1) - timedelta(days=1)
        for _ in range(offset):
            month_end = month_end.replace(day=1) - timedelta(days=1)
        month_start = month_end.replace(day=1)
        cash_flow = cash_flow_statement(venture, start=month_start, end=month_end)
        if cash_flow.get("available"):
            burn_rows.append(float(cash_flow.get("net_change") or 0.0))
    average_burn = (sum(burn_rows) / len(burn_rows)) if len(burn_rows) >= 2 else None
    runway = (cash / -average_burn) if average_burn is not None and average_burn < 0 and cash >= 0 else None
    return {
        "available": bool(venture.get("journal_entries")),
        "as_of": as_of.isoformat(),
        "currency": venture.get("reporting_currency"),
        "balances": balances,
        "current_assets": round(current_assets, 2),
        "current_liabilities": round(current_liabilities, 2),
        "working_capital": round(current_assets - current_liabilities, 2),
        "current_ratio": metric(current_ratio, kind="actual", method="current assets / current liabilities"),
        "quick_ratio": metric(quick_ratio, kind="actual", method="(current assets − inventory) / current liabilities"),
        "receivables_turnover": metric(ar_turnover, kind="actual", method=f"period revenue / ending receivables ({lookback_days} days)"),
        "payables_turnover": metric(ap_turnover, kind="actual", method=f"period COGS / ending payables ({lookback_days} days)"),
        "inventory_turnover": metric(inventory_turnover, kind="actual", method=f"period COGS / ending inventory ({lookback_days} days)"),
        "days_sales_outstanding": metric(dso, kind="actual", method=f"ending AR / revenue × {lookback_days}"),
        "days_payable_outstanding": metric(dpo, kind="actual", method=f"ending AP / COGS × {lookback_days}"),
        "days_inventory_outstanding": metric(dio, kind="actual", method=f"ending inventory / COGS × {lookback_days}"),
        "cash_conversion_cycle": metric(ccc, kind="actual", method="DSO + DIO − DPO"),
        "cash_balance": cash,
        "cash_runway_months": metric(
            runway,
            kind="estimate",
            method="cash / average monthly net operating cash burn over up to 3 observed months",
            note="Requires at least two observed months and positive burn; cash runs out only if that burn continues.",
        ),
        "operating_cash_flow": cash_flow_statement(venture, start=start, end=as_of),
        "method_note": (
            "Turnover and day metrics use ending balances against observed period flows; "
            "average balances were not recorded. Missing revenue/COGS yields NOT_CALCULABLE."
        ),
    }


def profitability(
    venture: Mapping[str, Any],
    *,
    start: date,
    end: date,
    customer_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    statement = income_statement(venture, start=start, end=end)
    expenses = expense_summary(venture, start=start, end=end)
    recognized = revenue_summary(venture, as_of=end).get("recognized_revenue", 0.0)
    customer_data: Mapping[str, Any] = customer_snapshot if isinstance(customer_snapshot, Mapping) else {}
    raw_customers = customer_data.get("customers")
    customers: list[Any] = raw_customers if isinstance(raw_customers, list) else []
    raw_analytics = customer_data.get("analytics")
    analytics: Mapping[str, Any] = raw_analytics if isinstance(raw_analytics, Mapping) else {}
    raw_acquisition = analytics.get("acquisition")
    acquisition: Mapping[str, Any] = raw_acquisition if isinstance(raw_acquisition, Mapping) else {}
    raw_retention = analytics.get("retention")
    retention: Mapping[str, Any] = raw_retention if isinstance(raw_retention, Mapping) else {}
    raw_cac = acquisition.get("acquisition_cost")
    cac_block: Mapping[str, Any] = raw_cac if isinstance(raw_cac, Mapping) else {}
    churn_block = retention.get("churn_rate")
    churn = float(churn_block) if isinstance(churn_block, (int, float)) and not isinstance(churn_block, bool) else None
    raw_summary = customer_data.get("summary")
    snapshot_summary: Mapping[str, Any] = raw_summary if isinstance(raw_summary, Mapping) else {}
    customer_count = len(customers) or int(snapshot_summary.get("customers") or 0)
    raw_cac_value = cac_block.get("value")
    cac = float(raw_cac_value) if cac_block.get("available") and raw_cac_value is not None else None
    arpc = (recognized / customer_count) if customer_count > 0 and recognized > 0 else None
    ltv = (arpc / churn) if arpc is not None and churn and churn > 0 else None
    payback = (cac / arpc) if cac is not None and arpc and arpc > 0 else None
    gross_margin = statement.get("gross_margin")
    fixed = float(statement.get("operating_expenses") or 0.0)
    break_even = (fixed / gross_margin) if gross_margin and gross_margin > 0 else None
    transaction_count = int(revenue_summary(venture, as_of=end).get("counts", {}).get("recognized", 0))
    cost_per_transaction = (float(expenses.get("actual") or 0.0) / transaction_count) if transaction_count else None
    product_rows: dict[str, dict[str, float]] = {}
    for record in venture.get("revenue_records", []):
        recognized_date = _date(record.get("recognition_date"))
        if not recognized_date or not (start <= recognized_date <= end) or record.get("status") == "cancelled":
            continue
        key = str(record.get("product_ref") or "unassigned")
        product_rows.setdefault(key, {"revenue": 0.0, "cost": 0.0})
        product_rows[key]["revenue"] += float(record.get("reporting_net", 0) or 0)
    for record in venture.get("expense_records", []):
        transaction = _date(record.get("transaction_date"))
        if (
            record.get("status") != "actual"
            or record.get("approval_status") != "approved"
            or not transaction
            or not (start <= transaction <= end)
        ):
            continue
        key = str(record.get("product_ref") or "unassigned")
        product_rows.setdefault(key, {"revenue": 0.0, "cost": 0.0})
        product_rows[key]["cost"] += float(record.get("reporting_amount", 0) or 0)
    by_product = []
    for key, row in sorted(product_rows.items(), key=lambda item: item[1]["revenue"], reverse=True):
        gross = row["revenue"] - row["cost"]
        by_product.append(
            {
                "product_ref": key,
                "recognized_revenue": round(row["revenue"], 2),
                "allocated_actual_cost": round(row["cost"], 2),
                "contribution": round(gross, 2),
                "margin": round(gross / row["revenue"], 6) if row["revenue"] else None,
                "allocation_note": "Cost is included only when the expense or journal record names this product.",
            }
        )
    return {
        "available": bool(statement.get("available")),
        "currency": venture.get("reporting_currency"),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "revenue": statement.get("revenue"),
        "gross_profit": statement.get("gross_profit"),
        "gross_margin": statement.get("gross_margin"),
        "operating_profit": statement.get("operating_profit"),
        "operating_margin": statement.get("operating_margin"),
        "net_profit": statement.get("net_profit"),
        "net_margin": statement.get("net_margin"),
        "contribution_margin": statement.get("gross_margin"),
        "contribution_note": "Contribution margin is gross profit / revenue from posted ledger activity; it is not a per-unit figure unless unit costs were recorded.",
        "customer_acquisition_cost": metric(cac, kind="actual", method="recorded Phase 37 acquisition cost / acquired customers", source="phase37"),
        "customer_lifetime_value": metric(ltv, kind="estimate", method="ARPC / monthly churn rate", note="Requires observed recognized revenue, customers and a positive churn rate."),
        "customer_payback_months": metric(payback, kind="estimate", method="CAC / monthly ARPC"),
        "average_revenue_per_customer": metric(arpc, kind="actual", method="recognized revenue / recorded customers"),
        "revenue_per_product": by_product,
        "cost_per_transaction": metric(cost_per_transaction, kind="estimate", method="approved actual expenses / recognized revenue records"),
        "break_even_revenue": metric(break_even, kind="estimate", method="operating expenses / gross margin ratio"),
        "customer_count": customer_count,
        "customer_churn_rate": churn,
    }


def operations_dashboard(venture: Mapping[str, Any], *, today: date) -> dict[str, Any]:
    tasks = venture.get("operational_tasks", [])
    completed = [row for row in tasks if row.get("status") == "complete"]
    blocked = [row for row in tasks if row.get("status") == "blocked"]
    overdue = [
        row
        for row in tasks
        if row.get("due_date")
        and row.get("status") not in {"complete", "cancelled"}
        and (_date(row.get("due_date")) or today) < today
    ]
    dependencies: list[dict[str, Any]] = []
    task_ids = {str(row.get("id")) for row in tasks}
    for row in tasks:
        missing = [dep for dep in row.get("dependencies", []) if dep not in task_ids]
        if missing:
            dependencies.append({"task_id": row.get("id"), "missing_dependencies": missing})
    utilization = [float(row.get("resource_utilization")) for row in tasks if row.get("resource_utilization") is not None]
    completion_rate = (len(completed) / len(tasks)) if tasks else None
    delivery_days = [
        float(row.get("delivery_days"))
        for row in tasks
        if row.get("delivery_days") is not None
    ]
    return {
        "available": bool(tasks) or bool(venture.get("operational_objectives")),
        "tasks": len(tasks),
        "completed": len(completed),
        "blocked": len(blocked),
        "overdue": len(overdue),
        "completion_rate": metric(completion_rate, method="completed tasks / recorded tasks"),
        "average_delivery_days": metric(
            (sum(delivery_days) / len(delivery_days)) if delivery_days else None,
            method="recorded delivery days / completed tasks with delivery evidence",
        ),
        "average_resource_utilization": metric(
            (sum(utilization) / len(utilization)) if utilization else None,
            method="mean recorded resource utilization",
        ),
        "operational_cost": round(sum(float(row.get("actual_cost", 0) or 0) for row in tasks), 2),
        "open_risks": len([row for row in venture.get("operational_risks", []) if row.get("status") == "open"]),
        "objectives": len(venture.get("operational_objectives", [])),
        "milestones": len(venture.get("milestones", [])),
        "workflows": len(venture.get("workflows", [])),
        "dependency_issues": dependencies,
        "note": "Completion, delay and utilization are recorded operational observations; no task outcome is inferred from a scheduled task alone.",
    }


def revenue_concentration(summary: Mapping[str, Any]) -> float | None:
    raw_rows = summary.get("by_customer")
    rows: list[Mapping[str, Any]] = (
        [row for row in raw_rows if isinstance(row, Mapping)] if isinstance(raw_rows, list) else []
    )
    # "unassigned" is missing evidence, not one giant customer. Counting it
    # raised a concentration alert for every venture that omitted customer_ref.
    rows = [row for row in rows if str(row.get("customer_ref") or "") not in {"", "unassigned"}]
    total = sum(float(row.get("recognized_revenue", 0) or 0) for row in rows)
    if total <= 0 or not rows:
        return None
    return max(float(row.get("recognized_revenue", 0) or 0) for row in rows) / total


def monthly_series_pair(
    venture: Mapping[str, Any], *, months: int = 6, as_of: date | None = None
) -> list[dict[str, Any]]:
    revenue = monthly_recognized_revenue(venture, months=months, as_of=as_of)
    expenses = monthly_actual_expenses(venture, months=months, as_of=as_of)
    return [
        {
            "month": left["month"],
            "recognized_revenue": left["recognized_revenue"],
            "actual_expenses": right["actual_expenses"],
            "revenue_observed": left["observed"],
            "expense_observed": right["observed"],
        }
        for left, right in zip(revenue, expenses)
    ]


__all__ = [
    "NOT_CALCULABLE",
    "expense_summary",
    "metric",
    "monthly_actual_expenses",
    "monthly_recognized_revenue",
    "monthly_series_pair",
    "operations_dashboard",
    "profitability",
    "revenue_concentration",
    "revenue_summary",
    "unavailable",
    "working_capital",
]
