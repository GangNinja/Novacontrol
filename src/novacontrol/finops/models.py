"""Phase 38 business-operations and financial-intelligence records.

The domain deliberately keeps one durable document per venture. Amounts are
stored as fixed-point decimal strings and only converted to floats at the API
edge, so a cent is never lost to binary floating point. The chart of accounts is
venture-local: two ventures in different currencies never share a ledger.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from uuid import uuid4

CENT = Decimal("0.01")

BUSINESS_MODELS: tuple[str, ...] = (
    "saas",
    "subscription",
    "digital_products",
    "services",
    "ecommerce",
    "project_based",
    "other",
)

OPERATING_STATUSES: tuple[str, ...] = ("planning", "active", "paused", "archived")

REVENUE_STAGES: tuple[str, ...] = ("booked", "invoiced", "recognized", "collected")
REVENUE_SOURCES: tuple[str, ...] = (
    "phase37_customer",
    "operator_entry",
    "invoice",
    "bank_statement",
    "approved_integration",
    "import",
)
EXPENSE_STATUSES: tuple[str, ...] = ("actual", "estimated", "committed", "projected")
PAYMENT_STATUSES: tuple[str, ...] = ("unpaid", "partial", "paid", "not_applicable")
EXPENSE_CATEGORIES: tuple[str, ...] = (
    "infrastructure",
    "api_model_usage",
    "software_subscriptions",
    "employee_contractor",
    "marketing",
    "sales",
    "hosting",
    "payment_processing",
    "product_development",
    "administrative",
    "taxes",
    "other",
)
EXPENSE_CATEGORY_ACCOUNTS: dict[str, str] = {
    "infrastructure": "6300",
    "api_model_usage": "6310",
    "software_subscriptions": "6320",
    "employee_contractor": "6200",
    "marketing": "6100",
    "sales": "6110",
    "hosting": "6330",
    "payment_processing": "6400",
    "product_development": "6500",
    "administrative": "6600",
    "taxes": "6700",
    "other": "6900",
}

ACCOUNT_TYPES: tuple[str, ...] = ("asset", "liability", "equity", "revenue", "expense")
ROLES: tuple[str, ...] = ("viewer", "analyst", "approver", "admin")
ROLE_LEVEL = {"viewer": 1, "analyst": 2, "approver": 3, "admin": 4}

DEFAULT_ACCOUNTS: tuple[dict[str, Any], ...] = (
    {"code": "1000", "name": "Cash and cash equivalents", "type": "asset", "normal_side": "debit", "category": "cash", "current": True},
    {"code": "1100", "name": "Accounts receivable", "type": "asset", "normal_side": "debit", "category": "accounts_receivable", "current": True},
    {"code": "1200", "name": "Inventory", "type": "asset", "normal_side": "debit", "category": "inventory", "current": True},
    {"code": "1300", "name": "Prepaid expenses", "type": "asset", "normal_side": "debit", "category": "prepaid", "current": True},
    {"code": "1500", "name": "Equipment and capitalised development", "type": "asset", "normal_side": "debit", "category": "noncurrent_asset", "current": False},
    {"code": "2000", "name": "Accounts payable", "type": "liability", "normal_side": "credit", "category": "accounts_payable", "current": True},
    {"code": "2100", "name": "Deferred revenue", "type": "liability", "normal_side": "credit", "category": "deferred_revenue", "current": True},
    {"code": "2200", "name": "Sales tax payable", "type": "liability", "normal_side": "credit", "category": "tax_payable", "current": True},
    {"code": "2500", "name": "Long-term liabilities", "type": "liability", "normal_side": "credit", "category": "noncurrent_liability", "current": False},
    {"code": "3000", "name": "Owner equity", "type": "equity", "normal_side": "credit", "category": "equity", "current": False},
    {"code": "3100", "name": "Retained earnings", "type": "equity", "normal_side": "credit", "category": "retained_earnings", "current": False},
    {"code": "4000", "name": "Revenue", "type": "revenue", "normal_side": "credit", "category": "revenue", "current": True},
    {"code": "4100", "name": "Refunds and discounts", "type": "revenue", "normal_side": "debit", "category": "contra_revenue", "current": True},
    {"code": "5000", "name": "Cost of goods sold", "type": "expense", "normal_side": "debit", "category": "cogs", "current": True},
    {"code": "6000", "name": "Operating expenses", "type": "expense", "normal_side": "debit", "category": "operating_expense", "current": True},
    {"code": "6100", "name": "Marketing", "type": "expense", "normal_side": "debit", "category": "marketing", "current": True},
    {"code": "6110", "name": "Sales", "type": "expense", "normal_side": "debit", "category": "sales", "current": True},
    {"code": "6200", "name": "Employee and contractor", "type": "expense", "normal_side": "debit", "category": "payroll", "current": True},
    {"code": "6300", "name": "Infrastructure", "type": "expense", "normal_side": "debit", "category": "infrastructure", "current": True},
    {"code": "6310", "name": "API and model usage", "type": "expense", "normal_side": "debit", "category": "api_model_usage", "current": True},
    {"code": "6320", "name": "Software subscriptions", "type": "expense", "normal_side": "debit", "category": "software", "current": True},
    {"code": "6330", "name": "Hosting", "type": "expense", "normal_side": "debit", "category": "hosting", "current": True},
    {"code": "6400", "name": "Payment processing fees", "type": "expense", "normal_side": "debit", "category": "payment_fees", "current": True},
    {"code": "6500", "name": "Product development", "type": "expense", "normal_side": "debit", "category": "development", "current": True},
    {"code": "6600", "name": "Administrative", "type": "expense", "normal_side": "debit", "category": "administrative", "current": True},
    {"code": "6700", "name": "Taxes", "type": "expense", "normal_side": "debit", "category": "taxes", "current": True},
    {"code": "6900", "name": "Other operating expense", "type": "expense", "normal_side": "debit", "category": "other", "current": True},
)

CASH_ACCOUNT = "1000"
AR_ACCOUNT = "1100"
INVENTORY_ACCOUNT = "1200"
AP_ACCOUNT = "2000"
DEFERRED_REVENUE_ACCOUNT = "2100"
TAX_PAYABLE_ACCOUNT = "2200"
REVENUE_ACCOUNT = "4000"
REFUND_ACCOUNT = "4100"
COGS_ACCOUNT = "5000"


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def parse_money(value: Any, label: str = "amount", *, allow_negative: bool = False, allow_zero: bool = False) -> Decimal:
    """Parse a finite amount to exact cents; bools and NaN/Infinity are refused."""
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{label} is required.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number.") from exc
    if not amount.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount < 0 and not allow_negative:
        raise ValueError(f"{label} cannot be negative.")
    if amount == 0 and not allow_zero:
        raise ValueError(f"{label} must be greater than zero.")
    return amount


def optional_money(value: Any, label: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    return parse_money(value, label, allow_zero=True)


def money_text(value: Decimal | str | float | int) -> str:
    """A JSON-safe exact decimal string with two fractional digits."""
    return str(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def money_value(value: Any) -> float:
    return float(Decimal(str(value or "0")).quantize(CENT, rounding=ROUND_HALF_UP))


def parse_date(value: Any, label: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD).") from exc


def optional_date(value: Any, label: str = "date") -> date | None:
    if value in (None, ""):
        return None
    return parse_date(value, label)


def iso_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()


def text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} is limited to {maximum} characters.")
    return cleaned


def strings(value: Any, label: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        rows = value.replace("\r", "").split("\n")
    elif isinstance(value, (list, tuple)):
        rows = list(value)
    else:
        raise ValueError(f"{label} must be a list of strings.")
    return [str(row).strip() for row in rows if str(row).strip()]


def mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object.")
    return {str(key): item for key, item in value.items()}


def currency_code(value: Any, label: str = "currency") -> str:
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"{label} must be a three-letter ISO currency code.")
    return code


def fiscal_period(value: Any) -> dict[str, Any]:
    data = mapping(value, "fiscal_period")
    start_month = int(data.get("start_month", 1) or 1)
    if start_month < 1 or start_month > 12:
        raise ValueError("fiscal_period.start_month must be 1–12.")
    return {
        "start_month": start_month,
        "label": str(data.get("label") or "calendar year")[:80],
        "timezone": str(data.get("timezone") or "UTC")[:64],
    }


def default_accounts() -> list[dict[str, Any]]:
    return [
        {
            **account,
            "id": f"acct-{account['code']}",
            "active": True,
            "opening_balance": "0.00",
        }
        for account in DEFAULT_ACCOUNTS
    ]


def account_by_code(venture: Mapping[str, Any], code: str) -> dict[str, Any] | None:
    for account in venture.get("accounts", []):
        if str(account.get("code")) == str(code):
            return dict(account)
    return None


def new_venture(
    *,
    name: str,
    description: str,
    business_model: str,
    products_services: list[str],
    ownership: dict[str, Any],
    authorization: dict[str, Any],
    reporting_currency: str,
    revenue_model: Any,
    expense_structure: Any,
    objectives: list[str],
    linked_products: list[str],
    customer_refs: list[str],
    fiscal: dict[str, Any],
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    venture_id = new_id("ven")
    return {
        "schema_version": 1,
        "id": venture_id,
        "name": name,
        "description": description,
        "business_model": business_model,
        "products_services": products_services,
        "ownership": ownership,
        "authorization": {
            "owner": actor,
            "members": [{"actor": actor, "role": "admin", "added_at": now.isoformat(), "added_by": actor}],
            "external_permissions": dict(authorization.get("external_permissions") or {}),
            "payment_execution": False,
            "purchase_execution": False,
            "banking_integration": False,
            "note": "Phase 38 records and analyses financial information; it cannot move money, pay invoices or make purchases.",
        },
        "operating_status": "planning",
        "revenue_model": revenue_model,
        "expense_structure": expense_structure,
        "reporting_currency": reporting_currency,
        "fiscal_period": fiscal,
        "objectives": objectives,
        "linked_products": linked_products,
        "customer_refs": customer_refs,
        "accounts": default_accounts(),
        "journal_entries": [],
        "revenue_records": [],
        "expense_records": [],
        "operational_objectives": [],
        "operational_tasks": [],
        "milestones": [],
        "workflows": [],
        "operational_risks": [],
        "reconciliations": [],
        "periods": [],
        "budgets": [],
        "forecasts": [],
        "scenarios": [],
        "health_alerts": [],
        "health_history": [],
        "management_reports": [],
        "health_thresholds": {
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
        },
        "resource_limits": {
            "journal_entries": 5000,
            "revenue_records": 5000,
            "expense_records": 5000,
            "alerts": 500,
            "events": 500,
        },
        "events": [],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


__all__ = [
    "ACCOUNT_TYPES",
    "AP_ACCOUNT",
    "AR_ACCOUNT",
    "BUSINESS_MODELS",
    "CASH_ACCOUNT",
    "CENT",
    "COGS_ACCOUNT",
    "DEFAULT_ACCOUNTS",
    "DEFERRED_REVENUE_ACCOUNT",
    "EXPENSE_CATEGORIES",
    "EXPENSE_CATEGORY_ACCOUNTS",
    "EXPENSE_STATUSES",
    "INVENTORY_ACCOUNT",
    "OPERATING_STATUSES",
    "PAYMENT_STATUSES",
    "REFUND_ACCOUNT",
    "REVENUE_ACCOUNT",
    "REVENUE_SOURCES",
    "REVENUE_STAGES",
    "ROLES",
    "ROLE_LEVEL",
    "TAX_PAYABLE_ACCOUNT",
    "account_by_code",
    "currency_code",
    "default_accounts",
    "fiscal_period",
    "iso_date",
    "mapping",
    "money_text",
    "money_value",
    "new_id",
    "new_venture",
    "optional_date",
    "optional_money",
    "parse_date",
    "parse_money",
    "strings",
    "text",
    "utcnow",
]
