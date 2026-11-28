"""Phase 37 — Customer Acquisition, Sales & Support."""

from novacontrol.customer.agent import CustomerGrowthAgent, build_customer_agent
from novacontrol.customer.analytics import analytics_snapshot
from novacontrol.customer.errors import (
    CommunicationNotConfigured,
    ConsentRequired,
    CustomerBudgetExceeded,
    CustomerConflict,
    CustomerError,
    CustomerNotFound,
    CustomerRecordNotFound,
    CustomerValidationError,
    ProductNotLaunched,
    error_payload,
)
from novacontrol.customer.models import (
    CommunicationChannel,
    CommunicationStatus,
    ConsentStatus,
    CustomerCategory,
    CustomerWorkspace,
    DEFAULT_PIPELINE_STAGES,
    DEFAULT_QUALIFICATION_CRITERIA,
    FeedbackKind,
    LEAD_SOURCES,
    LeadStatus,
    OnboardingItemStatus,
    ResearchVerification,
    RiskLevel,
    TICKET_CATEGORIES,
    TicketCategory,
    TicketStatus,
)
from novacontrol.customer.qualification import qualify_lead
from novacontrol.customer.runtime import CustomerModule
from novacontrol.customer.sales import sales_brief
from novacontrol.customer.service import CustomerService, NAMESPACE, SCHEMA_VERSION
from novacontrol.customer.support import classify_issue, support_assist

__all__ = [
    "CommunicationChannel",
    "CommunicationNotConfigured",
    "CommunicationStatus",
    "ConsentRequired",
    "ConsentStatus",
    "CustomerBudgetExceeded",
    "CustomerCategory",
    "CustomerConflict",
    "CustomerError",
    "CustomerGrowthAgent",
    "CustomerModule",
    "CustomerNotFound",
    "CustomerRecordNotFound",
    "CustomerService",
    "CustomerValidationError",
    "CustomerWorkspace",
    "DEFAULT_PIPELINE_STAGES",
    "DEFAULT_QUALIFICATION_CRITERIA",
    "FeedbackKind",
    "LEAD_SOURCES",
    "LeadStatus",
    "NAMESPACE",
    "OnboardingItemStatus",
    "ProductNotLaunched",
    "ResearchVerification",
    "RiskLevel",
    "SCHEMA_VERSION",
    "TICKET_CATEGORIES",
    "TicketCategory",
    "TicketStatus",
    "analytics_snapshot",
    "build_customer_agent",
    "classify_issue",
    "error_payload",
    "qualify_lead",
    "sales_brief",
    "support_assist",
]
