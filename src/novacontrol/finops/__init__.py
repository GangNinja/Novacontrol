"""Phase 38 — Business Operations & Financial Intelligence."""

from novacontrol.finops.agent import FinancialIntelligenceAgent, build_finance_agent
from novacontrol.finops.analytics import (
    NOT_CALCULABLE,
    expense_summary,
    metric,
    operations_dashboard,
    profitability,
    revenue_summary,
    unavailable,
    working_capital,
)
from novacontrol.finops.errors import (
    FinOpsAuthorizationError,
    FinOpsBudgetExceeded,
    FinOpsConflict,
    FinOpsError,
    FinOpsNotFound,
    FinOpsRecordNotFound,
    FinOpsValidationError,
    IdempotencyConflict,
    InsufficientFinancialData,
    PeriodClosed,
    VentureNotFound,
    error_payload,
)
from novacontrol.finops.forecasting import build_forecast, compare_scenarios
from novacontrol.finops.ledger import (
    balance_sheet,
    cash_flow_statement,
    income_statement,
    trial_balance,
)
from novacontrol.finops.models import (
    BUSINESS_MODELS,
    EXPENSE_CATEGORIES,
    REVENUE_STAGES,
    ROLES,
)
from novacontrol.finops.runtime import FinanceModule
from novacontrol.finops.service import NAMESPACE, SCHEMA_VERSION, FinOpsService

__all__ = [
    "BUSINESS_MODELS",
    "EXPENSE_CATEGORIES",
    "FinanceModule",
    "FinOpsAuthorizationError",
    "FinOpsBudgetExceeded",
    "FinOpsConflict",
    "FinOpsError",
    "FinOpsNotFound",
    "FinOpsRecordNotFound",
    "FinOpsService",
    "FinOpsValidationError",
    "FinancialIntelligenceAgent",
    "IdempotencyConflict",
    "InsufficientFinancialData",
    "NAMESPACE",
    "NOT_CALCULABLE",
    "PeriodClosed",
    "REVENUE_STAGES",
    "ROLES",
    "SCHEMA_VERSION",
    "VentureNotFound",
    "balance_sheet",
    "build_finance_agent",
    "build_forecast",
    "cash_flow_statement",
    "compare_scenarios",
    "error_payload",
    "expense_summary",
    "income_statement",
    "metric",
    "operations_dashboard",
    "profitability",
    "revenue_summary",
    "trial_balance",
    "unavailable",
    "working_capital",
]
