"""Phase 39 portfolio records, validation and metric helpers.

A portfolio stores references to Phase 38 ventures, never a second copy of their
ledgers. Money therefore remains an exact decimal string in the source record;
converted portfolio totals are only produced when an explicit FX rate exists.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

PORTFOLIO_STATUSES: tuple[str, ...] = ("active", "paused", "archived")
LIFECYCLE_STATUSES: tuple[str, ...] = (
    "OPPORTUNITY_DISCOVERY",
    "VALIDATION",
    "PRODUCT_DEVELOPMENT",
    "INTERNAL_TESTING",
    "READY_FOR_LAUNCH",
    "CUSTOMER_ACQUISITION",
    "OPERATING",
    "UNDER_REVIEW",
    "PAUSED",
    "RESTRUCTURING",
    "WINDING_DOWN",
    "ARCHIVED",
)
MANAGEMENT_LIFECYCLES = {
    "UNDER_REVIEW",
    "PAUSED",
    "RESTRUCTURING",
    "WINDING_DOWN",
}
ROLES: tuple[str, ...] = ("viewer", "analyst", "approver", "admin")
ROLE_LEVEL = {"viewer": 1, "analyst": 2, "approver": 3, "admin": 4}
RISK_CATEGORIES: tuple[str, ...] = (
    "financial",
    "liquidity",
    "revenue_concentration",
    "customer_concentration",
    "market",
    "operational",
    "product_delivery",
    "technology_dependency",
    "vendor_dependency",
    "regulatory",
    "security",
    "execution",
)
SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
ALLOCATION_ACTIONS: tuple[str, ...] = (
    "increase",
    "maintain",
    "reduce",
    "delay",
    "pause",
    "validate",
    "reallocate",
)
SCENARIO_TYPES: tuple[str, ...] = (
    "new_venture",
    "launch_delay",
    "development_spend",
    "reduce_opex",
    "acquisition_strategy",
    "marketing_budget",
    "pause_venture",
    "scale_venture",
    "revenue_shortfall",
    "infrastructure_cost",
    "custom",
)
DECISION_KINDS: tuple[str, ...] = (
    "continue",
    "increase_validation",
    "prioritize_product",
    "expand_acquisition",
    "reduce_costs",
    "maintain_allocation",
    "pause_development",
    "restructure",
    "consider_winding_down",
    "explore_opportunity",
)
KNOWLEDGE_KINDS: tuple[str, ...] = (
    "verified_fact",
    "historical_observation",
    "interpretation",
    "hypothesis",
    "recommendation",
)
REPORT_PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly", "quarterly", "custom")

DEFAULT_EVALUATION_POLICIES: dict[str, dict[str, Any]] = {
    "balanced": {
        "name": "balanced",
        "description": "Equal weight across financial, market, operational and strategic evidence.",
        "weights": {"financial": 0.30, "market": 0.25, "operational": 0.25, "strategic": 0.20},
        "min_coverage": 0.50,
    },
    "growth": {
        "name": "growth",
        "description": "Favours demonstrated acquisition and revenue growth over current margin.",
        "weights": {"financial": 0.25, "market": 0.35, "operational": 0.20, "strategic": 0.20},
        "min_coverage": 0.50,
    },
    "capital_efficiency": {
        "name": "capital_efficiency",
        "description": "Favours margin, runway and budget discipline over growth.",
        "weights": {"financial": 0.45, "market": 0.15, "operational": 0.25, "strategic": 0.15},
        "min_coverage": 0.50,
    },
}

DEFAULT_MONITORING: dict[str, Any] = {
    "interval_minutes": 60,
    "stale_after_hours": 24,
    "cash_runway_months": 3.0,
    "revenue_concentration": 0.60,
    "customer_churn": 0.10,
    "budget_overrun": 0.10,
    "product_delay_days": 14,
    "sales_conversion": 0.10,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def text(value: Any, label: str, limit: int, *, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{label} is required.")
    if len(result) > limit:
        raise ValueError(f"{label} cannot exceed {limit} characters.")
    return result


def number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    default: float | None = None,
) -> float:
    if value is None or value == "":
        if default is not None:
            return float(default)
        raise ValueError(f"{label} is required.")
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number.")
    if minimum is not None and result < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if maximum is not None and result > maximum:
        raise ValueError(f"{label} must be at most {maximum}.")
    return result


def parse_day(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date.") from exc


def optional_day(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    return parse_day(value, label)


def decimal_text(value: Any, label: str = "amount") -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{label} is required.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal number.") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be a decimal number.")
    return result


def money(value: Any) -> float:
    return round(float(Decimal(str(value or "0"))), 2)


def currency(value: Any, label: str = "currency") -> str:
    result = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise ValueError(f"{label} must be a three-letter currency code.")
    return result


def metric(
    value: Any,
    *,
    kind: str = "actual",
    method: str = "",
    source: str = "",
    period: str = "",
    note: str = "",
    reason: str = "",
) -> dict[str, Any]:
    if value is None:
        return {
            "available": False,
            "value": None,
            "kind": kind,
            "reason": reason or "the recorded source data is insufficient",
            "method": method,
            "source": source,
            "period": period,
            "note": note,
        }
    return {
        "available": True,
        "value": value,
        "kind": kind,
        "reason": "",
        "method": method,
        "source": source,
        "period": period,
        "note": note,
    }


def unavailable(reason: str, *, kind: str = "actual", source: str = "") -> dict[str, Any]:
    return metric(None, kind=kind, reason=reason, source=source)


def bounded_rows(value: Any, label: str, limit: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list.")
    if len(value) > limit:
        raise ValueError(f"{label} cannot contain more than {limit} rows.")
    return list(value)


__all__ = [
    "ALLOCATION_ACTIONS",
    "DECISION_KINDS",
    "DEFAULT_EVALUATION_POLICIES",
    "DEFAULT_MONITORING",
    "KNOWLEDGE_KINDS",
    "LIFECYCLE_STATUSES",
    "MANAGEMENT_LIFECYCLES",
    "PORTFOLIO_STATUSES",
    "REPORT_PERIODS",
    "RISK_CATEGORIES",
    "ROLE_LEVEL",
    "ROLES",
    "SCENARIO_TYPES",
    "SEVERITIES",
    "bounded_rows",
    "currency",
    "decimal_text",
    "metric",
    "money",
    "new_id",
    "number",
    "optional_day",
    "parse_day",
    "text",
    "unavailable",
    "utcnow",
]
