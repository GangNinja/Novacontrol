"""Phase 37 customer acquisition, sales and support records.

The customer lifecycle is stored as one durable workspace per Phase 36 product.
Leads, customers, deals, communication records, tickets, feedback, onboarding
and success records travel together so an operator can trace one customer's
story without a second CRM database or a second orchestration stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class CustomerCategory(StrEnum):
    BUSINESS = "business"
    INDIVIDUAL = "individual"


class LeadStatus(StrEnum):
    NEW = "NEW"
    RESEARCHING = "RESEARCHING"
    QUALIFIED = "QUALIFIED"
    UNQUALIFIED = "UNQUALIFIED"
    CONTACT_PENDING_APPROVAL = "CONTACT_PENDING_APPROVAL"
    IN_CONVERSATION = "IN_CONVERSATION"
    CONVERTED = "CONVERTED"
    LOST = "LOST"
    ARCHIVED = "ARCHIVED"


class ConsentStatus(StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    NOT_REQUIRED = "not_required"


class CommunicationChannel(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    MEETING = "meeting"
    PRODUCT_DEMO = "product_demo"
    ONBOARDING = "onboarding"
    OTHER = "other"


class CommunicationStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketCategory(StrEnum):
    TECHNICAL = "technical_issue"
    ACCOUNT = "account_issue"
    BILLING = "billing_inquiry"
    USAGE = "product_usage"
    FEATURE_REQUEST = "feature_request"
    BUG = "bug_report"
    GENERAL = "general_inquiry"


class FeedbackKind(StrEnum):
    FEATURE_REQUEST = "feature_request"
    BUG_REPORT = "bug_report"
    COMPLAINT = "complaint"
    SATISFACTION = "satisfaction"
    QUESTION = "common_question"
    USAGE_DIFFICULTY = "usage_difficulty"
    CHURN_REASON = "churn_reason"
    IMPROVEMENT = "improvement_suggestion"


class ResearchVerification(StrEnum):
    PUBLIC_RESEARCH = "public_research"
    DIRECT_VALIDATION = "direct_validation"
    OPERATOR_SUPPLIED = "operator_supplied"
    ASSUMPTION = "assumption"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class OnboardingItemStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    BLOCKED = "blocked"


DEFAULT_PIPELINE_STAGES: tuple[dict[str, Any], ...] = (
    {"id": "lead", "name": "Lead", "order": 1},
    {"id": "qualified", "name": "Qualified", "order": 2},
    {"id": "discovery", "name": "Discovery", "order": 3},
    {"id": "demonstration", "name": "Demonstration", "order": 4},
    {"id": "proposal", "name": "Proposal", "order": 5},
    {"id": "negotiation", "name": "Negotiation", "order": 6},
    {"id": "closed_won", "name": "Closed Won", "order": 7, "terminal": True, "outcome": "won"},
    {"id": "closed_lost", "name": "Closed Lost", "order": 8, "terminal": True, "outcome": "lost"},
)

DEFAULT_QUALIFICATION_CRITERIA: tuple[dict[str, Any], ...] = (
    {"id": "product_fit", "name": "Product fit", "weight": 0.20, "required": True},
    {"id": "problem_relevance", "name": "Problem relevance", "weight": 0.15, "required": True},
    {"id": "budget_compatibility", "name": "Budget compatibility", "weight": 0.15, "required": False},
    {"id": "purchase_intent", "name": "Purchase intent", "weight": 0.15, "required": False},
    {"id": "decision_authority", "name": "Decision-making authority", "weight": 0.10, "required": False},
    {"id": "timeline", "name": "Timeline", "weight": 0.10, "required": False},
    {"id": "engagement", "name": "Engagement history", "weight": 0.15, "required": False},
)

LEAD_SOURCES: tuple[str, ...] = (
    "website_inquiry",
    "voluntary_registration",
    "waitlist",
    "approved_referral",
    "public_directory",
    "manual_import",
    "approved_integration",
    "event",
    "operator_supplied",
)

TICKET_CATEGORIES: tuple[str, ...] = tuple(row.value for row in TicketCategory)

SENSITIVE_TICKET_CATEGORIES = {
    TicketCategory.ACCOUNT.value,
    TicketCategory.BILLING.value,
}


@dataclass
class CustomerWorkspace:
    """One product's isolated customer, sales and support workspace."""

    product_id: str
    product_snapshot: dict[str, Any]
    id: str = ""
    status: str = "active"
    pipeline_stages: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(row) for row in DEFAULT_PIPELINE_STAGES]
    )
    qualification_criteria: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(row) for row in DEFAULT_QUALIFICATION_CRITERIA]
    )
    automation_policy: dict[str, Any] = field(default_factory=dict)
    segments: list[dict[str, Any]] = field(default_factory=list)
    icps: list[dict[str, Any]] = field(default_factory=list)
    research: list[dict[str, Any]] = field(default_factory=list)
    customers: list[dict[str, Any]] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)
    interactions: list[dict[str, Any]] = field(default_factory=list)
    communications: list[dict[str, Any]] = field(default_factory=list)
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    onboarding: list[dict[str, Any]] = field(default_factory=list)
    success: list[dict[str, Any]] = field(default_factory=list)
    sales_assists: list[dict[str, Any]] = field(default_factory=list)
    acquisition_costs: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not str(self.product_id or "").strip():
            raise ValueError("A customer workspace needs a product id.")
        self.product_id = str(self.product_id).strip()
        self.id = self.id or f"customers-{self.product_id}"
        if not self.product_snapshot:
            raise ValueError("A customer workspace needs a recorded product snapshot.")
        if not self.pipeline_stages:
            self.pipeline_stages = [dict(row) for row in DEFAULT_PIPELINE_STAGES]
        if not self.qualification_criteria:
            self.qualification_criteria = [
                dict(row) for row in DEFAULT_QUALIFICATION_CRITERIA
            ]

    # -- lookups -------------------------------------------------------------

    def _find(self, rows: list[dict[str, Any]], record_id: str) -> dict[str, Any] | None:
        key = str(record_id or "").strip()
        return next((row for row in rows if str(row.get("id")) == key), None)

    def segment(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.segments, record_id)

    def icp(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.icps, record_id)

    def customer(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.customers, record_id)

    def lead(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.leads, record_id)

    def deal(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.deals, record_id)

    def communication(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.communications, record_id)

    def ticket(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.tickets, record_id)

    def onboarding_record(self, record_id: str) -> dict[str, Any] | None:
        return self._find(self.onboarding, record_id)

    def customer_success(self, record_id: str) -> dict[str, Any] | None:
        key = str(record_id or "").strip()
        return next(
            (
                row
                for row in self.success
                if str(row.get("customer_id")) == key
            ),
            None,
        )

    def stage(self, stage_id: str) -> dict[str, Any] | None:
        return self._find(self.pipeline_stages, stage_id)

    def summary(self) -> dict[str, Any]:
        open_tickets = [
            row
            for row in self.tickets
            if row.get("status") not in {TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value}
        ]
        open_deals = [
            row
            for row in self.deals
            if row.get("stage") not in {"closed_won", "closed_lost"}
        ]
        return {
            "id": self.id,
            "product_id": self.product_id,
            "product_name": self.product_snapshot.get("name", ""),
            "product_version": self.product_snapshot.get("version", ""),
            "launch_status": self.product_snapshot.get("launch_status", ""),
            "launch_authorized": bool(self.product_snapshot.get("launch_authorized")),
            "segments": len(self.segments),
            "icps": len(self.icps),
            "customers": len(self.customers),
            "leads": len(self.leads),
            "qualified_leads": sum(
                1
                for row in self.leads
                if row.get("status")
                in {
                    LeadStatus.QUALIFIED.value,
                    LeadStatus.CONTACT_PENDING_APPROVAL.value,
                    LeadStatus.IN_CONVERSATION.value,
                    LeadStatus.CONVERTED.value,
                }
            ),
            "deals": len(self.deals),
            "open_deals": len(open_deals),
            "tickets": len(self.tickets),
            "open_tickets": len(open_tickets),
            "feedback": len(self.feedback),
            "communications": len(self.communications),
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "product_snapshot": dict(self.product_snapshot),
            "status": self.status,
            "pipeline_stages": list(self.pipeline_stages),
            "qualification_criteria": list(self.qualification_criteria),
            "automation_policy": dict(self.automation_policy),
            "segments": list(self.segments),
            "icps": list(self.icps),
            "research": list(self.research),
            "customers": list(self.customers),
            "leads": list(self.leads),
            "deals": list(self.deals),
            "interactions": list(self.interactions),
            "communications": list(self.communications),
            "knowledge": list(self.knowledge),
            "tickets": list(self.tickets),
            "feedback": list(self.feedback),
            "onboarding": list(self.onboarding),
            "success": list(self.success),
            "sales_assists": list(self.sales_assists),
            "acquisition_costs": list(self.acquisition_costs),
            "approvals": list(self.approvals),
            "events": list(self.events),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CustomerWorkspace:
        def rows(key: str) -> list[dict[str, Any]]:
            raw = payload.get(key)
            if not isinstance(raw, (list, tuple)):
                return []
            return [dict(row) for row in raw if isinstance(row, Mapping)]

        stages = rows("pipeline_stages")
        criteria = rows("qualification_criteria")
        workspace = cls(
            id=str(payload.get("id", "")),
            product_id=str(payload.get("product_id", "")),
            product_snapshot=dict(payload.get("product_snapshot") or {}),
            status=str(payload.get("status", "active")),
            pipeline_stages=stages or [dict(row) for row in DEFAULT_PIPELINE_STAGES],
            qualification_criteria=criteria
            or [dict(row) for row in DEFAULT_QUALIFICATION_CRITERIA],
            automation_policy=dict(payload.get("automation_policy") or {}),
            segments=rows("segments"),
            icps=rows("icps"),
            research=rows("research"),
            customers=rows("customers"),
            leads=rows("leads"),
            deals=rows("deals"),
            interactions=rows("interactions"),
            communications=rows("communications"),
            knowledge=rows("knowledge"),
            tickets=rows("tickets"),
            feedback=rows("feedback"),
            onboarding=rows("onboarding"),
            success=rows("success"),
            sales_assists=rows("sales_assists"),
            acquisition_costs=rows("acquisition_costs"),
            approvals=rows("approvals"),
            events=rows("events"),
            created_at=_dt(payload.get("created_at")) or utcnow(),
            updated_at=_dt(payload.get("updated_at")) or utcnow(),
        )
        return workspace


__all__ = [
    "CommunicationChannel",
    "CommunicationStatus",
    "ConsentStatus",
    "CustomerCategory",
    "CustomerWorkspace",
    "DEFAULT_PIPELINE_STAGES",
    "DEFAULT_QUALIFICATION_CRITERIA",
    "FeedbackKind",
    "LEAD_SOURCES",
    "LeadStatus",
    "OnboardingItemStatus",
    "ResearchVerification",
    "RiskLevel",
    "SENSITIVE_TICKET_CATEGORIES",
    "TICKET_CATEGORIES",
    "TicketCategory",
    "TicketStatus",
    "new_id",
    "utcnow",
]
