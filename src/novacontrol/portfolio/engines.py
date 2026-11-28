"""Deterministic portfolio evaluation, risk, allocation and simulation engines.

Every function here consumes a *snapshot* assembled by ``PortfolioService``.
Snapshots contain Phase 35–38 read results and their timestamps; engines never
mutate a venture, ledger, budget or source record. Missing inputs stay missing
and are surfaced as ``available: false`` rather than scored as zero.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.portfolio.models import (
    RISK_CATEGORIES,
    SEVERITIES,
    metric,
)

DIMENSIONS: tuple[str, ...] = ("financial", "market", "operational", "strategic")


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ratio(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp((value - low) / (high - low), 0.0, 1.0)


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"risk-{digest}"


def _finance(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(snapshot.get("finance"))


def _income(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(_finance(snapshot).get("income"))


def _expenses(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(_finance(snapshot).get("expenses"))


def _capital(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(_finance(snapshot).get("working_capital"))


def _operations(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(_finance(snapshot).get("operations"))


def _customer(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(snapshot.get("customer"))


def _product(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(snapshot.get("product"))


def _opportunity(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(snapshot.get("opportunity"))


def _component(
    name: str,
    dimension: str,
    score: float | None,
    *,
    method: str,
    evidence: Sequence[str] = (),
    metrics: Sequence[Mapping[str, Any]] = (),
    missing: str = "",
) -> dict[str, Any]:
    kinds = {
        str(item.get("kind")) for item in metrics if isinstance(item, Mapping) and item.get("kind")
    }
    confidence = "low" if score is None else "high" if "actual" in kinds else "medium"
    return {
        "name": name,
        "dimension": dimension,
        "score": None if score is None else round(_clamp(score), 4),
        "method": method,
        "confidence": confidence,
        "evidence": [str(item) for item in evidence],
        "source_metrics": [dict(item) for item in metrics],
        "missing": [] if score is not None else [missing or f"{name} is unavailable"],
    }


def venture_metrics(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return provenance-labelled financial/market/operational metrics."""
    income = _income(snapshot)
    expenses = _expenses(snapshot)
    capital = _capital(snapshot)
    ops = _operations(snapshot)
    customer = _customer(snapshot)
    product = _product(snapshot)
    opportunity = _opportunity(snapshot)
    currency = str(snapshot.get("currency") or "")
    period = (
        str((_finance(snapshot).get("period") or {}).get("start", ""))
        + "/"
        + str((_finance(snapshot).get("period") or {}).get("end", ""))
    )
    revenue = _num(income.get("revenue"))
    gross_margin = _num(income.get("gross_margin"))
    operating_margin = _num(income.get("operating_margin"))
    net_profit = _num(income.get("net_profit"))
    actual_expenses = _num(expenses.get("actual"))
    cash_flow = _map(_finance(snapshot).get("cash_flow"))
    net_cash = _num(cash_flow.get("net_change"))
    cash = _num(capital.get("cash_balance"))
    runway = _map(capital.get("cash_runway_months"))
    runway_value = _num(runway.get("value")) if runway.get("available") else None
    prior_revenue = _num(snapshot.get("prior_revenue"))
    growth = None
    if revenue is not None and prior_revenue is not None and prior_revenue > 0:
        growth = round((revenue - prior_revenue) / prior_revenue, 6)
    budgets = [
        dict(row) for row in _rows(_finance(snapshot).get("budgets")) if isinstance(row, Mapping)
    ]
    approved_budgets = [row for row in budgets if row.get("status") == "approved"]
    approved_budget = round(
        sum(_num(row.get("total_budget")) or 0.0 for row in approved_budgets), 2
    )
    budget_reports = [
        dict(row)
        for row in _rows(_finance(snapshot).get("budget_reports"))
        if isinstance(row, Mapping)
    ]
    budget_actual = round(sum(_num(row.get("total_actual")) or 0.0 for row in budget_reports), 2)
    budget_variance = round(approved_budget - budget_actual, 2) if approved_budgets else None

    analytics = _map(customer.get("analytics"))
    acquisition = _map(analytics.get("acquisition"))
    sales = _map(analytics.get("sales"))
    retention = _map(analytics.get("retention"))
    support = _map(analytics.get("support"))
    summary = _map(snapshot.get("customer_summary"))
    customers = int(summary.get("customers") or 0) if summary else None
    qualified = _num(acquisition.get("qualified_leads"))
    conversion = _num(sales.get("conversion_rate"))
    churn = _num(retention.get("churn_rate"))
    open_tickets = _num(summary.get("open_tickets")) if summary else None
    if open_tickets is None:
        open_tickets = _num(support.get("open_tickets"))

    task_rows = _rows(ops.get("task_rows"))
    tasks = _num(ops.get("tasks"))
    completed = _num(ops.get("completed"))
    blocked = _num(ops.get("blocked")) or 0.0
    task_ratio = (completed / tasks) if tasks and completed is not None else None
    open_defects = _num((_map(product.get("summary"))).get("open_defects"))
    product_state = str(product.get("state") or _map(product.get("summary")).get("state") or "")
    readiness = _map(product.get("readiness"))
    readiness_status = str(readiness.get("status") or product.get("readiness_status") or "")
    product_progress = _map(product.get("progress"))
    strategic = _map(snapshot.get("strategic_inputs"))
    alignment = _num(strategic.get("alignment"))
    scalability = _num(strategic.get("scalability"))
    market_opportunity = _num(strategic.get("market_opportunity"))
    competitive = _num(strategic.get("competitive_position"))

    opportunity_score = _map(opportunity.get("score"))
    opportunity_weighted = _num(opportunity_score.get("weighted_score"))
    if market_opportunity is None and opportunity_weighted is not None:
        market_opportunity = (
            _clamp(opportunity_weighted / 100.0)
            if opportunity_weighted > 1
            else _clamp(opportunity_weighted)
        )

    return {
        "venture_id": snapshot.get("venture_id"),
        "venture_name": snapshot.get("name"),
        "currency": currency,
        "period": period,
        "source_updated_at": snapshot.get("source_updated_at", ""),
        "fresh": bool(snapshot.get("fresh", False)),
        "revenue": metric(
            revenue,
            kind="actual",
            method="income statement revenue from posted journal entries",
            source="phase38.posted_journal",
            period=period,
            reason=str(income.get("reason") or "no posted journal entries in the period"),
        ),
        "actual_expenses": metric(
            actual_expenses,
            kind="actual",
            method="approved actual expenses in the period",
            source="phase38.expense_records",
            period=period,
            reason="no approved actual expense exists in the period",
        ),
        "net_profit": metric(
            net_profit,
            kind="actual",
            method="income statement net profit",
            source="phase38.posted_journal",
            period=period,
            reason=str(income.get("reason") or "no posted journal entries in the period"),
        ),
        "gross_margin": metric(
            gross_margin,
            kind="actual",
            method="gross profit / revenue",
            source="phase38.income_statement",
            period=period,
            reason="revenue is unavailable or zero",
        ),
        "operating_margin": metric(
            operating_margin,
            kind="actual",
            method="operating profit / revenue",
            source="phase38.income_statement",
            period=period,
            reason="revenue is unavailable or zero",
        ),
        "revenue_growth": metric(
            growth,
            kind="actual",
            method="(current period revenue − immediately preceding equal period) / prior revenue",
            source="phase38.income_statement",
            period=period,
            reason="prior-period revenue is unavailable or zero",
        ),
        "net_cash_flow": metric(
            net_cash,
            kind="actual",
            method="cash flow statement net change",
            source="phase38.cash_flow_statement",
            period=period,
            reason=str(cash_flow.get("reason") or "cash movement is unavailable"),
        ),
        "cash_balance": metric(
            cash,
            kind="actual",
            method="posted cash account balance at period end",
            source="phase38.working_capital",
            period=period,
            reason="no posted cash balance exists",
        ),
        "cash_runway_months": metric(
            runway_value,
            kind="estimate",
            method=str(runway.get("method") or "cash / observed average monthly burn"),
            source="phase38.working_capital",
            period=period,
            reason=str(
                runway.get("reason") or "runway requires two observed months of positive burn"
            ),
        ),
        "approved_budget": metric(
            approved_budget if approved_budgets else None,
            kind="approved_budget",
            method="sum of planned lines in approved Phase 38 budgets",
            source="phase38.budgets",
            reason="no approved budget exists; a draft budget is not authorization",
        ),
        "budget_actual": metric(
            budget_actual if budget_reports else None,
            kind="actual",
            method="approved actual expenses matched to approved budget lines",
            source="phase38.budget_report",
            reason="no approved budget report is available",
        ),
        "budget_variance": metric(
            budget_variance,
            kind="actual",
            method="approved budget total − approved actual expenses",
            source="phase38.budget_report",
            reason="no approved budget exists",
            note="Positive means remaining; negative means over budget.",
        ),
        "customers": metric(
            customers,
            kind="actual",
            method="count of recorded Phase 37 customers",
            source="phase37.customer_workspace",
            reason="no Phase 37 customer workspace or customer record is linked",
        ),
        "qualified_leads": metric(
            qualified,
            kind="actual",
            method="count of leads in a qualified or later status",
            source="phase37.analytics",
            reason="no Phase 37 lead analytics are available",
        ),
        "sales_conversion": metric(
            conversion,
            kind="actual",
            method="recorded Phase 37 sales conversion rate",
            source="phase37.analytics",
            reason="Phase 37 has no recorded conversion rate",
        ),
        "churn_rate": metric(
            churn,
            kind="actual",
            method="recorded Phase 37 churn rate",
            source="phase37.analytics",
            reason="Phase 37 has no recorded churn rate",
        ),
        "open_tickets": metric(
            open_tickets,
            kind="actual",
            method="open support tickets",
            source="phase37.customer_workspace",
            reason="no Phase 37 support snapshot is available",
        ),
        "task_completion": metric(
            task_ratio,
            kind="actual",
            method="completed operational tasks / total operational tasks",
            source="phase38.operations",
            reason="no operational task exists",
        ),
        "blocked_tasks": metric(
            blocked if tasks else None,
            kind="actual",
            method="count of blocked operational tasks",
            source="phase38.operations",
            reason="no operational task exists",
        ),
        "product_state": metric(
            product_state or None,
            kind="actual",
            method="Phase 36 product state",
            source="phase36.product",
            reason="no Phase 36 product is linked",
        ),
        "launch_readiness": metric(
            readiness_status or None,
            kind="actual",
            method="Phase 36 human launch-readiness state",
            source="phase36.product",
            reason="no Phase 36 readiness assessment is available",
        ),
        "open_defects": metric(
            open_defects,
            kind="actual",
            method="open Phase 36 defects",
            source="phase36.product",
            reason="no Phase 36 product is linked",
        ),
        "product_progress": metric(
            _num(product_progress.get("ratio")),
            kind="actual",
            method="completed product tasks / total product tasks",
            source="phase36.product",
            reason="no Phase 36 task plan is available",
        ),
        "strategic_alignment": metric(
            alignment,
            kind="assessment",
            method="operator-recorded strategic input; not an observed financial fact",
            source="portfolio.strategic_inputs",
            reason="no strategic alignment assessment is recorded",
        ),
        "scalability": metric(
            scalability,
            kind="assessment",
            method="operator-recorded strategic input",
            source="portfolio.strategic_inputs",
            reason="no scalability assessment is recorded",
        ),
        "market_opportunity": metric(
            market_opportunity,
            kind="assessment",
            method="Phase 35 weighted evidence score or operator-recorded strategic input",
            source="phase35.opportunity",
            reason="no Phase 35 score or strategic input is available",
        ),
        "competitive_position": metric(
            competitive,
            kind="assessment",
            method="operator-recorded strategic input",
            source="portfolio.strategic_inputs",
            reason="no competitive-position assessment is recorded",
        ),
        "technical": {
            "task_rows": task_rows,
            "revenue": revenue,
            "actual_expenses": actual_expenses,
            "net_profit": net_profit,
            "gross_margin": gross_margin,
            "operating_margin": operating_margin,
            "net_cash": net_cash,
            "cash": cash,
            "runway": runway_value,
            "growth": growth,
            "approved_budget": approved_budget if approved_budgets else None,
            "budget_actual": budget_actual if budget_reports else None,
            "budget_variance": budget_variance,
            "customers": customers,
            "qualified": qualified,
            "conversion": conversion,
            "churn": churn,
            "open_tickets": open_tickets,
            "tasks": tasks,
            "completed": completed,
            "blocked": blocked if tasks else None,
            "open_defects": open_defects,
            "product_state": product_state,
            "readiness": readiness_status,
            "alignment": alignment,
            "scalability": scalability,
            "market_opportunity": market_opportunity,
            "competitive": competitive,
        },
    }


def _score_components(
    snapshot: Mapping[str, Any], metrics: Mapping[str, Any]
) -> list[dict[str, Any]]:
    t = _map(metrics.get("technical"))
    components: list[dict[str, Any]] = []

    growth = _num(t.get("growth"))
    components.append(
        _component(
            "revenue_growth",
            "financial",
            None if growth is None else _ratio(growth, -0.20, 0.50),
            method="linear scale: -20% growth = 0, +50% growth = 1",
            evidence=[] if growth is None else [f"Revenue growth {growth:.1%}."],
            metrics=[metrics.get("revenue_growth", {})],
            missing="requires current and prior-period posted revenue",
        )
    )
    gross = _num(t.get("gross_margin"))
    components.append(
        _component(
            "gross_margin",
            "financial",
            None if gross is None else _clamp(gross),
            method="gross margin as a fraction, capped to 0..1",
            evidence=[] if gross is None else [f"Gross margin {gross:.1%}."],
            metrics=[metrics.get("gross_margin", {})],
            missing="requires posted revenue and cost of goods sold",
        )
    )
    operating = _num(t.get("operating_margin"))
    components.append(
        _component(
            "operating_margin",
            "financial",
            None if operating is None else _ratio(operating, -0.20, 0.30),
            method="linear scale: -20% operating margin = 0, +30% = 1",
            evidence=[] if operating is None else [f"Operating margin {operating:.1%}."],
            metrics=[metrics.get("operating_margin", {})],
            missing="requires posted revenue and operating expenses",
        )
    )
    cash_flow = _num(t.get("net_cash"))
    revenue = _num(t.get("revenue"))
    components.append(
        _component(
            "cash_generation",
            "financial",
            None
            if cash_flow is None
            else (
                0.5
                if revenue in (None, 0)
                else _ratio(cash_flow / max(abs(revenue), 1.0), -0.50, 0.30)
            ),
            method="period net cash change / revenue, scaled -50%..+30%; neutral 0.5 when revenue is absent",
            evidence=[] if cash_flow is None else [f"Net cash flow {cash_flow:,.2f}."],
            metrics=[metrics.get("net_cash_flow", {})],
            missing="requires posted cash movement",
        )
    )
    runway = _num(t.get("runway"))
    components.append(
        _component(
            "cash_runway",
            "financial",
            None if runway is None else _clamp(runway / 12.0),
            method="observed cash runway months / 12, capped to 1",
            evidence=[] if runway is None else [f"Estimated runway {runway:.1f} months."],
            metrics=[metrics.get("cash_runway_months", {})],
            missing="requires at least two observed months of positive cash burn",
        )
    )
    budget = _num(t.get("approved_budget"))
    actual = _num(t.get("budget_actual"))
    budget_score = None
    if budget is not None and actual is not None and budget > 0:
        budget_score = _clamp(1.0 - max(0.0, (actual - budget) / budget))
    components.append(
        _component(
            "budget_performance",
            "financial",
            budget_score,
            method="1 − positive overrun fraction against approved budget; under-spend is not penalised",
            evidence=[]
            if budget_score is None
            else [f"Approved budget {budget:,.2f}; actual {actual:,.2f}."],
            metrics=[metrics.get("budget_variance", {})],
            missing="requires an approved Phase 38 budget and matching actual expenses",
        )
    )

    conversion = _num(t.get("conversion"))
    components.append(
        _component(
            "sales_conversion",
            "market",
            None if conversion is None else _ratio(conversion, 0.0, 0.50),
            method="conversion rate / 50%, capped to 1",
            evidence=[] if conversion is None else [f"Sales conversion {conversion:.1%}."],
            metrics=[metrics.get("sales_conversion", {})],
            missing="Phase 37 has no recorded sales conversion",
        )
    )
    customers = _num(t.get("customers"))
    components.append(
        _component(
            "customer_traction",
            "market",
            None if customers is None else _clamp(customers / 25.0),
            method="recorded customers / 25, capped to 1; no growth inference without history",
            evidence=[] if customers is None else [f"{int(customers)} recorded customer(s)."],
            metrics=[metrics.get("customers", {})],
            missing="no recorded Phase 37 customer base",
        )
    )
    churn = _num(t.get("churn"))
    components.append(
        _component(
            "retention",
            "market",
            None if churn is None else _clamp(1.0 - _ratio(churn, 0.0, 0.30)),
            method="1 − churn scaled 0..30%; no churn inference without a recorded rate",
            evidence=[] if churn is None else [f"Observed churn {churn:.1%}."],
            metrics=[metrics.get("churn_rate", {})],
            missing="Phase 37 has no recorded churn rate",
        )
    )
    demand = _num(t.get("market_opportunity"))
    components.append(
        _component(
            "product_demand",
            "market",
            demand,
            method="Phase 35 evidence score normalised to 0..1; this is evidence strength, not demand revenue",
            evidence=[] if demand is None else [f"Phase 35 opportunity score {demand:.2f}."],
            metrics=[metrics.get("market_opportunity", {})],
            missing="no Phase 35 score or strategic market assessment",
        )
    )

    task_total = _num(t.get("tasks"))
    tasks_completed = _num(t.get("completed"))
    task_ratio = (
        tasks_completed / task_total
        if tasks_completed is not None and task_total is not None and task_total > 0
        else None
    )
    components.append(
        _component(
            "delivery_progress",
            "operational",
            task_ratio,
            method="completed operational tasks / total operational tasks",
            evidence=[]
            if task_ratio is None
            else [
                f"{int(_num(t.get('completed')) or 0)} of {int(_num(t.get('tasks')) or 0)} operational tasks complete."
            ],
            metrics=[metrics.get("task_completion", {})],
            missing="no Phase 38 operational task exists",
        )
    )
    defects = _num(t.get("open_defects"))
    components.append(
        _component(
            "product_reliability",
            "operational",
            None if defects is None else _clamp(1.0 - defects / 5.0),
            method="1 − open defects / 5, floored at 0; no defect count means unavailable",
            evidence=[] if defects is None else [f"{int(defects)} open product defect(s)."],
            metrics=[metrics.get("open_defects", {})],
            missing="no Phase 36 product or defect record",
        )
    )
    blocked = _num(t.get("blocked"))
    components.append(
        _component(
            "execution_health",
            "operational",
            None
            if blocked is None
            else _clamp(1.0 - blocked / max(_num(t.get("tasks")) or 1.0, 1.0)),
            method="1 − blocked operational tasks / total tasks",
            evidence=[] if blocked is None else [f"{int(blocked)} blocked operational task(s)."],
            metrics=[metrics.get("blocked_tasks", {})],
            missing="no Phase 38 operational task exists",
        )
    )
    progress = _num((metrics.get("product_progress") or {}).get("value"))
    components.append(
        _component(
            "development_progress",
            "operational",
            progress,
            method="completed Phase 36 product tasks / total tasks",
            evidence=[] if progress is None else [f"Product task completion {progress:.1%}."],
            metrics=[metrics.get("product_progress", {})],
            missing="no Phase 36 task plan exists",
        )
    )

    for key, label, method in (
        (
            "alignment",
            "portfolio_alignment",
            "operator-recorded alignment with portfolio objectives",
        ),
        (
            "market_opportunity",
            "market_opportunity",
            "Phase 35 evidence strength or strategic market assessment",
        ),
        ("scalability", "scalability", "operator-recorded scalability assessment"),
        (
            "competitive",
            "competitive_position",
            "operator-recorded competitive-position assessment",
        ),
    ):
        value = _num(t.get(key))
        components.append(
            _component(
                label,
                "strategic",
                value,
                method=method,
                evidence=[]
                if value is None
                else [f"{label.replace('_', ' ').title()} {value:.2f}."],
                metrics=[
                    metrics.get(
                        {
                            "alignment": "strategic_alignment",
                            "market_opportunity": "market_opportunity",
                            "scalability": "scalability",
                            "competitive": "competitive_position",
                        }[key],
                        {},
                    )
                ],
                missing=f"no recorded {label.replace('_', ' ')} assessment",
            )
        )
    return components


def evaluate_venture(
    snapshot: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    policy_name: str,
) -> dict[str, Any]:
    metrics = venture_metrics(snapshot)
    components = _score_components(snapshot, metrics)
    weights = {str(key): float(value) for key, value in _map(policy.get("weights")).items()}
    for dimension in DIMENSIONS:
        weights.setdefault(dimension, 0.0)
    total_weight = sum(max(0.0, value) for value in weights.values())
    if total_weight <= 0:
        raise ValueError("Evaluation policy weights must contain a positive weight.")
    weights = {key: value / total_weight for key, value in weights.items()}

    dimension_rows: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    weighted_available = 0.0
    available_weight = 0.0
    for dimension in DIMENSIONS:
        rows = [row for row in components if row["dimension"] == dimension]
        scored = [row for row in rows if row["score"] is not None]
        for row in rows:
            missing.extend(row["missing"])
        score = sum(float(row["score"]) for row in scored) / len(scored) if scored else None
        dimension_rows[dimension] = {
            "score": None if score is None else round(score, 4),
            "weight": round(weights[dimension], 4),
            "components": rows,
            "available_components": len(scored),
            "total_components": len(rows),
        }
        if score is not None and weights[dimension] > 0:
            weighted_available += score * weights[dimension]
            available_weight += weights[dimension]
    raw_score = weighted_available / available_weight if available_weight > 0 else None
    coverage = available_weight / total_weight if total_weight > 0 else 0.0
    # Coverage is a gate and a penalty: a venture with one good metric cannot
    # outrank a fully evidenced venture merely because the rest is missing.
    adjusted = None if raw_score is None else round(raw_score * coverage, 4)
    min_coverage = float(_map(policy).get("min_coverage", 0.50))
    eligible = bool(adjusted is not None and coverage >= min_coverage)
    confidence = "high" if coverage >= 0.8 else "medium" if coverage >= 0.6 else "low"
    return {
        "venture_id": snapshot.get("venture_id"),
        "venture_name": snapshot.get("name"),
        "policy": policy_name,
        "weights": {key: round(value, 4) for key, value in weights.items()},
        "dimensions": dimension_rows,
        "components": components,
        "raw_weighted_score": None if raw_score is None else round(raw_score, 4),
        "coverage": round(coverage, 4),
        "score": adjusted,
        "eligible_for_ranking": eligible,
        "confidence": confidence,
        "missing": list(dict.fromkeys(missing)),
        "metrics": metrics,
        "methodology": (
            "Dimension scores are the arithmetic mean of available component scores. "
            "The raw weighted score excludes unavailable components; the published score "
            "multiplies it by evidence coverage. Ventures below min_coverage are not rankable."
        ),
        "evaluated_at": snapshot.get("fetched_at", ""),
        "source_updated_at": snapshot.get("source_updated_at", ""),
    }


def rank_evaluations(evaluations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        evaluations,
        key=lambda row: (
            1 if row.get("eligible_for_ranking") else 0,
            float(row.get("score") or 0.0),
            float(row.get("coverage") or 0.0),
        ),
        reverse=True,
    )
    output = []
    for position, row in enumerate(ranked, start=1):
        output.append(
            {
                "rank": position if row.get("eligible_for_ranking") else None,
                "venture_id": row.get("venture_id"),
                "venture_name": row.get("venture_name"),
                "score": row.get("score"),
                "coverage": row.get("coverage"),
                "confidence": row.get("confidence"),
                "eligible_for_ranking": bool(row.get("eligible_for_ranking")),
                "reason": ""
                if row.get("eligible_for_ranking")
                else "Excluded from ranking because evidence coverage is below the policy minimum.",
            }
        )
    return output


def _risk(
    venture_id: str,
    venture_name: str,
    category: str,
    severity: str,
    likelihood: str,
    impact: str,
    observed: str,
    evidence: Sequence[str],
    mitigation: str,
    *,
    metric_name: str,
    scope: str = "venture",
) -> dict[str, Any]:
    if category not in RISK_CATEGORIES:
        raise ValueError(f"Unsupported risk category: {category}")
    if severity not in SEVERITIES:
        raise ValueError(f"Unsupported risk severity: {severity}")
    return {
        "id": _stable_id(venture_id, category, metric_name),
        "portfolio_id": "",
        "venture_id": venture_id,
        "venture_name": venture_name,
        "category": category,
        "scope": scope,
        "severity": severity,
        "likelihood": likelihood,
        "impact": impact,
        "observed": observed,
        "evidence": list(evidence),
        "mitigation": mitigation,
        "status": "open",
        "source": "deterministic_assessment",
        "created_at": "",
        "updated_at": "",
        "kind": "risk",
    }


def venture_risks(
    snapshot: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Any],
) -> list[dict[str, Any]]:
    metrics = venture_metrics(snapshot)
    t = _map(metrics.get("technical"))
    vid = str(snapshot.get("venture_id") or "")
    name = str(snapshot.get("name") or "")
    risks: list[dict[str, Any]] = []
    net_profit = _num(t.get("net_profit"))
    if net_profit is not None and net_profit < 0:
        risks.append(
            _risk(
                vid,
                name,
                "financial",
                "high" if net_profit < -1000 else "medium",
                "observed",
                "current period profitability",
                f"Net loss of {abs(net_profit):,.2f} {metrics.get('currency')} in the period.",
                [str(metrics.get("net_profit"))],
                "Review approved actual expenses and revenue quality; no automatic cost change is made.",
                metric_name="net_profit",
            )
        )
    runway = _num(t.get("runway"))
    threshold_runway = float(thresholds.get("cash_runway_months", 3.0))
    if runway is not None and runway < threshold_runway:
        risks.append(
            _risk(
                vid,
                name,
                "liquidity",
                "critical" if runway < max(1.0, threshold_runway / 2) else "high",
                "estimated",
                "cash availability",
                f"Observed burn implies {runway:.1f} month(s) of runway, below the {threshold_runway:.1f}-month threshold.",
                [str(metrics.get("cash_runway_months"))],
                "Prepare a funded operating plan or reduce discretionary spend; no transfer or commitment is executed.",
                metric_name="cash_runway_months",
            )
        )
    revenue = _num(t.get("revenue"))
    concentration = _map(_finance(snapshot).get("concentration"))
    max_customer = _num(concentration.get("max_customer_share"))
    threshold_concentration = float(thresholds.get("revenue_concentration", 0.60))
    if (
        revenue
        and revenue > 0
        and max_customer is not None
        and max_customer > threshold_concentration
    ):
        risks.append(
            _risk(
                vid,
                name,
                "revenue_concentration",
                "high" if max_customer >= 0.8 else "medium",
                "observed",
                "revenue durability",
                f"Largest assigned customer is {max_customer:.1%} of recognized revenue.",
                [
                    f"Named customer concentration {max_customer:.1%}",
                    f"Recognized revenue {revenue:,.2f}",
                ],
                "Diversify customer references and validate repeat demand; pipeline is not counted as revenue.",
                metric_name="max_customer_share",
            )
        )
    customers = _num(t.get("customers"))
    if (
        customers
        and customers > 0
        and max_customer is not None
        and max_customer > threshold_concentration
    ):
        risks.append(
            _risk(
                vid,
                name,
                "customer_concentration",
                "medium",
                "observed",
                "customer base",
                f"Customer concentration is {max_customer:.1%} among {int(customers)} recorded customer(s).",
                [f"Customers {int(customers)}", f"Largest customer share {max_customer:.1%}"],
                "Document account dependencies and a retention plan; do not share customer data across ventures.",
                metric_name="customer_concentration",
            )
        )
    churn = _num(t.get("churn"))
    threshold_churn = float(thresholds.get("customer_churn", 0.10))
    if churn is not None and churn > threshold_churn and customers:
        risks.append(
            _risk(
                vid,
                name,
                "market",
                "high" if churn > threshold_churn * 2 else "medium",
                "observed",
                "retention",
                f"Observed churn is {churn:.1%}, above the {threshold_churn:.1%} threshold.",
                [str(metrics.get("churn_rate"))],
                "Review customer-success evidence and support themes; no outbound action is taken.",
                metric_name="churn_rate",
            )
        )
    blocked = _num(t.get("blocked"))
    if blocked is not None and blocked > 0:
        risks.append(
            _risk(
                vid,
                name,
                "operational",
                "high" if blocked >= 3 else "medium",
                "observed",
                "delivery",
                f"{int(blocked)} operational task(s) are blocked.",
                [f"Task completion {t.get('completed')}/{t.get('tasks')}"],
                "Resolve dependencies and escalate the owner; portfolio does not silently reprioritise another venture.",
                metric_name="blocked_tasks",
            )
        )
    if blocked is None:
        pass
    open_defects = _num(t.get("open_defects"))
    if open_defects is not None and open_defects > 0:
        risks.append(
            _risk(
                vid,
                name,
                "product_delivery",
                "high" if open_defects >= 3 else "medium",
                "observed",
                "release quality",
                f"{int(open_defects)} open product defect(s).",
                [str(metrics.get("open_defects"))],
                "Resolve defects before a launch recommendation; a launch remains a human decision.",
                metric_name="open_defects",
            )
        )
    budget = _num(t.get("approved_budget"))
    actual = _num(t.get("budget_actual"))
    threshold_overrun = float(thresholds.get("budget_overrun", 0.10))
    if budget and actual is not None and actual > budget * (1 + threshold_overrun):
        overrun = (actual - budget) / budget
        risks.append(
            _risk(
                vid,
                name,
                "execution",
                "high" if overrun >= threshold_overrun * 2 else "medium",
                "observed",
                "approved budget",
                f"Actual spend is {overrun:.1%} above the approved budget.",
                [f"Approved budget {budget:,.2f}", f"Actual {actual:,.2f}"],
                "Require a revised budget through Phase 38 approval; the portfolio cannot reallocate funds.",
                metric_name="budget_overrun",
            )
        )
    # Phase 38's own operational risks are evidence, but are never merged into
    # this venture's ledger. Map only explicit categories the operator recorded.
    for row in _rows(_finance(snapshot).get("operational_risks")):
        if not isinstance(row, Mapping) or row.get("status") not in (None, "", "open"):
            continue
        raw_category = str(row.get("category") or "").lower().replace(" ", "_")
        category = raw_category if raw_category in RISK_CATEGORIES else "operational"
        severity = str(row.get("severity") or "medium").lower()
        if severity not in SEVERITIES:
            severity = "medium"
        risks.append(
            _risk(
                vid,
                name,
                category,
                severity,
                str(row.get("likelihood") or "recorded"),
                str(row.get("impact") or "recorded"),
                str(row.get("description") or row.get("title") or "Recorded operational risk."),
                [f"Phase 38 operational risk {row.get('id', '')}"],
                str(row.get("mitigation") or "Review the recorded mitigation and owner."),
                metric_name=f"phase38:{row.get('id', '')}",
            )
        )
    for alert in _rows(_finance(snapshot).get("open_alerts")):
        if not isinstance(alert, Mapping):
            continue
        severity = str(alert.get("severity") or "medium").lower()
        if severity not in SEVERITIES:
            severity = "medium"
        category = "financial" if "revenue" in str(alert.get("metric")) else "operational"
        risks.append(
            _risk(
                vid,
                name,
                category,
                severity,
                "observed",
                str(alert.get("metric") or "financial health"),
                f"Phase 38 open alert on {alert.get('metric')}: {alert.get('observed_value')}.",
                [f"Open Phase 38 alert {alert.get('id', '')}"],
                str(alert.get("recommended_action") or "Review the recorded Phase 38 alert."),
                metric_name=f"alert:{alert.get('metric', '')}",
            )
        )
    return risks


def correlated_risks(risks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in risks:
        if row.get("status") == "resolved":
            continue
        grouped[str(row.get("category"))].append(row)
    output: list[dict[str, Any]] = []
    for category, rows in sorted(grouped.items()):
        venture_ids = sorted({str(row.get("venture_id")) for row in rows if row.get("venture_id")})
        if len(venture_ids) < 2:
            continue
        highest = max(
            (str(row.get("severity")) for row in rows),
            key=lambda value: SEVERITIES.index(value) if value in SEVERITIES else 0,
        )
        output.append(
            {
                "id": _stable_id("portfolio", category),
                "category": category,
                "scope": "portfolio",
                "severity": highest,
                "venture_ids": venture_ids,
                "evidence": [
                    f"{row.get('venture_name') or row.get('venture_id')}: {row.get('observed')}"
                    for row in rows
                ],
                "correlation_basis": (
                    "The same recorded risk category appears in at least two ventures. "
                    "This is a shared-exposure observation, not a statistical correlation or "
                    "a claim of diversification."
                ),
                "mitigation": "Review the affected ventures together and assign an accountable owner.",
                "status": "open",
            }
        )
    return output


def allocation_recommendation(
    snapshot: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    metrics = venture_metrics(snapshot)
    t = _map(metrics.get("technical"))
    vid = str(snapshot.get("venture_id") or "")
    name = str(snapshot.get("name") or "")
    currency = str(snapshot.get("currency") or "")
    runway = _num(t.get("runway"))
    margin = _num(t.get("operating_margin"))
    growth = _num(t.get("growth"))
    budget = _num(t.get("approved_budget"))
    actual = _num(t.get("budget_actual"))
    revenue = _num(t.get("revenue"))
    lifecycle = str(snapshot.get("lifecycle_status") or "")
    evidence: list[str] = []
    assumptions: list[str] = []
    benefits: list[str] = []
    costs: list[str] = []
    risks: list[str] = []
    alternatives: list[str] = []
    confidence = "low"
    action = "validate"
    rationale = ""
    if revenue is not None:
        evidence.append(f"Period revenue {currency} {revenue:,.2f}.")
        confidence = "medium"
    if margin is not None:
        evidence.append(f"Operating margin {margin:.1%}.")
    if runway is not None:
        evidence.append(f"Estimated cash runway {runway:.1f} months.")
        confidence = "medium-high" if growth is not None else "medium"
    if budget is not None and actual is not None:
        evidence.append(
            f"Approved budget {currency} {budget:,.2f}; actual {currency} {actual:,.2f}."
        )
    if lifecycle in {"WINDING_DOWN", "ARCHIVED"}:
        action = "pause"
        rationale = (
            "The venture is already being wound down or archived; no new allocation is recommended."
        )
    elif runway is not None and runway < float(thresholds.get("cash_runway_months", 3.0)):
        action = "reduce" if revenue else "pause"
        rationale = "Observed runway is below the portfolio threshold."
        benefits.append("Extends the period before cash is exhausted.")
        costs.append("May slow delivery and customer support.")
        risks.append("A reduction can damage retention if applied without a service plan.")
        alternatives.extend(["delay", "reallocate"])
    elif (
        budget is not None
        and actual is not None
        and budget > 0
        and actual > budget * (1 + float(thresholds.get("budget_overrun", 0.10)))
    ):
        action = "reduce"
        rationale = "Actual spend is above the approved budget."
        benefits.append("Restores spending discipline against the approved budget.")
        costs.append("May defer planned development.")
        risks.append("Cost reduction can create delivery risk.")
        alternatives.extend(["delay", "reallocate"])
    elif margin is not None and margin < 0 and revenue:
        action = "reduce"
        rationale = "The venture is loss-making on posted entries."
        benefits.append("Reduces the cash requirement while preserving the option to continue.")
        costs.append("Could slow growth.")
        risks.append("Cost cuts may reduce product quality.")
        alternatives.extend(["restructure", "validate"])
    elif growth is not None and growth > 0.15 and (margin is None or margin >= 0):
        action = "increase"
        rationale = "Observed revenue growth is positive and there is no recorded margin loss."
        benefits.append("Additional approved resource may compound observed demand.")
        costs.append("Any increase requires a separately approved Phase 38 budget.")
        risks.append("Growth may not persist; forecast values are not actuals.")
        alternatives.extend(["maintain", "validate"])
    elif revenue is None and lifecycle in {
        "OPPORTUNITY_DISCOVERY",
        "VALIDATION",
        "PRODUCT_DEVELOPMENT",
        "INTERNAL_TESTING",
    }:
        action = "validate" if lifecycle in {"OPPORTUNITY_DISCOVERY", "VALIDATION"} else "delay"
        rationale = "There is not enough actual financial evidence for an operating allocation."
        benefits.append("Avoids committing resources on unvalidated demand.")
        costs.append("Delays potential learning.")
        risks.append("Further validation may still fail.")
        alternatives.extend(["pause", "maintain"])
    else:
        action = "maintain"
        rationale = "Recorded evidence supports neither a clear increase nor a clear reduction."
        benefits.append("Preserves the current operating plan.")
        costs.append("No additional funding is authorized.")
        risks.append("Status quo may underfund a developing opportunity.")
        alternatives.extend(["increase", "validate"])
    assumptions.append(
        "The recommendation uses only recorded actuals and explicitly labelled estimates."
    )
    assumptions.append(
        "A Phase 38 approved budget is the only authority to spend; this proposal changes nothing."
    )
    if not evidence:
        evidence.append("No actual financial or customer evidence is available for this venture.")
    return {
        "id": f"alloc-{vid}",
        "venture_id": vid,
        "venture_name": name,
        "currency": currency,
        "action": action,
        "rationale": rationale,
        "evidence": evidence,
        "expected_benefits": benefits,
        "estimated_costs": costs,
        "risks": risks,
        "assumptions": assumptions,
        "alternatives": alternatives,
        "confidence": confidence,
        "approval_required": True,
        "approval_policy": "Phase 38 approved budget and a human portfolio approver; no funds are moved.",
        "applied": False,
        "created_at": snapshot.get("fetched_at", ""),
        "source_updated_at": snapshot.get("source_updated_at", ""),
        "kind": "recommendation",
    }


def build_scenario(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    scenario_type: str,
    target_venture_id: str,
    horizon_months: int,
    assumptions: Mapping[str, Any],
    base_currency: str,
    fx_rates: Mapping[str, Mapping[str, Any]],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    horizon = max(1, min(int(horizon_months), 36))
    selected: list[Mapping[str, Any]]
    if scenario_type == "new_venture" and not target_venture_id:
        selected = [
            {
                "venture_id": "",
                "name": str(assumptions.get("name") or "New venture"),
                "currency": base_currency,
            }
        ]
    else:
        selected = [
            row for row in snapshots if target_venture_id in ("", str(row.get("venture_id")))
        ]
        if not selected:
            raise ValueError("Scenario target venture is not in this portfolio.")
    missing: list[str] = []
    rows: list[dict[str, Any]] = []
    total_revenue = total_expenses = 0.0
    total_cash = 0.0
    base_revenue = base_expenses = 0.0
    for snapshot in selected:
        metrics = venture_metrics(snapshot)
        t = _map(metrics.get("technical"))
        revenue = _num(t.get("revenue"))
        expenses = _num(t.get("actual_expenses"))
        cash = _num(t.get("cash"))
        if scenario_type == "new_venture":
            # A new venture has no actual history by definition; its inputs are
            # explicit assumptions, not invented source actuals.
            revenue = expenses = cash = 0.0
        currency = str(snapshot.get("currency") or base_currency)
        rate = 1.0 if currency == base_currency else _num(_map(fx_rates.get(currency)).get("rate"))
        if rate is None:
            missing.append(f"no explicit FX rate for {currency} to {base_currency}")
            continue
        if scenario_type != "new_venture" and (revenue is None or expenses is None or cash is None):
            missing.append(
                f"{snapshot.get('name')}: revenue, approved actual expenses or cash is unavailable"
            )
            continue
        assert revenue is not None and expenses is not None and cash is not None
        custom_revenue = _num(assumptions.get("monthly_revenue"))
        custom_expense = _num(assumptions.get("monthly_expenses"))
        monthly_revenue = custom_revenue if custom_revenue is not None else revenue
        monthly_expense = custom_expense if custom_expense is not None else expenses
        starting_cash = cash * rate
        scenario_revenue = monthly_revenue
        scenario_expense = monthly_expense
        one_time = 0.0
        pause_months = max(0, int(assumptions.get("pause_months") or 0))
        delay_months = max(0, int(assumptions.get("delay_months") or 0))
        revenue_factor = 1.0 + float(assumptions.get("revenue_change_pct") or 0.0) / 100.0
        expense_factor = 1.0 + float(assumptions.get("expense_change_pct") or 0.0) / 100.0
        if scenario_type == "launch_delay":
            # Delay the revenue start; operating costs continue during the delay.
            delay_months = max(delay_months, 1)
        elif scenario_type == "development_spend":
            scenario_expense += float(assumptions.get("additional_monthly_cost") or 0.0)
        elif scenario_type == "reduce_opex":
            scenario_expense *= max(
                0.0, 1.0 - abs(float(assumptions.get("reduction_pct") or 10.0)) / 100.0
            )
        elif scenario_type == "acquisition_strategy":
            scenario_expense += float(assumptions.get("additional_monthly_marketing") or 0.0)
            scenario_revenue *= (
                1.0 + float(assumptions.get("expected_revenue_lift_pct") or 0.0) / 100.0
            )
        elif scenario_type == "marketing_budget":
            scenario_expense += float(assumptions.get("marketing_delta") or 0.0)
        elif scenario_type == "pause_venture":
            # Pausing stops revenue, not recorded operating costs. An explicit
            # pause cost is added once; costs can be reduced through assumptions.
            pause_months = max(pause_months, 1)
            one_time = float(assumptions.get("pause_cost") or 0.0)
        elif scenario_type == "scale_venture":
            scenario_revenue *= 1.0 + float(assumptions.get("revenue_growth_pct") or 20.0) / 100.0
            scenario_expense *= 1.0 + float(assumptions.get("expense_growth_pct") or 10.0) / 100.0
        elif scenario_type == "revenue_shortfall":
            scenario_revenue *= max(
                0.0, 1.0 - abs(float(assumptions.get("shortfall_pct") or 20.0)) / 100.0
            )
        elif scenario_type == "infrastructure_cost":
            scenario_expense += float(assumptions.get("additional_monthly_infrastructure") or 0.0)
        elif scenario_type == "new_venture":
            scenario_revenue = float(assumptions.get("expected_monthly_revenue") or 0.0)
            scenario_expense = float(assumptions.get("monthly_operating_cost") or 0.0)
            starting_cash = float(assumptions.get("initial_investment") or 0.0)
        scenario_revenue *= revenue_factor
        scenario_expense *= expense_factor
        lost_revenue_months = delay_months
        if scenario_type == "pause_venture":
            lost_revenue_months += pause_months
        projected_revenue = scenario_revenue * max(0, horizon - lost_revenue_months)
        projected_expense = scenario_expense * horizon + one_time
        projected_cash = starting_cash + projected_revenue - projected_expense
        first_month_net = scenario_revenue - scenario_expense
        if scenario_type == "launch_delay" and delay_months > 0:
            first_month_net = -scenario_expense
        if scenario_type == "pause_venture" and pause_months > 0:
            first_month_net = -scenario_expense
        runway_cash = starting_cash - one_time
        runway = (
            runway_cash / -first_month_net if first_month_net < 0 and runway_cash >= 0 else None
        )
        row = {
            "venture_id": snapshot.get("venture_id"),
            "venture_name": snapshot.get("name"),
            "currency": base_currency,
            "source_currency": currency,
            "fx_rate": rate,
            "kind": "simulated",
            "actual_monthly_revenue": round(monthly_revenue * rate, 2),
            "actual_monthly_expenses": round(monthly_expense * rate, 2),
            "simulated_monthly_revenue": round(scenario_revenue * rate, 2),
            "simulated_monthly_expenses": round(scenario_expense * rate, 2),
            "projected_revenue": round(projected_revenue * rate, 2),
            "projected_expenses": round(projected_expense * rate, 2),
            "projected_net_cash_flow": round((projected_revenue - projected_expense) * rate, 2),
            "projected_ending_cash": round(projected_cash * rate, 2),
            "simulated_runway_months": None if runway is None else round(runway, 2),
        }
        rows.append(row)
        total_revenue += projected_revenue * rate
        total_expenses += projected_expense * rate
        total_cash += projected_cash * rate
        base_revenue += monthly_revenue * rate * horizon
        base_expenses += monthly_expense * rate * horizon
    available = bool(rows) and not missing
    risk_before = (
        "high"
        if total_cash < 0
        else "medium"
        if base_revenue and total_revenue < base_revenue * 0.8
        else "low"
    )
    return {
        "id": "",
        "kind": "simulated",
        "scenario_type": scenario_type,
        "target_venture_id": target_venture_id,
        "horizon_months": horizon,
        "currency": base_currency,
        "available": available,
        "assumptions": dict(assumptions),
        "methodology": (
            "Each venture's actual period revenue and approved actual expenses are used as the "
            "monthly baseline unless the operator supplies an explicit baseline; projections are "
            "arithmetic scenarios, not forecasts or guarantees. Missing FX/data excludes the venture."
        ),
        "baseline": {
            "period_revenue": round(base_revenue, 2),
            "period_expenses": round(base_expenses, 2),
            "period_net": round(base_revenue - base_expenses, 2),
        },
        "projected": {
            "revenue": round(total_revenue, 2),
            "expenses": round(total_expenses, 2),
            "net_cash_flow": round(total_revenue - total_expenses, 2),
            "ending_cash": round(total_cash, 2),
            "risk_exposure": risk_before,
        },
        "ventures": rows,
        "missing": missing,
        "limitations": [
            "Simulated outcomes are not actual historical performance and are not guarantees.",
            "No budget, allocation, purchase, transfer or financial commitment is changed.",
            "Ventures without complete actual data or an explicit FX rate are excluded, not estimated.",
        ],
        "requires_human_authorization": True,
        "applied": False,
    }


def recommendation_from_metrics(
    snapshot: Mapping[str, Any],
    *,
    allocation: Mapping[str, Any],
    risks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metrics = venture_metrics(snapshot)
    t = _map(metrics.get("technical"))
    lifecycle = str(snapshot.get("lifecycle_status") or "")
    revenue = _num(t.get("revenue"))
    margin = _num(t.get("operating_margin"))
    growth = _num(t.get("growth"))
    runway = _num(t.get("runway"))
    defects = _num(t.get("open_defects"))
    high_risks = [row for row in risks if row.get("severity") in {"high", "critical"}]
    if lifecycle in {"WINDING_DOWN", "ARCHIVED"}:
        kind = "consider_winding_down"
        action = "Complete wind-down review; no records are deleted or closed automatically."
    elif defects is not None and defects >= 2:
        kind = "prioritize_product"
        action = "Prioritize verified defect resolution before further acquisition spend."
    elif runway is not None and runway < 1.5:
        kind = "reduce_costs"
        action = "Prepare a cost-reduction and funding plan for human approval."
    elif marshal_high := bool(high_risks):
        kind = "restructure" if margin is not None and margin < 0 else "maintain_allocation"
        action = "Review high-severity risks and agree a mitigation owner."
        _ = marshal_high
    elif growth is not None and growth > 0.15 and (margin is None or margin >= 0):
        kind = "expand_acquisition"
        action = "Test additional acquisition within an approved budget; do not launch a paid campaign automatically."
    elif revenue is None and lifecycle in {"OPPORTUNITY_DISCOVERY", "VALIDATION"}:
        kind = "increase_validation"
        action = (
            "Collect evidence and complete validation before treating the venture as operating."
        )
    elif lifecycle == "PAUSED":
        kind = "continue"
        action = (
            "Decide whether to resume or continue the pause after reviewing the recorded evidence."
        )
    else:
        kind = "maintain_allocation"
        action = "Maintain the current approved allocation while monitoring actual results."
    evidence = list(allocation.get("evidence") or [])
    evidence.extend(f"{row.get('category')}: {row.get('observed')}" for row in high_risks[:3])
    return {
        "id": f"rec-{snapshot.get('venture_id')}",
        "venture_id": snapshot.get("venture_id"),
        "venture_name": snapshot.get("name"),
        "kind": kind,
        "recommended_action": action,
        "decision_context": f"Portfolio review for {snapshot.get('name')} in lifecycle {lifecycle}.",
        "evidence": evidence or ["No recorded actual evidence supports a stronger action."],
        "financial_implications": allocation.get("estimated_costs", []),
        "operational_implications": allocation.get("expected_benefits", []),
        "risks": allocation.get("risks", []),
        "alternatives": allocation.get("alternatives", []),
        "assumptions": allocation.get("assumptions", []),
        "expected_outcomes": allocation.get("expected_benefits", []),
        "confidence": allocation.get("confidence", "low"),
        "approval_required": True,
        "status": "proposed",
        "kind_label": "recommendation",
        "created_at": snapshot.get("fetched_at", ""),
        "applied": False,
        "note": "An AI-generated recommendation is not a portfolio decision and changes nothing.",
    }


__all__ = [
    "DIMENSIONS",
    "allocation_recommendation",
    "build_scenario",
    "correlated_risks",
    "evaluate_venture",
    "rank_evaluations",
    "recommendation_from_metrics",
    "venture_metrics",
    "venture_risks",
]
