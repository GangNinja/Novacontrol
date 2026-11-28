"""Business-health rules for Phase 38.

Alerts are evidence-gated: a metric with missing inputs or stale source data
never produces an alert. Every alert names the observed value, threshold,
evidence, recommended investigation and timestamp.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from novacontrol.finops.analytics import (
    expense_summary,
    monthly_actual_expenses,
    monthly_recognized_revenue,
    revenue_concentration,
    revenue_summary,
)
from novacontrol.finops.models import money_value

DEFAULT_THRESHOLDS: dict[str, float] = {
    "cash_runway_months": 6.0,
    "revenue_concentration": 0.6,
    "expense_growth": 0.25,
    "revenue_decline": 0.10,
    "cac_growth": 0.25,
    "gross_margin_decline": 0.05,
    "churn_rate": 0.05,
    "overdue_receivable_days": 30,
    "budget_overrun": 1.0,
    "infrastructure_growth": 0.30,
    "blocked_tasks": 1,
    "stale_data_days": 120,
}


def _severity(value: float, threshold: float) -> str:
    if threshold <= 0:
        return "high" if value > 0 else "low"
    ratio = value / threshold
    if ratio >= 1.75:
        return "critical"
    if ratio >= 1.25:
        return "high"
    if ratio > 1.0:
        return "medium"
    return "low"


def _alert(
    venture: Mapping[str, Any],
    *,
    metric: str,
    observed: Any,
    threshold: Any,
    severity: str,
    evidence: Sequence[str],
    investigation: str,
    today: date,
) -> dict[str, Any]:
    return {
        "id": "",
        "venture_id": venture.get("id"),
        "metric": metric,
        "observed_value": observed,
        "threshold": threshold,
        "severity": severity,
        "evidence": list(evidence),
        "recommended_investigation": investigation,
        "timestamp": today.isoformat(),
        "status": "open",
        "resolved_at": "",
        "resolution_note": "",
        "kind": "alert",
    }


def _month_growth(rows: list[dict[str, Any]], key: str) -> tuple[float | None, dict[str, Any] | None, dict[str, Any] | None]:
    observed = [row for row in rows if row.get("observed")]
    if len(observed) < 2:
        return None, None, None
    previous, current = observed[-2], observed[-1]
    before = float(previous.get(key, 0) or 0)
    after = float(current.get(key, 0) or 0)
    if before <= 0:
        return None, previous, current
    return (after - before) / before, previous, current


def evaluate_health(
    venture: Mapping[str, Any],
    *,
    today: date,
    customer_snapshot: Mapping[str, Any] | None,
    budget_reports: Sequence[Mapping[str, Any]],
    previous_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_THRESHOLDS, **dict(venture.get("health_thresholds") or {})}
    revenue = revenue_summary(venture, as_of=today)
    expenses = expense_summary(venture)
    revenue_months = monthly_recognized_revenue(venture, months=4, as_of=today)
    expense_months = monthly_actual_expenses(venture, months=4, as_of=today)
    latest_dates: list[date] = []
    for record in venture.get("revenue_records", []):
        for key in ("recognition_date", "collection_date", "booked_date"):
            try:
                latest_dates.append(date.fromisoformat(str(record.get(key) or "")[:10]))
            except ValueError:
                pass
    for record in venture.get("expense_records", []):
        try:
            latest_dates.append(date.fromisoformat(str(record.get("transaction_date") or "")[:10]))
        except ValueError:
            pass
    for entry in venture.get("journal_entries", []):
        try:
            latest_dates.append(date.fromisoformat(str(entry.get("date") or "")[:10]))
        except ValueError:
            pass
    latest = max(latest_dates) if latest_dates else None
    stale_days = int(thresholds.get("stale_data_days", 120))
    if latest is None:
        # An empty venture has no observation at all. Reporting it as "fresh"
        # produced a no-breach alert claiming records existed, which is exactly
        # the missing-data-as-evidence failure this gate is meant to prevent.
        return {
            "as_of": today.isoformat(),
            "data_fresh": False,
            "freshness": {
                "latest_observed_date": "",
                "stale_after_days": stale_days,
                "reason": "No financial record has been observed yet; no health metric is asserted.",
            },
            "alerts": [],
            "metrics": {},
            "note": "No alert is created from missing or stale data.",
        }
    stale = bool((today - latest).days > stale_days)
    alerts: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    if stale:
        return {
            "as_of": today.isoformat(),
            "data_fresh": False,
            "freshness": {
                "latest_observed_date": latest.isoformat() if latest else "",
                "stale_after_days": stale_days,
                "reason": "Source data is older than the configured freshness limit; no trend alert is asserted.",
            },
            "alerts": [],
            "metrics": metrics,
            "note": "No alert is created from missing or stale data.",
        }
    raw_summary = customer_snapshot.get("analytics") if isinstance(customer_snapshot, Mapping) else None
    summary: Mapping[str, Any] = raw_summary if isinstance(raw_summary, Mapping) else {}
    raw_retention = summary.get("retention")
    retention: Mapping[str, Any] = raw_retention if isinstance(raw_retention, Mapping) else {}
    raw_acquisition = summary.get("acquisition")
    acquisition: Mapping[str, Any] = raw_acquisition if isinstance(raw_acquisition, Mapping) else {}
    churn = retention.get("churn_rate")
    if isinstance(churn, (int, float)) and not isinstance(churn, bool):
        metrics["churn_rate"] = churn
        if churn > float(thresholds["churn_rate"]):
            alerts.append(
                _alert(
                    venture,
                    metric="customer_churn",
                    observed=round(float(churn), 4),
                    threshold=float(thresholds["churn_rate"]),
                    severity=_severity(float(churn), float(thresholds["churn_rate"])),
                    evidence=[f"Phase 37 retention reported churn_rate={churn}", "Requires an observed churn numerator and denominator."],
                    investigation="Check which customers churned, when, and whether the cause is product, support or price.",
                    today=today,
                )
            )
    concentration = revenue_concentration(revenue)
    if concentration is not None:
        metrics["revenue_concentration"] = round(concentration, 6)
        if concentration > float(thresholds["revenue_concentration"]):
            alerts.append(
                _alert(
                    venture,
                    metric="revenue_concentration",
                    observed=round(concentration, 4),
                    threshold=float(thresholds["revenue_concentration"]),
                    severity="high" if concentration > 0.8 else "medium",
                    evidence=[
                        f"Top customer is {round(concentration * 100, 1)}% of recognized revenue.",
                        f"Recognized revenue {revenue['currency']} {revenue['recognized_revenue']:,.2f} across {len(revenue['by_customer'])} customer reference(s).",
                    ],
                    investigation="Review contract renewal dates and build a diversification plan for the largest account.",
                    today=today,
                )
            )
    growth, previous, current = _month_growth(revenue_months, "recognized_revenue")
    if growth is not None and current and previous:
        metrics["revenue_growth"] = round(growth, 6)
        if growth < -float(thresholds["revenue_decline"]):
            alerts.append(
                _alert(
                    venture,
                    metric="declining_revenue",
                    observed=round(growth, 4),
                    threshold=-float(thresholds["revenue_decline"]),
                    severity=_severity(abs(growth), float(thresholds["revenue_decline"])),
                    evidence=[f"{previous['month']} recognized {previous['recognized_revenue']:,.2f}.", f"{current['month']} recognized {current['recognized_revenue']:,.2f}."],
                    investigation="Separate churn, downgrade, timing and recognition effects before changing the forecast.",
                    today=today,
                )
            )
    expense_growth, previous_expense, current_expense = _month_growth(expense_months, "actual_expenses")
    if expense_growth is not None and current_expense and previous_expense:
        metrics["expense_growth"] = round(expense_growth, 6)
        if expense_growth > float(thresholds["expense_growth"]):
            alerts.append(
                _alert(
                    venture,
                    metric="expense_growth",
                    observed=round(expense_growth, 4),
                    threshold=float(thresholds["expense_growth"]),
                    severity=_severity(expense_growth, float(thresholds["expense_growth"])),
                    evidence=[f"{previous_expense['month']} actual expenses {previous_expense['actual_expenses']:,.2f}.", f"{current_expense['month']} actual expenses {current_expense['actual_expenses']:,.2f}."],
                    investigation="Break the increase down by expense category and vendor before approving new commitments.",
                    today=today,
                )
            )
    infra_by_month: dict[str, float] = {}
    for record in venture.get("expense_records", []):
        if record.get("category") != "infrastructure" or record.get("status") != "actual" or record.get("approval_status") != "approved":
            continue
        month = str(record.get("transaction_date") or "")[:7]
        infra_by_month[month] = infra_by_month.get(month, 0.0) + float(record.get("reporting_amount", 0) or 0)
    ordered_infra = sorted(infra_by_month.items())
    if len(ordered_infra) >= 2:
        before = ordered_infra[-2][1]
        after = ordered_infra[-1][1]
        if before > 0:
            growth = (after - before) / before
            metrics["infrastructure_growth"] = round(growth, 6)
            if growth > float(thresholds["infrastructure_growth"]):
                alerts.append(
                    _alert(
                        venture,
                        metric="infrastructure_cost_spike",
                        observed=round(growth, 4),
                        threshold=float(thresholds["infrastructure_growth"]),
                        severity=_severity(growth, float(thresholds["infrastructure_growth"])),
                        evidence=[f"{ordered_infra[-2][0]} infrastructure {before:,.2f}.", f"{ordered_infra[-1][0]} infrastructure {after:,.2f}."],
                        investigation="Attribute the increase to compute, storage or network usage before resizing anything.",
                        today=today,
                    )
                )
    prior_cac = None
    if isinstance(previous_history, Mapping):
        raw_metrics = previous_history.get("metrics")
        prior_metrics: Mapping[str, Any] = raw_metrics if isinstance(raw_metrics, Mapping) else {}
        prior_cac = prior_metrics.get("cac")
    raw_cac = acquisition.get("acquisition_cost")
    current_cac: Mapping[str, Any] = raw_cac if isinstance(raw_cac, Mapping) else {}
    if current_cac.get("available") and isinstance(current_cac.get("value"), (int, float)):
        metrics["cac"] = float(current_cac["value"])
        if isinstance(prior_cac, (int, float)) and prior_cac > 0:
            growth = (float(current_cac["value"]) - float(prior_cac)) / float(prior_cac)
            metrics["cac_growth"] = round(growth, 6)
            if growth > float(thresholds["cac_growth"]):
                alerts.append(
                    _alert(
                        venture,
                        metric="increasing_acquisition_cost",
                        observed=round(growth, 4),
                        threshold=float(thresholds["cac_growth"]),
                        severity=_severity(growth, float(thresholds["cac_growth"])),
                        evidence=[f"Prior observed CAC {prior_cac:,.2f}.", f"Current observed CAC {float(current_cac['value']):,.2f}."],
                        investigation="Compare channel mix, conversion rate and sales cycle before increasing acquisition spend.",
                        today=today,
                    )
                )
    prior_margin = None
    if isinstance(previous_history, Mapping):
        raw_margin_metrics = previous_history.get("metrics")
        prior_margin_metrics: Mapping[str, Any] = (
            raw_margin_metrics if isinstance(raw_margin_metrics, Mapping) else {}
        )
        prior_margin = prior_margin_metrics.get("gross_margin")
    margin = None
    if expenses.get("available") and revenue.get("recognized_revenue"):
        margin = (float(revenue["recognized_revenue"]) - float(expenses.get("actual") or 0.0)) / float(revenue["recognized_revenue"])
        metrics["gross_margin"] = round(margin, 6)
    if margin is not None and isinstance(prior_margin, (int, float)) and margin < float(prior_margin) - float(thresholds["gross_margin_decline"]):
        alerts.append(
            _alert(
                venture,
                metric="falling_gross_margin",
                observed=round(margin, 4),
                threshold=round(float(prior_margin) - float(thresholds["gross_margin_decline"]), 4),
                severity="high" if margin < 0 else "medium",
                evidence=[f"Prior observed gross margin {float(prior_margin):.4f}.", f"Current gross margin {margin:.4f}."],
                investigation="Check price changes, refunds, COGS and product mix; the current margin is a gross proxy, not a substitute for the income statement.",
                today=today,
            )
        )
    overdue_days = int(thresholds["overdue_receivable_days"])
    due_alerts = []
    for record in venture.get("revenue_records", []):
        if record.get("status") == "cancelled" or record.get("collection_date"):
            continue
        due = str(record.get("due_date") or "")[:10]
        if not due:
            continue
        try:
            due_date = date.fromisoformat(due)
        except ValueError:
            continue
        if (today - due_date).days >= overdue_days:
            due_alerts.append(record.get("id", ""))
    if due_alerts:
        outstanding = float(revenue.get("outstanding_invoices") or 0.0)
        alerts.append(
            _alert(
                venture,
                metric="overdue_receivables",
                observed=len(due_alerts),
                threshold=overdue_days,
                severity="high" if outstanding > 0 else "medium",
                evidence=[f"{len(due_alerts)} invoice(s) are at least {overdue_days} days past due.", f"Outstanding invoices {revenue['currency']} {outstanding:,.2f}.", "Only invoices with a recorded due date are counted."],
                investigation="Review the overdue invoices, customer contact history and collection status; do not assume collectability.",
                today=today,
            )
        )
    for report in budget_reports:
        for line in report.get("lines", []) if isinstance(report, Mapping) else []:
            actual = line.get("actual") if isinstance(line, Mapping) else None
            budget = line.get("budget") if isinstance(line, Mapping) else None
            if isinstance(actual, (int, float)) and isinstance(budget, (int, float)) and budget > 0:
                ratio = actual / budget
                if ratio > float(thresholds["budget_overrun"]):
                    alerts.append(
                        _alert(
                            venture,
                            metric="budget_overrun",
                            observed=round(ratio, 4),
                            threshold=float(thresholds["budget_overrun"]),
                            severity=_severity(ratio, float(thresholds["budget_overrun"])),
                            evidence=[f"{line.get('label') or line.get('account_code') or 'budget line'} actual {actual:,.2f} against {budget:,.2f}."],
                            investigation="Confirm whether the spend is committed, recurring or a data-entry error before revising the budget.",
                            today=today,
                        )
                    )
    metrics["blocked_tasks"] = len([row for row in venture.get("operational_tasks", []) if row.get("status") == "blocked"])
    if metrics["blocked_tasks"] > int(thresholds["blocked_tasks"]):
        alerts.append(
            _alert(
                venture,
                metric="operational_bottleneck",
                observed=metrics["blocked_tasks"],
                threshold=int(thresholds["blocked_tasks"]),
                severity="high" if metrics["blocked_tasks"] > 2 else "medium",
                evidence=[f"{metrics['blocked_tasks']} task(s) are blocked.", "Blocked tasks are recorded task states, not inferred from elapsed time."],
                investigation="Inspect the blocking dependency, owner and due date; resolve or explicitly cancel the task.",
                today=today,
            )
        )
    if not alerts:
        alerts.append(
            _alert(
                venture,
                metric="no_threshold_breach",
                observed="within configured thresholds",
                threshold=thresholds,
                severity="info",
                evidence=["At least one observed financial record exists and no configured threshold is breached."],
                investigation="No action required; continue periodic review.",
                today=today,
            )
        )
    return {
        "as_of": today.isoformat(),
        "data_fresh": True,
        "freshness": {"latest_observed_date": latest.isoformat() if latest else "", "stale_after_days": stale_days},
        "metrics": metrics,
        "alerts": alerts,
        "note": "Alerts are deterministic threshold crossings. Missing and stale inputs are reported, never converted into alerts.",
    }


__all__ = ["DEFAULT_THRESHOLDS", "evaluate_health"]
