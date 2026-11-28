"""Phase 37 customer acquisition, sales and support service.

One durable workspace per launch-authorized Phase 36 product. The service reads
the product's own published information, uses the application's JSON state,
events, audit and memory seams, and never runs an external action other than the
explicitly injected communication sender.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from novacontrol.customer.analytics import analytics_snapshot
from novacontrol.customer.errors import (
    CommunicationNotConfigured,
    ConsentRequired,
    CustomerBudgetExceeded,
    CustomerConflict,
    CustomerNotFound,
    CustomerRecordNotFound,
    CustomerValidationError,
    ProductNotLaunched,
)
from novacontrol.customer.models import (
    CommunicationChannel,
    CommunicationStatus,
    ConsentStatus,
    CustomerCategory,
    CustomerWorkspace,
    FeedbackKind,
    LEAD_SOURCES,
    LeadStatus,
    OnboardingItemStatus,
    ResearchVerification,
    RiskLevel,
    TICKET_CATEGORIES,
    TicketCategory,
    TicketStatus,
    new_id,
    utcnow,
)
from novacontrol.customer.qualification import qualify_lead
from novacontrol.customer.sales import sales_brief
from novacontrol.customer.support import classify_issue, support_assist

SCHEMA_VERSION = 1
NAMESPACE = "customer_growth"

ProductReader = Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
Sender = Callable[[Mapping[str, Any]], Mapping[str, Any]]
MemoryCallback = Callable[..., Any]
KnowledgeCallback = Callable[..., Any]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [row.strip() for row in value.replace("\r", "").split("\n") if row.strip()]
    if isinstance(value, (list, tuple)):
        return [row for row in value if str(row).strip()]
    raise CustomerValidationError("Expected a list of values.")


def _as_strings(value: Any) -> list[str]:
    return [str(row).strip() for row in _as_list(value) if str(row).strip()]


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CustomerValidationError(f"{label} must be an object.")
    return dict(value)


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CustomerValidationError(f"{label} must be true or false.")
    return value


def _optional_bool(data: Mapping[str, Any], key: str) -> bool:
    raw = data.get(key)
    if raw is None:
        return False
    return _as_bool(raw, key)


def _bounded_text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    text = _text(value)
    if required and not text:
        raise CustomerValidationError(f"{label} is required.")
    if len(text) > maximum:
        raise CustomerValidationError(f"{label} is limited to {maximum} characters.")
    return text


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value) or utcnow().isoformat()


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return value
    return value


async def _await_maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class CustomerService:
    """The persisted customer-growth lifecycle service."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        products: ProductReader | None = None,
        sender: Sender | None = None,
        publish: Callable[[str, Mapping[str, Any]], Any] | None = None,
        audit: Callable[..., Any] | None = None,
        memory: MemoryCallback | None = None,
        knowledge: KnowledgeCallback | None = None,
        actor: str = "operator",
        max_products: int = 50,
        max_customers: int = 500,
        max_leads: int = 1000,
        max_deals: int = 500,
        max_tickets: int = 1000,
        max_communications: int = 1000,
        max_feedback: int = 1000,
        max_events: int = 500,
        max_messages_per_ticket: int = 200,
        max_communications_per_day: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._products = products
        self._sender = sender
        self._publish_callback = publish
        self._audit_callback = audit
        self._memory_callback = memory
        self._knowledge_callback = knowledge
        self.actor = actor
        self.max_products = max(1, int(max_products))
        self.max_customers = max(1, int(max_customers))
        self.max_leads = max(1, int(max_leads))
        self.max_deals = max(1, int(max_deals))
        self.max_tickets = max(1, int(max_tickets))
        self.max_communications = max(1, int(max_communications))
        self.max_feedback = max(1, int(max_feedback))
        self.max_events = max(20, int(max_events))
        self.max_messages_per_ticket = max(10, int(max_messages_per_ticket))
        self.max_communications_per_day = max(1, int(max_communications_per_day))
        self._clock = clock or utcnow
        self._workspaces: dict[str, CustomerWorkspace] = {}
        self._loaded = False
        self._load_errors: list[str] = []
        self._write_lock = threading.Lock()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        payload = self._store.read(NAMESPACE)
        rows = payload.get("workspaces") if isinstance(payload, Mapping) else None
        if not isinstance(rows, (list, tuple)):
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                workspace = CustomerWorkspace.from_dict(row)
            except Exception as exc:  # noqa: BLE001 - one corrupt record must not hide the rest
                self._load_errors.append(str(exc))
                continue
            self._workspaces[workspace.product_id] = workspace

    def _persist(self) -> None:
        if self._store is None:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._clock().isoformat(),
            "workspaces": [row.to_dict() for row in self._workspaces.values()],
        }
        with self._write_lock:
            self._store.write(NAMESPACE, payload)

    def _get(self, product_id: str) -> CustomerWorkspace:
        self._load()
        key = _text(product_id)
        workspace = self._workspaces.get(key)
        if workspace is None:
            raise CustomerNotFound(key)
        return workspace

    def _require_active(self, workspace: CustomerWorkspace) -> None:
        if workspace.status != "active":
            raise CustomerConflict("The customer workspace is not active.")
        if not workspace.product_snapshot.get("launch_authorized"):
            raise ProductNotLaunched(
                workspace.product_id, str(workspace.product_snapshot.get("launch_status", ""))
            )

    # -- recording -----------------------------------------------------------

    def _schedule(self, result: Any) -> None:
        if not inspect.isawaitable(result):
            return

        async def consume(awaitable: Any) -> None:
            try:
                await awaitable
            except Exception:  # noqa: BLE001 - optional integration
                pass

        try:
            asyncio.get_running_loop().create_task(consume(result))
        except RuntimeError:
            pass

    def _remember(self, workspace: CustomerWorkspace, event_type: str) -> None:
        if self._memory_callback is None:
            return
        try:
            result = self._memory_callback(
                key=f"customer:{workspace.product_id}:latest",
                value={
                    **workspace.summary(),
                    "last_event": event_type,
                },
                text=(
                    f"Customer workspace for {workspace.product_snapshot.get('name', '')}: "
                    f"{event_type}; {len(workspace.customers)} customer(s), "
                    f"{len(workspace.leads)} lead(s), {len(workspace.tickets)} ticket(s)."
                ),
                importance=0.65,
            )
            self._schedule(result)
        except Exception:  # noqa: BLE001 - memory must not roll back a record
            pass

    def _event(
        self,
        workspace: CustomerWorkspace,
        event_type: str,
        *,
        actor: str,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        row = {
            "id": new_id("cevt"),
            "type": event_type,
            "product_id": workspace.product_id,
            "at": now.isoformat(),
            "actor": actor,
            "detail": {str(key): value for key, value in (detail or {}).items()},
        }
        workspace.events.append(row)
        workspace.events = workspace.events[-self.max_events :]
        workspace.updated_at = now
        if self._publish_callback is not None:
            try:
                result = self._publish_callback(
                    f"customer.{event_type}",
                    {
                        "product_id": workspace.product_id,
                        "event_id": row["id"],
                        "status": workspace.status,
                    },
                )
                self._schedule(result)
            except Exception:  # noqa: BLE001 - notification must not roll back a record
                pass
        if self._audit_callback is not None and event_type in {
            "workspace_created",
            "customer_created",
            "lead_created",
            "lead_qualified",
            "lead_converted",
            "consent_updated",
            "deal_created",
            "deal_stage_changed",
            "communication_sent",
            "communication_blocked",
            "ticket_created",
            "ticket_escalated",
            "customer_erased",
        }:
            try:
                self._audit_callback(
                    task_id=workspace.product_id,
                    user_request=f"customer:{workspace.product_id}:{event_type}",
                    outcome=event_type,
                    source="customer_growth",
                    actions=[event_type],
                    failures=[],
                )
            except Exception:  # noqa: BLE001 - audit failure is surfaced by the audit layer
                pass
        if event_type in {
            "workspace_created",
            "lead_converted",
            "deal_stage_changed",
            "ticket_escalated",
            "customer_erased",
        }:
            self._remember(workspace, event_type)
        return row

    def _approval(
        self,
        workspace: CustomerWorkspace,
        kind: str,
        *,
        target_id: str,
        actor: str,
        note: str,
        approved: bool,
    ) -> dict[str, Any]:
        row = {
            "kind": kind,
            "target_id": target_id,
            "approved": approved,
            "actor": actor,
            "note": note,
            "at": self._clock().isoformat(),
            "automated_recommendation": False,
        }
        workspace.approvals.append(row)
        workspace.approvals = workspace.approvals[-200:]
        return row

    def _assert_no_secrets(self, value: Any) -> None:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
        # Keep this deliberately narrow: credential-shaped values belong in the
        # application's secret store, not in customer records or message bodies.
        if re.search(
            r"(?i)(api[_-]?key|secret|password|bearer)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}",
            text,
        ):
            raise CustomerValidationError(
                "Customer data contains a credential-shaped value; keep secrets in configuration."
            )

    def _check_capacity(self, workspace: CustomerWorkspace, collection: str, maximum: int) -> None:
        if len(getattr(workspace, collection)) >= maximum:
            raise CustomerBudgetExceeded(
                f"The {collection.replace('_', ' ')} budget ({maximum}) is exhausted."
            )

    # -- product snapshot ----------------------------------------------------

    async def _read_product(self, product_id: str) -> dict[str, Any]:
        if self._products is None:
            raise CustomerConflict("No Phase 36 product reader is configured.")
        try:
            detail = await _await_maybe(self._products(product_id))
        except Exception as exc:  # noqa: BLE001 - the Phase 36 refusal is the useful error
            raise CustomerConflict(f"Could not read the Phase 36 product: {exc}") from exc
        if not isinstance(detail, Mapping):
            raise CustomerConflict("The Phase 36 product reader returned no record.")
        return dict(detail)

    def _snapshot(self, product_id: str, detail: Mapping[str, Any]) -> dict[str, Any]:
        summary = dict(detail.get("summary") or {})
        prd = dict(detail.get("prd") or {})
        architecture = dict(detail.get("architecture") or {})
        release = dict((detail.get("releases") or [{}])[-1] or {})
        state = str(summary.get("state") or detail.get("state") or "")
        approval = dict(detail.get("launch_approval") or {})
        launch_authorized = state == "APPROVED_FOR_LAUNCH" and approval.get("approved") is True
        requirements = [
            row
            for row in (prd.get("requirements") or [])
            if isinstance(row, Mapping)
            and row.get("status") in {"verified", "implemented", "approved"}
        ]
        features = _as_strings(prd.get("core_features")) or [
            str(row.get("title", ""))
            for row in requirements
            if row.get("kind") in {"functional", "core_feature"} and row.get("title")
        ]
        exclusions = _as_strings(prd.get("explicit_exclusions"))
        missing = _as_strings(prd.get("missing_information"))
        opportunity = dict(detail.get("opportunity_snapshot") or {})
        financial = dict(opportunity.get("financial") or {})
        pricing = {
            key: financial[key]
            for key in ("price", "pricing", "currency", "unit", "assumptions")
            if key in financial
        }
        docs = []
        if release:
            for key in ("notes", "deployment_docs", "rollback_plan"):
                if release.get(key):
                    docs.append(
                        {
                            "kind": key,
                            "reference": f"release {release.get('version', '')}",
                        }
                    )
        return {
            "product_id": product_id,
            "name": str(summary.get("name") or detail.get("name") or ""),
            "version": str(release.get("version") or prd.get("version") or ""),
            "description": _text(prd.get("problem_statement") or detail.get("description")),
            "target_segments": _as_strings(prd.get("target_users")),
            "value_proposition": _text(prd.get("vision")) if prd.get("status") == "approved" else "",
            "pricing_assumptions": pricing,
            "features": features,
            "limitations": exclusions + missing,
            "approved_marketing_claims": [],
            "marketing_claims_note": (
                "Phase 36 records no separately approved marketing-claims field; "
                "none are inferred."
            ),
            "documentation": docs,
            "launch_status": state,
            "launch_approved": bool(approval.get("approved")),
            "launch_authorized": launch_authorized,
            "launch_approval": approval,
            "prd_version": prd.get("version"),
            "architecture_status": architecture.get("status", ""),
            "source": "Phase 36 ProductService.detail",
        }

    async def create_workspace(
        self, product_id: str, *, actor: str = "", refresh: bool = False
    ) -> dict[str, Any]:
        self._load()
        key = _text(product_id)
        if not key:
            raise CustomerValidationError("A product id is required.")
        existing = self._workspaces.get(key)
        if existing is not None and not refresh:
            return {**existing.summary(), "already_created": True}
        detail = await self._read_product(key)
        snapshot = self._snapshot(key, detail)
        if not snapshot["launch_authorized"]:
            if existing is not None:
                existing.product_snapshot = snapshot
                self._event(
                    existing,
                    "product_authorization_revoked",
                    actor=_text(actor) or self.actor,
                    detail={"launch_status": snapshot["launch_status"]},
                )
                self._persist()
            raise ProductNotLaunched(key, snapshot["launch_status"])
        if existing is None:
            if len(self._workspaces) >= self.max_products:
                raise CustomerBudgetExceeded(
                    f"The customer-workspace budget ({self.max_products}) is exhausted."
                )
            workspace = CustomerWorkspace(product_id=key, product_snapshot=snapshot)
            workspace.automation_policy = {
                "enabled": False,
                "channels": [],
                "max_recipients": 1,
                "daily_limit": self.max_communications_per_day,
                "consent_required": True,
                "note": "",
            }
            self._workspaces[key] = workspace
            actor_name = _text(actor) or self.actor
            self._event(
                workspace,
                "workspace_created",
                actor=actor_name,
                detail={"launch_status": snapshot["launch_status"]},
            )
            self._persist()
            return {**self.detail(key), "already_created": False}
        existing.product_snapshot = snapshot
        actor_name = _text(actor) or self.actor
        self._event(
            existing,
            "product_snapshot_refreshed",
            actor=actor_name,
            detail={"launch_status": snapshot["launch_status"]},
        )
        self._persist()
        return {**self.detail(key), "already_created": False}

    async def refresh_product(self, product_id: str, *, actor: str = "") -> dict[str, Any]:
        return await self.create_workspace(product_id, actor=actor, refresh=True)

    # -- reads ---------------------------------------------------------------

    def list_workspaces(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self._load()
        rows = sorted(self._workspaces.values(), key=lambda row: row.updated_at, reverse=True)
        total = len(rows)
        page = rows[max(0, offset) : max(0, offset) + max(1, min(200, limit))]
        return {
            "workspaces": [row.summary() for row in page],
            "total": total,
            "limit": max(1, min(200, limit)),
            "offset": max(0, offset),
        }

    def detail(self, product_id: str) -> dict[str, Any]:
        workspace = self._get(product_id)
        return {
            **workspace.to_dict(),
            "summary": workspace.summary(),
            "analytics": analytics_snapshot(workspace),
            "available_actions": self._available_actions(workspace),
            "decision_boundary": {
                "outbound_communication_requires_approval": True,
                "automation_policy_must_be_explicitly_enabled": True,
                "no_unsolicited_bulk_messaging": True,
                "no_payment_or_refund_execution": True,
                "no_external_launch_or_paid_campaign": True,
                "supported_capabilities": [
                    "customer_onboarding",
                    "customer_success",
                    "support_tickets",
                    "sales_pipeline",
                ],
            },
        }

    def history(self, product_id: str) -> dict[str, Any]:
        workspace = self._get(product_id)
        return {
            "product_id": product_id,
            "events": list(workspace.events),
            "approvals": list(workspace.approvals),
            "communication_history": list(workspace.communications),
            "ticket_history": [
                {
                    "ticket_id": row.get("id"),
                    "status": row.get("status"),
                    "history": list(row.get("history") or []),
                }
                for row in workspace.tickets
            ],
        }

    def _available_actions(self, workspace: CustomerWorkspace) -> dict[str, bool]:
        return {
            "add_segment": True,
            "add_icp": True,
            "add_research": True,
            "add_customer": True,
            "add_lead": True,
            "qualify_lead": bool(workspace.leads),
            "convert_lead": any(
                row.get("status")
                in {
                    LeadStatus.QUALIFIED.value,
                    LeadStatus.CONTACT_PENDING_APPROVAL.value,
                    LeadStatus.IN_CONVERSATION.value,
                }
                for row in workspace.leads
            ),
            "create_deal": bool(workspace.leads or workspace.customers),
            "draft_communication": bool(workspace.leads or workspace.customers),
            "send_communication": self._sender is not None,
            "create_ticket": bool(workspace.customers),
            "support_assist": bool(workspace.tickets),
            "start_onboarding": bool(workspace.customers),
            "refresh_success": bool(workspace.customers),
            "analytics": True,
        }

    def status(self) -> dict[str, Any]:
        self._load()
        tickets = sum(len(row.tickets) for row in self._workspaces.values())
        leads = sum(len(row.leads) for row in self._workspaces.values())
        customers = sum(len(row.customers) for row in self._workspaces.values())
        return {
            "service": "customer_growth",
            "namespace": NAMESPACE,
            "schema_version": SCHEMA_VERSION,
            "workspaces": len(self._workspaces),
            "products": len(self._workspaces),
            "leads": leads,
            "customers": customers,
            "tickets": tickets,
            "configured": {
                "product_reader": self._products is not None,
                "communication_sender": self._sender is not None,
                "memory": self._memory_callback is not None,
                "knowledge": self._knowledge_callback is not None,
            },
            "limits": {
                "max_products": self.max_products,
                "max_customers_per_product": self.max_customers,
                "max_leads_per_product": self.max_leads,
                "max_deals_per_product": self.max_deals,
                "max_tickets_per_product": self.max_tickets,
                "max_communications_per_product": self.max_communications,
                "max_feedback_per_product": self.max_feedback,
                "max_events_per_product": self.max_events,
                "max_messages_per_ticket": self.max_messages_per_ticket,
                "max_communications_per_day": self.max_communications_per_day,
            },
            "boundaries": {
                "external_communication_default": "disabled_without_configured_sender",
                "unsolicited_bulk_messaging": False,
                "payment_or_refund_execution": False,
                "paid_advertising": False,
                "contractual_commitments_by_agent": False,
            },
            "load_errors": list(self._load_errors),
        }

    # -- segmentation, ICP and research --------------------------------------

    def add_segment(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        name = _bounded_text(data.get("name"), "Segment name", 160, required=True)
        category = _text(data.get("category") or CustomerCategory.BUSINESS.value).lower()
        if category not in {row.value for row in CustomerCategory}:
            raise CustomerValidationError("Segment category must be business or individual.")
        evidence = _as_strings(data.get("evidence"))
        verified = _optional_bool(data, "verified")
        if verified and not evidence:
            raise CustomerValidationError("A verified segment needs recorded evidence.")
        row = {
            "id": new_id("segment"),
            "product_id": workspace.product_id,
            "name": name,
            "category": category,
            "industry": _bounded_text(data.get("industry"), "Industry", 120),
            "company_size": _bounded_text(data.get("company_size"), "Company size", 80),
            "geography": _bounded_text(data.get("geography"), "Geography", 120),
            "needs": _as_strings(data.get("needs")),
            "pain_points": _as_strings(data.get("pain_points")),
            "budget_indicators": _as_strings(data.get("budget_indicators")),
            "buying_intent": _bounded_text(data.get("buying_intent"), "Buying intent", 240),
            "use_cases": _as_strings(data.get("use_cases")),
            "decision_maker_roles": _as_strings(data.get("decision_maker_roles")),
            "evidence": evidence,
            "assumptions": _as_strings(data.get("assumptions")),
            "verified": verified,
            "verification_state": "verified" if verified else "inferred",
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.segments.append(row)
        self._event(workspace, "segment_added", actor=_text(actor) or self.actor, detail={"segment_id": row["id"]})
        self._persist()
        return row

    def add_icp(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        name = _bounded_text(data.get("name"), "ICP name", 160, required=True)
        segment_id = _text(data.get("segment_id"))
        if segment_id and workspace.segment(segment_id) is None:
            raise CustomerRecordNotFound("segment", segment_id)
        evidence = _as_strings(data.get("evidence"))
        verified = _optional_bool(data, "verified")
        if verified and not evidence:
            raise CustomerValidationError("A verified ICP needs recorded evidence.")
        criteria = data.get("criteria")
        if criteria is not None and not isinstance(criteria, (Mapping, list, tuple, str)):
            raise CustomerValidationError("ICP criteria must be an object or a list.")
        row = {
            "id": new_id("icp"),
            "product_id": workspace.product_id,
            "name": name,
            "segment_id": segment_id,
            "criteria": criteria if criteria is not None else {},
            "needs": _as_strings(data.get("needs")),
            "pain_points": _as_strings(data.get("pain_points")),
            "decision_roles": _as_strings(data.get("decision_roles")),
            "evidence": evidence,
            "assumptions": _as_strings(data.get("assumptions")),
            "verified": verified,
            "verification_state": "verified" if verified else "inferred",
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.icps.append(row)
        self._event(workspace, "icp_added", actor=_text(actor) or self.actor, detail={"icp_id": row["id"]})
        self._persist()
        return row

    def add_research(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        source_url = _bounded_text(data.get("source_url"), "Source URL", 500)
        source_reference = _bounded_text(data.get("source_reference"), "Source reference", 500)
        if not source_url and not source_reference:
            raise CustomerValidationError("Research needs a source URL or reference.")
        verification = _text(data.get("verification_status") or ResearchVerification.PUBLIC_RESEARCH.value).lower()
        allowed = {row.value for row in ResearchVerification}
        if verification not in allowed:
            raise CustomerValidationError(f"verification_status must be one of: {', '.join(sorted(allowed))}")
        summary = _bounded_text(data.get("summary"), "Research summary", 4000, required=True)
        confidence = data.get("confidence", 0.5)
        if isinstance(confidence, bool):
            raise CustomerValidationError("confidence must be a number.")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError) as exc:
            raise CustomerValidationError("confidence must be a number.") from exc
        if not 0.0 <= confidence_value <= 1.0:
            raise CustomerValidationError("confidence must be between 0 and 1.")
        segment_id = _text(data.get("segment_id"))
        if segment_id and workspace.segment(segment_id) is None:
            raise CustomerRecordNotFound("segment", segment_id)
        row = {
            "id": new_id("research"),
            "product_id": workspace.product_id,
            "segment_id": segment_id,
            "kind": _bounded_text(data.get("kind") or "market_research", "Research kind", 80),
            "title": _bounded_text(data.get("title"), "Research title", 240, required=True),
            "summary": summary,
            "source_url": source_url,
            "source_reference": source_reference,
            "retrieved_at": _text(data.get("retrieved_at")) or self._clock().isoformat(),
            "confidence": round(confidence_value, 4),
            "verification_status": verification,
            "direct_validation": verification == ResearchVerification.DIRECT_VALIDATION.value,
            "verified": verification == ResearchVerification.DIRECT_VALIDATION.value,
            "created_by": _text(data.get("created_by")) or _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
            "notes": _bounded_text(data.get("notes"), "Research notes", 2000),
        }
        workspace.research.append(row)
        self._event(workspace, "research_recorded", actor=_text(actor) or self.actor, detail={"research_id": row["id"], "verification": verification})
        self._persist()
        return row

    # -- customers and leads -------------------------------------------------

    def _normalise_contact(self, value: Any) -> dict[str, str]:
        data = _as_mapping(value, "contact")
        contact = {
            "email": _bounded_text(data.get("email"), "Email", 320).lower(),
            "phone": _bounded_text(data.get("phone"), "Phone", 80),
        }
        if contact["email"] and not _email(contact["email"]):
            raise CustomerValidationError("Email is not a valid address.")
        return contact

    def _normalise_consent(self, value: Any) -> dict[str, Any]:
        data = _as_mapping(value, "consent")
        status = _text(data.get("status") or ConsentStatus.UNKNOWN.value).lower()
        allowed = {row.value for row in ConsentStatus}
        if status not in allowed:
            raise CustomerValidationError(f"consent.status must be one of: {', '.join(sorted(allowed))}")
        channels = [str(row).lower() for row in _as_strings(data.get("channels"))]
        invalid = [row for row in channels if row not in {item.value for item in CommunicationChannel}]
        if invalid:
            raise CustomerValidationError("consent contains an unsupported communication channel.")
        return {
            "status": status,
            "channels": channels,
            "source": _bounded_text(data.get("source"), "Consent source", 300),
            "captured_at": _text(data.get("captured_at")),
            "withdrawn_at": _text(data.get("withdrawn_at")),
            "note": _bounded_text(data.get("note"), "Consent note", 1000),
        }

    def _contact_for(self, workspace: CustomerWorkspace, kind: str, record_id: str) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
        if kind == "lead":
            lead = workspace.lead(record_id)
            if lead is None:
                raise CustomerRecordNotFound("lead", record_id)
            return lead, dict(lead.get("contact") or {}), dict(lead.get("consent") or {})
        if kind == "customer":
            customer = workspace.customer(record_id)
            if customer is None:
                raise CustomerRecordNotFound("customer", record_id)
            return customer, dict(customer.get("contact") or {}), dict(customer.get("consent") or {})
        raise CustomerValidationError("related_type must be lead or customer.")

    def update_consent(
        self,
        product_id: str,
        related_type: str,
        record_id: str,
        inputs: Mapping[str, Any],
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        """Record a consent decision without discarding its provenance.

        Consent is read live again by approval and send, so a withdrawal cannot
        be bypassed by an older draft that was created while consent was granted.
        """
        workspace = self._get(product_id)
        self._require_active(workspace)
        kind = _text(related_type).lower()
        related, _contact, current = self._contact_for(workspace, kind, record_id)
        data = dict(inputs or {})
        if "status" not in data:
            raise CustomerValidationError("A consent update needs a status.")
        previous_status = str(current.get("status") or ConsentStatus.UNKNOWN.value)
        merged = dict(current)
        for key in ("status", "source", "captured_at", "withdrawn_at", "note"):
            if key in data:
                merged[key] = data[key]
        if "channels" in data:
            merged["channels"] = data["channels"]
        consent = self._normalise_consent(merged)
        if (
            consent["status"] in {ConsentStatus.GRANTED.value, ConsentStatus.NOT_REQUIRED.value}
            and previous_status not in {ConsentStatus.GRANTED.value, ConsentStatus.NOT_REQUIRED.value}
            and not _text(data.get("source"))
        ):
            raise CustomerValidationError("Granting consent needs a recorded source.")
        if consent["status"] == ConsentStatus.WITHDRAWN.value and not consent["withdrawn_at"]:
            consent["withdrawn_at"] = self._clock().isoformat()
        if consent["status"] in {ConsentStatus.GRANTED.value, ConsentStatus.NOT_REQUIRED.value} and not consent["captured_at"]:
            consent["captured_at"] = self._clock().isoformat()
        related["consent"] = consent
        if kind == "lead":
            related["consent_status"] = consent["status"]
        related["updated_at"] = self._clock().isoformat()
        self._event(
            workspace,
            "consent_updated",
            actor=_text(actor) or self.actor,
            detail={"related_type": kind, "related_id": record_id, "status": consent["status"], "channels": consent["channels"]},
        )
        self._persist()
        return {
            "related_type": kind,
            "related_id": record_id,
            "consent": consent,
            "updated_at": related["updated_at"],
        }

    def add_customer(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        self._check_capacity(workspace, "customers", self.max_customers)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        name = _bounded_text(data.get("name"), "Customer name", 160, required=True)
        category = _text(data.get("category") or CustomerCategory.BUSINESS.value).lower()
        if category not in {row.value for row in CustomerCategory}:
            raise CustomerValidationError("Category must be business or individual.")
        segment_id = _text(data.get("segment_id"))
        if segment_id and workspace.segment(segment_id) is None:
            raise CustomerRecordNotFound("segment", segment_id)
        contact = self._normalise_contact(data.get("contact"))
        consent = self._normalise_consent(data.get("consent"))
        evidence = _as_strings(data.get("evidence"))
        verified = _optional_bool(data, "verified")
        if verified and not evidence:
            raise CustomerValidationError("A verified customer needs recorded evidence.")
        row = {
            "id": new_id("customer"),
            "product_id": workspace.product_id,
            "name": name,
            "category": category,
            "segment_id": segment_id,
            "industry": _bounded_text(data.get("industry"), "Industry", 120),
            "company_size": _bounded_text(data.get("company_size"), "Company size", 80),
            "location": _bounded_text(data.get("location"), "Location", 160),
            "contact": contact,
            "contact_source": _bounded_text(data.get("contact_source"), "Contact source", 300),
            "consent": consent,
            "verified": verified,
            "verification_state": "verified" if verified else "recorded",
            "status": "active",
            "owner": _bounded_text(data.get("owner"), "Owner", 120) or self.actor,
            "next_action": _bounded_text(data.get("next_action"), "Next action", 500),
            "tags": _as_strings(data.get("tags")),
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.customers.append(row)
        self._event(workspace, "customer_created", actor=_text(actor) or self.actor, detail={"customer_id": row["id"]})
        self._persist()
        return row

    def list_customers(self, product_id: str) -> list[dict[str, Any]]:
        return list(self._get(product_id).customers)

    def customer_detail(self, product_id: str, customer_id: str) -> dict[str, Any]:
        workspace = self._get(product_id)
        customer = workspace.customer(customer_id)
        if customer is None:
            raise CustomerRecordNotFound("customer", customer_id)
        return {
            **customer,
            "interactions": [row for row in workspace.interactions if row.get("related_id") == customer_id],
            "deals": [row for row in workspace.deals if row.get("customer_id") == customer_id],
            "tickets": [row for row in workspace.tickets if row.get("customer_id") == customer_id],
            "feedback": [row for row in workspace.feedback if row.get("customer_id") == customer_id],
            "onboarding": [row for row in workspace.onboarding if row.get("customer_id") == customer_id],
            "success": workspace.customer_success(customer_id),
        }

    def add_lead(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        self._check_capacity(workspace, "leads", self.max_leads)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        name = _bounded_text(data.get("name") or data.get("organization"), "Lead name or organization", 180, required=True)
        source = _text(data.get("source") or "operator_supplied").lower()
        if source not in LEAD_SOURCES:
            raise CustomerValidationError(f"Lead source must be one of: {', '.join(LEAD_SOURCES)}")
        lawful_basis = _bounded_text(data.get("lawful_basis"), "Lawful basis", 500)
        if source in {"public_directory", "manual_import", "approved_integration"} and not lawful_basis:
            raise CustomerValidationError(f"Source {source} requires a recorded lawful basis.")
        segment_id = _text(data.get("segment_id"))
        if segment_id and workspace.segment(segment_id) is None:
            raise CustomerRecordNotFound("segment", segment_id)
        contact = self._normalise_contact(data.get("contact"))
        consent = self._normalise_consent(data.get("consent"))
        inputs_data = data.get("qualification_inputs")
        if inputs_data is not None and not isinstance(inputs_data, Mapping):
            raise CustomerValidationError("qualification_inputs must be an object.")
        row = {
            "id": new_id("lead"),
            "product_id": workspace.product_id,
            "name": name,
            "organization": _bounded_text(data.get("organization"), "Organization", 180),
            "contact": contact,
            "contact_source": _bounded_text(data.get("contact_source"), "Contact source", 300),
            "source": source,
            "source_detail": _bounded_text(data.get("source_detail"), "Source detail", 500),
            "lawful_basis": lawful_basis,
            "product_interest": _bounded_text(data.get("product_interest"), "Product interest", 500)
            or workspace.product_snapshot.get("name", ""),
            "segment_id": segment_id,
            "location": _bounded_text(data.get("location"), "Location", 160),
            "status": LeadStatus.NEW.value,
            "status_history": [{"status": LeadStatus.NEW.value, "at": self._clock().isoformat(), "actor": _text(actor) or self.actor}],
            "consent": consent,
            "consent_status": consent.get("status", ConsentStatus.UNKNOWN.value),
            "owner": _bounded_text(data.get("owner"), "Owner", 120) or self.actor,
            "next_action": _bounded_text(data.get("next_action"), "Next action", 500),
            "disqualification_reason": _bounded_text(
                data.get("disqualification_reason"), "Disqualification reason", 500
            ),
            "qualification_inputs": dict(inputs_data or {}),
            "qualification": None,
            "qualification_history": [],
            "interaction_ids": [],
            "customer_id": "",
            "lost_reason": "",
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.leads.append(row)
        self._event(workspace, "lead_created", actor=_text(actor) or self.actor, detail={"lead_id": row["id"], "source": source})
        self._persist()
        return row

    def list_leads(self, product_id: str, *, status: str = "") -> list[dict[str, Any]]:
        rows = list(self._get(product_id).leads)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def qualify(
        self, product_id: str, lead_id: str, *, inputs: Mapping[str, Any] | None = None, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        lead = workspace.lead(lead_id)
        if lead is None:
            raise CustomerRecordNotFound("lead", lead_id)
        if inputs is not None and not isinstance(inputs, Mapping):
            raise CustomerValidationError("qualification inputs must be an object.")
        try:
            result = qualify_lead(
                lead,
                workspace.qualification_criteria,
                inputs,
                qualified_at=0.60,
                unqualified_at=0.35,
            )
        except ValueError as exc:
            raise CustomerValidationError(str(exc)) from exc
        lead["qualification"] = result
        lead.setdefault("qualification_history", []).append(result)
        lead["qualification_history"] = lead["qualification_history"][-20:]
        current = str(lead.get("status", LeadStatus.NEW.value))
        if current in {
            LeadStatus.NEW.value,
            LeadStatus.RESEARCHING.value,
            LeadStatus.UNQUALIFIED.value,
            LeadStatus.QUALIFIED.value,
        }:
            lead["status"] = result["recommended_status"]
        lead["updated_at"] = self._clock().isoformat()
        self._event(
            workspace,
            "lead_qualified",
            actor=_text(actor) or self.actor,
            detail={"lead_id": lead_id, "score": result["score"], "status": lead["status"]},
        )
        self._persist()
        return result

    _LEAD_TRANSITIONS: dict[str, set[str]] = {
        LeadStatus.NEW.value: {
            LeadStatus.RESEARCHING.value,
            LeadStatus.QUALIFIED.value,
            LeadStatus.UNQUALIFIED.value,
            LeadStatus.CONTACT_PENDING_APPROVAL.value,
            LeadStatus.ARCHIVED.value,
        },
        LeadStatus.RESEARCHING.value: {
            LeadStatus.QUALIFIED.value,
            LeadStatus.UNQUALIFIED.value,
            LeadStatus.CONTACT_PENDING_APPROVAL.value,
            LeadStatus.ARCHIVED.value,
        },
        LeadStatus.QUALIFIED.value: {
            LeadStatus.CONTACT_PENDING_APPROVAL.value,
            LeadStatus.IN_CONVERSATION.value,
            LeadStatus.LOST.value,
            LeadStatus.ARCHIVED.value,
        },
        LeadStatus.UNQUALIFIED.value: {LeadStatus.RESEARCHING.value, LeadStatus.ARCHIVED.value},
        LeadStatus.CONTACT_PENDING_APPROVAL.value: {
            LeadStatus.IN_CONVERSATION.value,
            LeadStatus.LOST.value,
            LeadStatus.ARCHIVED.value,
        },
        LeadStatus.IN_CONVERSATION.value: {
            LeadStatus.CONVERTED.value,
            LeadStatus.LOST.value,
            LeadStatus.ARCHIVED.value,
        },
        LeadStatus.CONVERTED.value: {LeadStatus.ARCHIVED.value},
        LeadStatus.LOST.value: {LeadStatus.RESEARCHING.value, LeadStatus.ARCHIVED.value},
        LeadStatus.ARCHIVED.value: {LeadStatus.RESEARCHING.value},
    }

    def set_lead_status(
        self,
        product_id: str,
        lead_id: str,
        status: str,
        *,
        lost_reason: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        lead = workspace.lead(lead_id)
        if lead is None:
            raise CustomerRecordNotFound("lead", lead_id)
        target = _text(status).upper()
        current = str(lead.get("status", LeadStatus.NEW.value))
        if target == current:
            return lead
        if target == LeadStatus.CONVERTED.value:
            raise CustomerValidationError("Use the convert action to create the customer record.")
        allowed = self._LEAD_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise CustomerConflict(f"Lead status {current} cannot move to {target}.")
        qualification = lead.get("qualification") or {}
        if target in {LeadStatus.QUALIFIED.value, LeadStatus.CONTACT_PENDING_APPROVAL.value} and qualification.get("recommended_status") != LeadStatus.QUALIFIED.value:
            raise CustomerValidationError(
                "A lead needs a QUALIFIED qualification result before this status."
            )
        if target == LeadStatus.LOST.value and not _text(lost_reason):
            raise CustomerValidationError("A lost lead needs a recorded reason.")
        lead["status"] = target
        if target == LeadStatus.LOST.value:
            lead["lost_reason"] = _text(lost_reason)
        lead["updated_at"] = self._clock().isoformat()
        lead.setdefault("status_history", []).append(
            {"status": target, "at": lead["updated_at"], "actor": _text(actor) or self.actor, "reason": _text(lost_reason)}
        )
        self._event(workspace, "lead_status_changed", actor=_text(actor) or self.actor, detail={"lead_id": lead_id, "status": target})
        self._persist()
        return lead

    def convert_lead(
        self,
        product_id: str,
        lead_id: str,
        *,
        customer_inputs: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        lead = workspace.lead(lead_id)
        if lead is None:
            raise CustomerRecordNotFound("lead", lead_id)
        if lead.get("status") not in {
            LeadStatus.QUALIFIED.value,
            LeadStatus.CONTACT_PENDING_APPROVAL.value,
            LeadStatus.IN_CONVERSATION.value,
        } or (lead.get("qualification") or {}).get("recommended_status") != LeadStatus.QUALIFIED.value:
            raise CustomerConflict("Only a positively qualified or in-conversation lead can be converted.")
        if lead.get("customer_id"):
            customer = workspace.customer(str(lead["customer_id"]))
            if customer is None:
                raise CustomerConflict("The linked customer record is missing.")
            return customer
        data = dict(customer_inputs or {})
        data.setdefault("name", lead.get("name"))
        data.setdefault("category", CustomerCategory.BUSINESS.value)
        data.setdefault("segment_id", lead.get("segment_id", ""))
        data.setdefault("industry", lead.get("industry", ""))
        data.setdefault("location", lead.get("location", ""))
        data.setdefault("contact", lead.get("contact") or {})
        data.setdefault("contact_source", lead.get("contact_source", ""))
        data.setdefault("consent", lead.get("consent") or {})
        data.setdefault("owner", lead.get("owner", ""))
        customer = self.add_customer(product_id, data, actor=actor or self.actor)
        lead["customer_id"] = customer["id"]
        lead["status"] = LeadStatus.CONVERTED.value
        lead["updated_at"] = self._clock().isoformat()
        lead.setdefault("status_history", []).append(
            {"status": LeadStatus.CONVERTED.value, "at": lead["updated_at"], "actor": _text(actor) or self.actor}
        )
        self._event(workspace, "lead_converted", actor=_text(actor) or self.actor, detail={"lead_id": lead_id, "customer_id": customer["id"]})
        self._persist()
        return {**customer, "lead_id": lead_id}

    # -- interactions and deals ----------------------------------------------

    def add_interaction(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        summary = _bounded_text(data.get("summary"), "Interaction summary", 4000, required=True)
        related_type = _text(data.get("related_type") or "customer").lower()
        related_id = _text(data.get("related_id"))
        if related_type == "lead":
            related = workspace.lead(related_id)
        elif related_type == "customer":
            related = workspace.customer(related_id)
        elif related_type == "deal":
            related = workspace.deal(related_id)
        elif related_type == "ticket":
            related = workspace.ticket(related_id)
        else:
            raise CustomerValidationError("related_type must be lead, customer, deal or ticket.")
        if related is None:
            raise CustomerRecordNotFound(related_type, related_id)
        row = {
            "id": new_id("interaction"),
            "product_id": workspace.product_id,
            "related_type": related_type,
            "related_id": related_id,
            "kind": _bounded_text(data.get("kind") or "note", "Interaction kind", 80),
            "summary": summary,
            "direction": _bounded_text(data.get("direction") or "internal", "Direction", 40),
            "occurred_at": _text(data.get("occurred_at")) or self._clock().isoformat(),
            "source": _bounded_text(data.get("source"), "Interaction source", 300),
            "created_by": _text(data.get("created_by")) or _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
        }
        workspace.interactions.append(row)
        if related_type == "lead":
            related.setdefault("interaction_ids", []).append(row["id"])
        self._event(workspace, "interaction_recorded", actor=_text(actor) or self.actor, detail={"interaction_id": row["id"]})
        self._persist()
        return row

    def create_deal(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        self._check_capacity(workspace, "deals", self.max_deals)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        lead_id = _text(data.get("lead_id"))
        customer_id = _text(data.get("customer_id"))
        lead = workspace.lead(lead_id) if lead_id else None
        if lead_id and lead is None:
            raise CustomerRecordNotFound("lead", lead_id)
        if customer_id and workspace.customer(customer_id) is None:
            raise CustomerRecordNotFound("customer", customer_id)
        if lead is not None:
            if lead.get("status") not in {
                LeadStatus.QUALIFIED.value,
                LeadStatus.CONTACT_PENDING_APPROVAL.value,
                LeadStatus.IN_CONVERSATION.value,
                LeadStatus.CONVERTED.value,
            } or (lead.get("qualification") or {}).get("recommended_status") != LeadStatus.QUALIFIED.value:
                raise CustomerConflict("A deal needs a positively qualified lead or a recorded customer.")
            linked_customer = _text(lead.get("customer_id"))
            if customer_id and linked_customer and customer_id != linked_customer:
                raise CustomerConflict("The lead and customer belong to different customer records.")
            if not customer_id and linked_customer:
                if workspace.customer(linked_customer) is None:
                    raise CustomerConflict("The lead's linked customer record is missing.")
                customer_id = linked_customer
        if not lead_id and not customer_id:
            raise CustomerValidationError("A deal needs a lead or customer.")
        value = data.get("value", 0)
        if isinstance(value, bool):
            raise CustomerValidationError("Deal value must be a number.")
        try:
            amount = float(value or 0)
        except (TypeError, ValueError) as exc:
            raise CustomerValidationError("Deal value must be a number.") from exc
        if not math.isfinite(amount):
            raise CustomerValidationError("Deal value must be a finite number.")
        if amount < 0:
            raise CustomerValidationError("Deal value cannot be negative.")
        currency = _text(data.get("currency") or "USD").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CustomerValidationError("Currency must be a three-letter code.")
        probability = data.get("probability", 0.0)
        if isinstance(probability, bool):
            raise CustomerValidationError("Probability must be a number.")
        try:
            chance = float(probability or 0.0)
        except (TypeError, ValueError) as exc:
            raise CustomerValidationError("Probability must be a number.") from exc
        if not 0.0 <= chance <= 1.0:
            raise CustomerValidationError("Probability must be between 0 and 1.")
        stage = workspace.pipeline_stages[0]
        row = {
            "id": new_id("deal"),
            "product_id": workspace.product_id,
            "lead_id": lead_id,
            "customer_id": customer_id,
            "name": _bounded_text(data.get("name"), "Deal name", 200)
            or f"Deal — {workspace.product_snapshot.get('name', '')}",
            "stage": str(stage.get("id")),
            "value": round(amount, 2),
            "value_is_estimate": True,
            "currency": currency,
            "probability": round(chance, 4),
            "expected_close": _text(data.get("expected_close")),
            "owner": _bounded_text(data.get("owner"), "Owner", 120) or self.actor,
            "next_action": _bounded_text(data.get("next_action"), "Next action", 500),
            "objections": _as_strings(data.get("objections")),
            "competitor_alternatives": _as_strings(data.get("competitor_alternatives")),
            "proposal_history": [],
            "decision_history": [],
            "stage_history": [{"stage": str(stage.get("id")), "at": self._clock().isoformat(), "actor": _text(actor) or self.actor}],
            "realized_amount": None,
            "closed_at": "",
            "lost_reason": "",
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.deals.append(row)
        self._event(workspace, "deal_created", actor=_text(actor) or self.actor, detail={"deal_id": row["id"], "stage": row["stage"]})
        self._persist()
        return row

    _DEAL_TERMINAL = {"closed_won", "closed_lost"}

    def move_deal(
        self,
        product_id: str,
        deal_id: str,
        stage: str,
        *,
        lost_reason: str = "",
        realized_amount: Any = None,
        actor: str = "",
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        deal = workspace.deal(deal_id)
        if deal is None:
            raise CustomerRecordNotFound("deal", deal_id)
        target = _text(stage).lower()
        stage_row = workspace.stage(target)
        if stage_row is None:
            raise CustomerValidationError(f"Unknown pipeline stage: {target}")
        current = str(deal.get("stage"))
        if current in self._DEAL_TERMINAL:
            raise CustomerConflict("A closed deal cannot change stage.")
        if target == current:
            return deal
        if target == "closed_lost" and not _text(lost_reason):
            raise CustomerValidationError("A closed-lost deal needs a recorded reason.")
        if target != "closed_won" and realized_amount is not None:
            raise CustomerValidationError("Only a closed-won deal can record a realized amount.")
        if target == "closed_won" and realized_amount is not None:
            if isinstance(realized_amount, bool):
                raise CustomerValidationError("realized_amount must be a number.")
            try:
                realized = float(realized_amount)
            except (TypeError, ValueError) as exc:
                raise CustomerValidationError("realized_amount must be a number.") from exc
            if not math.isfinite(realized):
                raise CustomerValidationError("realized_amount must be a finite number.")
            if realized < 0:
                raise CustomerValidationError("realized_amount cannot be negative.")
            deal["realized_amount"] = round(realized, 2)
        deal["stage"] = target
        deal["updated_at"] = self._clock().isoformat()
        if target in self._DEAL_TERMINAL:
            deal["closed_at"] = deal["updated_at"]
        if target == "closed_lost":
            deal["lost_reason"] = _text(lost_reason)
        deal.setdefault("stage_history", []).append(
            {"stage": target, "at": deal["updated_at"], "actor": _text(actor) or self.actor}
        )
        self._event(workspace, "deal_stage_changed", actor=_text(actor) or self.actor, detail={"deal_id": deal_id, "stage": target})
        self._persist()
        return deal

    def add_proposal(
        self, product_id: str, deal_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        deal = workspace.deal(deal_id)
        if deal is None:
            raise CustomerRecordNotFound("deal", deal_id)
        data = dict(inputs or {})
        value = data.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise CustomerValidationError("Proposal value must be a number.")
        row = {
            "id": new_id("proposal"),
            "title": _bounded_text(data.get("title") or "Proposal", "Proposal title", 200),
            "summary": _bounded_text(data.get("summary"), "Proposal summary", 4000, required=True),
            "value": value,
            "terms": _bounded_text(data.get("terms"), "Proposal terms", 2000),
            "source_reference": _bounded_text(data.get("source_reference"), "Source reference", 500),
            "created_by": _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
        }
        deal.setdefault("proposal_history", []).append(row)
        deal["updated_at"] = self._clock().isoformat()
        self._event(workspace, "proposal_recorded", actor=_text(actor) or self.actor, detail={"deal_id": deal_id, "proposal_id": row["id"]})
        self._persist()
        return row

    def assist_sales(
        self,
        product_id: str,
        *,
        lead_id: str = "",
        deal_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        lead = workspace.lead(lead_id) if lead_id else None
        deal = workspace.deal(deal_id) if deal_id else None
        if lead_id and lead is None:
            raise CustomerRecordNotFound("lead", lead_id)
        if deal_id and deal is None:
            raise CustomerRecordNotFound("deal", deal_id)
        if lead is not None and deal is not None:
            deal_lead = _text(deal.get("lead_id"))
            if deal_lead and deal_lead != _text(lead.get("id")):
                raise CustomerConflict("The sales brief lead and deal do not belong together.")
            lead_customer = _text(lead.get("customer_id"))
            deal_customer = _text(deal.get("customer_id"))
            if lead_customer and deal_customer and lead_customer != deal_customer:
                raise CustomerConflict("The sales brief lead and deal have different customers.")
        try:
            result = sales_brief(workspace, lead=lead, deal=deal)
        except ValueError as exc:
            raise CustomerValidationError(str(exc)) from exc
        workspace.sales_assists.append(result)
        workspace.sales_assists = workspace.sales_assists[-100:]
        self._event(
            workspace,
            "sales_assistance_recorded",
            actor=_text(actor) or self.actor,
            detail={
                "sales_brief_id": result["id"],
                "lead_id": lead_id,
                "deal_id": deal_id,
            },
        )
        self._persist()
        return result

    def record_decision(
        self, product_id: str, deal_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        deal = workspace.deal(deal_id)
        if deal is None:
            raise CustomerRecordNotFound("deal", deal_id)
        data = dict(inputs or {})
        row = {
            "id": new_id("decision"),
            "decision": _bounded_text(data.get("decision"), "Decision", 200, required=True),
            "reason": _bounded_text(data.get("reason"), "Decision reason", 2000),
            "source_reference": _bounded_text(data.get("source_reference"), "Source reference", 500),
            "recorded_by": _text(actor) or self.actor,
            "at": self._clock().isoformat(),
        }
        deal.setdefault("decision_history", []).append(row)
        deal["updated_at"] = self._clock().isoformat()
        self._event(workspace, "deal_decision_recorded", actor=_text(actor) or self.actor, detail={"deal_id": deal_id})
        self._persist()
        return row

    # -- communication -------------------------------------------------------

    def set_automation_policy(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = "", note: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        enabled = _optional_bool(data, "enabled")
        channels = [str(row).lower() for row in _as_strings(data.get("channels"))]
        invalid = [row for row in channels if row not in {item.value for item in CommunicationChannel}]
        if invalid:
            raise CustomerValidationError("Automation policy contains an unsupported channel.")
        try:
            max_recipients = int(data.get("max_recipients", 1))
            daily_limit = int(data.get("daily_limit", self.max_communications_per_day))
        except (TypeError, ValueError) as exc:
            raise CustomerValidationError("Automation limits must be integers.") from exc
        if max_recipients < 1 or max_recipients > 10:
            raise CustomerValidationError("max_recipients must be between 1 and 10.")
        if daily_limit < 1 or daily_limit > self.max_communications_per_day:
            raise CustomerValidationError(
                f"daily_limit must be between 1 and {self.max_communications_per_day}."
            )
        if enabled and not _text(note):
            raise CustomerValidationError("Enabling a communication policy needs a recorded approval note.")
        policy = {
            "enabled": enabled,
            "channels": channels,
            "max_recipients": max_recipients,
            "daily_limit": daily_limit,
            "consent_required": True,
            "note": _bounded_text(note, "Policy note", 1000),
            "updated_by": _text(actor) or self.actor,
            "updated_at": self._clock().isoformat(),
        }
        workspace.automation_policy = policy
        self._approval(workspace, "communication_policy", target_id=workspace.id, actor=_text(actor) or self.actor, note=note, approved=enabled)
        self._event(workspace, "communication_policy_updated", actor=_text(actor) or self.actor, detail={"enabled": enabled, "channels": channels})
        self._persist()
        return policy

    def _live_consent(self, workspace: CustomerWorkspace, row: Mapping[str, Any]) -> dict[str, Any]:
        """Read the current consent of the related record, never a stale draft copy."""
        try:
            _related, _contact, consent = self._contact_for(
                workspace,
                str(row.get("related_type", "")),
                str(row.get("related_id", "")),
            )
        except (CustomerRecordNotFound, CustomerValidationError):
            return {}
        return consent

    def _consent_allows(self, consent: Mapping[str, Any], channel: str) -> tuple[bool, str]:
        status = str(consent.get("status", ConsentStatus.UNKNOWN.value))
        if status == ConsentStatus.NOT_REQUIRED.value:
            return True, "Consent was recorded as not required."
        if status == ConsentStatus.WITHDRAWN.value:
            return False, "Communication consent was withdrawn."
        if status != ConsentStatus.GRANTED.value:
            return False, "Communication consent has not been granted."
        channels = [str(row).lower() for row in consent.get("channels") or []]
        if channels and channel not in channels:
            return False, f"Consent does not include the {channel} channel."
        return True, "Consent allows this channel."

    def draft_communication(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        self._check_capacity(workspace, "communications", self.max_communications)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        related_type = _text(data.get("related_type") or "customer").lower()
        related_id = _text(data.get("related_id"))
        related, contact, consent = self._contact_for(workspace, related_type, related_id)
        channel = _text(data.get("channel") or CommunicationChannel.EMAIL.value).lower()
        if channel not in {row.value for row in CommunicationChannel}:
            raise CustomerValidationError(f"channel must be one of: {', '.join(row.value for row in CommunicationChannel)}")
        recipient = _bounded_text(data.get("recipient"), "Recipient", 320) or contact.get(channel) or contact.get("email") or ""
        if channel == CommunicationChannel.EMAIL.value and not recipient:
            raise CustomerValidationError("An email communication needs a recipient address.")
        if channel == CommunicationChannel.EMAIL.value and not _email(recipient):
            raise CustomerValidationError("Recipient email is not a valid address.")
        body = _bounded_text(data.get("body"), "Message body", 12000, required=True)
        subject = _bounded_text(data.get("subject"), "Subject", 300)
        consent_ok, consent_reason = self._consent_allows(consent, channel)
        row = {
            "id": new_id("comm"),
            "product_id": workspace.product_id,
            "related_type": related_type,
            "related_id": related_id,
            "customer_id": related_id if related_type == "customer" else _text(related.get("customer_id")),
            "channel": channel,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "status": CommunicationStatus.DRAFT.value,
            "consent_status": consent.get("status", ConsentStatus.UNKNOWN.value),
            "consent_ok": consent_ok,
            "consent_reason": consent_reason,
            "requires_approval": True,
            "approved_by": "",
            "approved_at": "",
            "approval_note": "",
            "sent_at": "",
            "message_id": "",
            "failure": "",
            "automation_policy": False,
            "created_by": _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.communications.append(row)
        self._event(workspace, "communication_drafted", actor=_text(actor) or self.actor, detail={"communication_id": row["id"], "channel": channel})
        self._persist()
        return row

    def approve_communication(
        self,
        product_id: str,
        communication_id: str,
        *,
        approved: bool,
        actor: str,
        note: str,
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        row = workspace.communication(communication_id)
        if row is None:
            raise CustomerRecordNotFound("communication", communication_id)
        if row.get("status") != CommunicationStatus.DRAFT.value:
            raise CustomerConflict("Only a draft communication can be approved or rejected.")
        if not _text(actor) or not _text(note):
            raise CustomerValidationError("A communication decision needs a named approver and note.")
        current_consent = self._live_consent(workspace, row)
        consent_ok, consent_reason = self._consent_allows(
            current_consent, str(row.get("channel", ""))
        )
        row["consent_status"] = current_consent.get("status", ConsentStatus.UNKNOWN.value)
        row["consent_ok"] = consent_ok
        row["consent_reason"] = consent_reason
        if approved and not consent_ok:
            raise ConsentRequired(
                f"Consent does not allow this communication: {consent_reason}",
                channel=str(row.get("channel", "")),
                recipient=str(row.get("recipient", "")),
            )
        row["status"] = CommunicationStatus.APPROVED.value if approved else CommunicationStatus.CANCELLED.value
        row["approved_by"] = _text(actor)
        row["approved_at"] = self._clock().isoformat()
        row["approval_note"] = _text(note)
        row["updated_at"] = row["approved_at"]
        self._approval(workspace, "communication", target_id=communication_id, actor=_text(actor), note=_text(note), approved=approved)
        self._event(workspace, "communication_approved" if approved else "communication_cancelled", actor=_text(actor), detail={"communication_id": communication_id})
        self._persist()
        return row

    def send_communication(
        self, product_id: str, communication_id: str, *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        row = workspace.communication(communication_id)
        if row is None:
            raise CustomerRecordNotFound("communication", communication_id)
        if row.get("status") in {
            CommunicationStatus.SENT.value,
            CommunicationStatus.CANCELLED.value,
            CommunicationStatus.FAILED.value,
        }:
            return row
        current_consent = self._live_consent(workspace, row)
        consent_ok, consent_reason = self._consent_allows(
            current_consent, str(row.get("channel", ""))
        )
        row["consent_status"] = current_consent.get("status", ConsentStatus.UNKNOWN.value)
        row["consent_ok"] = consent_ok
        row["consent_reason"] = consent_reason
        if not consent_ok:
            row["status"] = CommunicationStatus.BLOCKED.value
            row["failure"] = consent_reason
            row["consent_ok"] = False
            row["updated_at"] = self._clock().isoformat()
            self._event(workspace, "communication_blocked", actor=_text(actor) or self.actor, detail={"communication_id": communication_id, "reason": consent_reason})
            self._persist()
            raise ConsentRequired(consent_reason, channel=str(row.get("channel", "")), recipient=str(row.get("recipient", "")))
        policy = dict(workspace.automation_policy or {})
        if row.get("status") != CommunicationStatus.APPROVED.value:
            auto = (
                policy.get("enabled") is True
                and row.get("channel") in (policy.get("channels") or [])
                and row.get("consent_ok") is True
                and int(policy.get("max_recipients", 0) or 0) >= 1
            )
            if not auto:
                raise ConsentRequired(
                    "Explicit approval is required before sending this communication.",
                    channel=str(row.get("channel", "")),
                    recipient=str(row.get("recipient", "")),
                )
            row["automation_policy"] = True
        limit = int(policy.get("daily_limit", self.max_communications_per_day) or self.max_communications_per_day)
        now = self._clock()
        recent = [
            item
            for item in workspace.communications
            if item.get("status") == CommunicationStatus.SENT.value
            and item.get("sent_at")
            and (now - (_date(item.get("sent_at")) or now)).total_seconds() < 86400
        ]
        if len(recent) >= limit:
            raise CustomerBudgetExceeded(f"The daily communication limit ({limit}) is exhausted.")
        if self._sender is None:
            row["status"] = CommunicationStatus.NOT_CONFIGURED.value
            row["failure"] = (
                "No communication sender is configured. The draft was recorded but nothing was sent."
            )
            row["updated_at"] = self._clock().isoformat()
            self._event(workspace, "communication_blocked", actor=_text(actor) or self.actor, detail={"communication_id": communication_id, "reason": "sender_not_configured"})
            self._persist()
            raise CommunicationNotConfigured(row["failure"])
        try:
            response = self._sender(dict(row))
            if inspect.isawaitable(response):
                raise CommunicationNotConfigured("The configured sender must be synchronous.")
            if not isinstance(response, Mapping) or not response.get("sent"):
                raise CustomerConflict(str((response or {}).get("error") or "The sender did not report success."))
            row["status"] = CommunicationStatus.SENT.value
            row["sent_at"] = self._clock().isoformat()
            row["message_id"] = _text(response.get("message_id"))
            row["failure"] = ""
        except Exception as exc:  # noqa: BLE001 - a sender failure is recorded honestly
            row["status"] = CommunicationStatus.FAILED.value
            row["failure"] = str(exc)
        row["updated_at"] = self._clock().isoformat()
        self._event(
            workspace,
            "communication_sent" if row["status"] == CommunicationStatus.SENT.value else "communication_failed",
            actor=_text(actor) or self.actor,
            detail={"communication_id": communication_id, "status": row["status"]},
        )
        self._persist()
        return row

    # -- support -------------------------------------------------------------

    def add_knowledge(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        title = _bounded_text(data.get("title"), "Knowledge title", 240, required=True)
        body = _bounded_text(data.get("body"), "Knowledge body", 12000, required=True)
        source = _bounded_text(data.get("source"), "Knowledge source", 500)
        approved = _optional_bool(data, "approved")
        verified = _optional_bool(data, "verified")
        if approved and not source:
            raise CustomerValidationError("Approved product knowledge needs a source reference.")
        if verified and not approved:
            raise CustomerValidationError("Verified support knowledge must also be approved.")
        row = {
            "id": new_id("knowledge"),
            "product_id": workspace.product_id,
            "title": title,
            "body": body,
            "source": source,
            "version": _bounded_text(data.get("version"), "Knowledge version", 80)
            or str(workspace.product_snapshot.get("version", "")),
            "approved": approved,
            "verified": verified,
            "keywords": _as_strings(data.get("keywords")),
            "steps": _as_strings(data.get("steps")),
            "created_by": _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
        }
        workspace.knowledge.append(row)
        if self._knowledge_callback is not None and approved:
            try:
                result = self._knowledge_callback(
                    f"{title}\n\n{body}\n\nSource: {source}",
                    title=f"Customer support — {title}",
                )
                self._schedule(result)
            except Exception:  # noqa: BLE001 - optional integration
                pass
        self._event(workspace, "support_knowledge_added", actor=_text(actor) or self.actor, detail={"knowledge_id": row["id"], "approved": approved})
        self._persist()
        return row

    def create_ticket(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        self._check_capacity(workspace, "tickets", self.max_tickets)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        customer_id = _text(data.get("customer_id"))
        customer = workspace.customer(customer_id)
        if customer is None:
            raise CustomerRecordNotFound("customer", customer_id)
        subject = _bounded_text(data.get("subject"), "Ticket subject", 300, required=True)
        description = _bounded_text(data.get("description"), "Ticket description", 12000, required=True)
        category = _text(
            data.get("category") or classify_issue(subject, description)
        ).lower()
        if category not in TICKET_CATEGORIES:
            raise CustomerValidationError(f"category must be one of: {', '.join(TICKET_CATEGORIES)}")
        priority = _bounded_text(data.get("priority") or "normal", "Priority", 40).lower()
        severity = _bounded_text(data.get("severity") or "normal", "Severity", 40).lower()
        if priority not in {"low", "normal", "high", "critical"}:
            raise CustomerValidationError("priority must be low, normal, high or critical.")
        if severity not in {"low", "normal", "high", "critical"}:
            raise CustomerValidationError("severity must be low, normal, high or critical.")
        status = TicketStatus.OPEN.value
        row = {
            "id": new_id("ticket"),
            "product_id": workspace.product_id,
            "customer_id": customer_id,
            "customer_name": customer.get("name", ""),
            "category": category,
            "priority": priority,
            "severity": severity,
            "status": status,
            "subject": subject,
            "description": description,
            "assignee": _bounded_text(data.get("assignee"), "Assignee", 120),
            "attachments": _as_strings(data.get("attachments")),
            "messages": [],
            "internal_notes": [],
            "resolution": "",
            "resolved_at": "",
            "escalation_history": [],
            "reopen_count": 0,
            "assistant_runs": [],
            "history": [{"status": status, "at": self._clock().isoformat(), "actor": _text(actor) or self.actor}],
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.tickets.append(row)
        self._event(workspace, "ticket_created", actor=_text(actor) or self.actor, detail={"ticket_id": row["id"], "category": category})
        self._persist()
        return row

    def list_tickets(self, product_id: str, *, status: str = "") -> list[dict[str, Any]]:
        rows = list(self._get(product_id).tickets)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def ticket_detail(self, product_id: str, ticket_id: str) -> dict[str, Any]:
        workspace = self._get(product_id)
        ticket = workspace.ticket(ticket_id)
        if ticket is None:
            raise CustomerRecordNotFound("ticket", ticket_id)
        customer = workspace.customer(str(ticket.get("customer_id", "")))
        return {**ticket, "customer": customer or {}}

    def triage_ticket(
        self, product_id: str, ticket_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        ticket = workspace.ticket(ticket_id)
        if ticket is None:
            raise CustomerRecordNotFound("ticket", ticket_id)
        if ticket.get("status") not in {TicketStatus.OPEN.value, TicketStatus.TRIAGED.value}:
            raise CustomerConflict("Only an open or triaged ticket can be triaged.")
        data = dict(inputs or {})
        category = _text(data.get("category") or ticket.get("category")).lower()
        if category not in TICKET_CATEGORIES:
            raise CustomerValidationError("Unknown ticket category.")
        ticket["category"] = category
        for field, maximum in (("priority", 40), ("severity", 40), ("assignee", 120)):
            if field in data:
                value = _bounded_text(data.get(field), field.title(), maximum)
                if field in {"priority", "severity"}:
                    value = value.lower()
                    if value not in {"low", "normal", "high", "critical"}:
                        raise CustomerValidationError(
                            f"{field} must be low, normal, high or critical."
                        )
                ticket[field] = value
        if ticket.get("status") == TicketStatus.OPEN.value:
            ticket["status"] = TicketStatus.TRIAGED.value
            ticket.setdefault("history", []).append({"status": ticket["status"], "at": self._clock().isoformat(), "actor": _text(actor) or self.actor})
        ticket["updated_at"] = self._clock().isoformat()
        self._event(workspace, "ticket_triaged", actor=_text(actor) or self.actor, detail={"ticket_id": ticket_id, "category": category})
        self._persist()
        return ticket

    def add_ticket_message(
        self, product_id: str, ticket_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        ticket = workspace.ticket(ticket_id)
        if ticket is None:
            raise CustomerRecordNotFound("ticket", ticket_id)
        if ticket.get("status") == TicketStatus.CLOSED.value:
            raise CustomerConflict("A closed ticket must be reopened before adding a message.")
        data = dict(inputs or {})
        body = _bounded_text(data.get("body"), "Message body", 12000, required=True)
        internal = bool(data.get("internal") is True)
        message = {
            "id": new_id("ticketmsg"),
            "ticket_id": ticket_id,
            "body": body,
            "internal": internal,
            "author": _text(data.get("author")) or _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
        }
        if len(ticket.get("messages") or []) >= self.max_messages_per_ticket:
            raise CustomerBudgetExceeded("The ticket message budget is exhausted.")
        if internal:
            ticket.setdefault("internal_notes", []).append(message)
        else:
            ticket.setdefault("messages", []).append(message)
            if ticket.get("status") == TicketStatus.OPEN.value:
                ticket["status"] = TicketStatus.TRIAGED.value
            elif ticket.get("status") == TicketStatus.WAITING_FOR_CUSTOMER.value:
                ticket["status"] = TicketStatus.IN_PROGRESS.value
            elif ticket.get("status") == TicketStatus.RESOLVED.value:
                ticket["status"] = TicketStatus.OPEN.value
                ticket["reopen_count"] = int(ticket.get("reopen_count", 0) or 0) + 1
                ticket["resolved_at"] = ""
            ticket.setdefault("history", []).append({"status": ticket["status"], "at": message["created_at"], "actor": message["author"]})
        ticket["updated_at"] = self._clock().isoformat()
        self._event(workspace, "ticket_message_added", actor=_text(actor) or self.actor, detail={"ticket_id": ticket_id, "internal": internal})
        self._persist()
        return message

    _TICKET_TRANSITIONS: dict[str, set[str]] = {
        TicketStatus.OPEN.value: {TicketStatus.TRIAGED.value, TicketStatus.IN_PROGRESS.value, TicketStatus.ESCALATED.value, TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value},
        TicketStatus.TRIAGED.value: {TicketStatus.IN_PROGRESS.value, TicketStatus.WAITING_FOR_CUSTOMER.value, TicketStatus.ESCALATED.value, TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value},
        TicketStatus.IN_PROGRESS.value: {TicketStatus.WAITING_FOR_CUSTOMER.value, TicketStatus.ESCALATED.value, TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value},
        TicketStatus.WAITING_FOR_CUSTOMER.value: {TicketStatus.IN_PROGRESS.value, TicketStatus.ESCALATED.value, TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value},
        TicketStatus.ESCALATED.value: {TicketStatus.IN_PROGRESS.value, TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value},
        TicketStatus.RESOLVED.value: {TicketStatus.CLOSED.value, TicketStatus.OPEN.value},
        TicketStatus.CLOSED.value: {TicketStatus.OPEN.value},
    }

    def transition_ticket(
        self, product_id: str, ticket_id: str, status: str, *, reason: str = "", resolution: str = "", actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        ticket = workspace.ticket(ticket_id)
        if ticket is None:
            raise CustomerRecordNotFound("ticket", ticket_id)
        target = _text(status).upper()
        current = str(ticket.get("status"))
        if target not in {row.value for row in TicketStatus}:
            raise CustomerValidationError("Unknown ticket status.")
        if target == current:
            return ticket
        if target not in self._TICKET_TRANSITIONS.get(current, set()):
            raise CustomerConflict(f"Ticket status {current} cannot move to {target}.")
        if target == TicketStatus.ESCALATED.value and not _text(reason):
            raise CustomerValidationError("Escalation needs a reason.")
        if target == TicketStatus.RESOLVED.value and not _text(resolution):
            raise CustomerValidationError("Resolution needs a recorded resolution.")
        if target == TicketStatus.OPEN.value and current in {TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value}:
            ticket["reopen_count"] = int(ticket.get("reopen_count", 0) or 0) + 1
        ticket["status"] = target
        ticket["updated_at"] = self._clock().isoformat()
        if target == TicketStatus.RESOLVED.value:
            ticket["resolution"] = _text(resolution)
            ticket["resolved_at"] = ticket["updated_at"]
        if target == TicketStatus.ESCALATED.value:
            ticket.setdefault("escalation_history", []).append(
                {"reason": _text(reason), "at": ticket["updated_at"], "actor": _text(actor) or self.actor}
            )
        ticket.setdefault("history", []).append({"status": target, "at": ticket["updated_at"], "actor": _text(actor) or self.actor, "reason": _text(reason)})
        self._event(workspace, "ticket_status_changed", actor=_text(actor) or self.actor, detail={"ticket_id": ticket_id, "status": target})
        self._persist()
        return ticket

    def assist_ticket(
        self, product_id: str, ticket_id: str, *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        ticket = workspace.ticket(ticket_id)
        if ticket is None:
            raise CustomerRecordNotFound("ticket", ticket_id)
        result = support_assist(ticket, workspace.knowledge, product_snapshot=workspace.product_snapshot)
        result["product_id"] = workspace.product_id
        ticket.setdefault("assistant_runs", []).append(result)
        ticket["assistant_runs"] = ticket["assistant_runs"][-20:]
        ticket["updated_at"] = self._clock().isoformat()
        self._event(workspace, "ticket_assistance_recorded", actor=_text(actor) or self.actor, detail={"ticket_id": ticket_id, "grounded": result["grounded"], "escalation": result["escalation_required"]})
        self._persist()
        return result

    # -- feedback, onboarding and success ------------------------------------

    def add_feedback(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        self._check_capacity(workspace, "feedback", self.max_feedback)
        data = dict(inputs or {})
        self._assert_no_secrets(data)
        kind = _text(data.get("kind") or FeedbackKind.COMPLAINT.value).lower()
        if kind not in {row.value for row in FeedbackKind}:
            raise CustomerValidationError(f"kind must be one of: {', '.join(row.value for row in FeedbackKind)}")
        summary = _bounded_text(data.get("summary"), "Feedback summary", 4000, required=True)
        customer_id = _text(data.get("customer_id"))
        segment_id = _text(data.get("segment_id"))
        ticket_id = _text(data.get("ticket_id"))
        feature = _bounded_text(data.get("feature"), "Feature", 200)
        requirement_id = _bounded_text(data.get("requirement_id"), "Requirement id", 120)
        task_id = _bounded_text(data.get("task_id"), "Task id", 120)
        if customer_id and workspace.customer(customer_id) is None:
            raise CustomerRecordNotFound("customer", customer_id)
        if segment_id and workspace.segment(segment_id) is None:
            raise CustomerRecordNotFound("segment", segment_id)
        if ticket_id and workspace.ticket(ticket_id) is None:
            raise CustomerRecordNotFound("ticket", ticket_id)
        rating = data.get("rating")
        if rating is not None:
            if isinstance(rating, bool):
                raise CustomerValidationError("rating must be a number from 1 to 5.")
            try:
                rating_value = int(rating)
            except (TypeError, ValueError) as exc:
                raise CustomerValidationError("rating must be a number from 1 to 5.") from exc
            if not 1 <= rating_value <= 5:
                raise CustomerValidationError("rating must be between 1 and 5.")
        else:
            rating_value = None
        source = _bounded_text(data.get("source"), "Feedback source", 500)
        verified = _optional_bool(data, "verified")
        if verified and not source:
            raise CustomerValidationError("Verified feedback needs a source.")
        row = {
            "id": new_id("feedback"),
            "product_id": workspace.product_id,
            "product_version": _bounded_text(data.get("product_version"), "Product version", 80)
            or str(workspace.product_snapshot.get("version", "")),
            "customer_id": customer_id,
            "segment_id": segment_id,
            "ticket_id": ticket_id,
            "kind": kind,
            "summary": summary,
            "rating": rating_value,
            "feature": feature,
            "requirement_id": requirement_id,
            "task_id": task_id,
            "severity": _bounded_text(data.get("severity") or "normal", "Severity", 40).lower(),
            "source": source,
            "verified": verified,
            "verification_state": "verified" if verified else "recorded",
            "created_at": self._clock().isoformat(),
        }
        workspace.feedback.append(row)
        self._event(workspace, "feedback_recorded", actor=_text(actor) or self.actor, detail={"feedback_id": row["id"], "kind": kind})
        self._persist()
        return row

    def start_onboarding(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        customer_id = _text(data.get("customer_id"))
        customer = workspace.customer(customer_id)
        if customer is None:
            raise CustomerRecordNotFound("customer", customer_id)
        items = data.get("items")
        if items is None:
            items = ["Confirm account details", "Review product documentation", "Complete initial configuration"]
        raw_items = _as_list(items)
        checklist = []
        for index, item in enumerate(raw_items, start=1):
            if isinstance(item, Mapping):
                title = _bounded_text(item.get("title"), "Onboarding item", 300, required=True)
                item_id = _text(item.get("id")) or f"step-{index}"
            else:
                title = _bounded_text(item, "Onboarding item", 300, required=True)
                item_id = f"step-{index}"
            checklist.append(
                {
                    "id": item_id,
                    "title": title,
                    "status": OnboardingItemStatus.PENDING.value,
                    "evidence": [],
                    "completed_at": "",
                }
            )
        if not checklist:
            raise CustomerValidationError("An onboarding workflow needs at least one item.")
        adoption = data.get("adoption_score", 0.0)
        if isinstance(adoption, bool):
            raise CustomerValidationError("adoption_score must be a number.")
        adoption_value = _number(adoption)
        if adoption_value is None:
            raise CustomerValidationError("adoption_score must be a number.")
        if not 0.0 <= adoption_value <= 1.0:
            raise CustomerValidationError("adoption_score must be between 0 and 1.")
        row = {
            "id": new_id("onboarding"),
            "product_id": workspace.product_id,
            "customer_id": customer_id,
            "status": "in_progress",
            "checklist": checklist,
            "account_setup_guidance": _bounded_text(data.get("account_setup_guidance"), "Account guidance", 4000),
            "tutorials": _as_strings(data.get("tutorials")),
            "documentation": _as_strings(data.get("documentation")),
            "initial_configuration": dict(_as_mapping(data.get("initial_configuration"), "initial_configuration")),
            "adoption_score": round(adoption_value, 4),
            "success_milestones": _as_strings(data.get("success_milestones")),
            "next_follow_up": _text(data.get("next_follow_up")),
            "created_by": _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
        }
        workspace.onboarding.append(row)
        self._event(workspace, "onboarding_started", actor=_text(actor) or self.actor, detail={"onboarding_id": row["id"], "customer_id": customer_id})
        self._persist()
        return row

    def update_onboarding(
        self, product_id: str, onboarding_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        row = workspace.onboarding_record(onboarding_id)
        if row is None:
            raise CustomerRecordNotFound("onboarding", onboarding_id)
        data = dict(inputs or {})
        item_id = _text(data.get("item_id"))
        status = _text(data.get("status") or OnboardingItemStatus.IN_PROGRESS.value).lower()
        if status not in {item.value for item in OnboardingItemStatus}:
            raise CustomerValidationError("Unknown onboarding item status.")
        item = next((entry for entry in row.get("checklist", []) if entry.get("id") == item_id), None)
        if item is None:
            raise CustomerRecordNotFound("onboarding item", item_id)
        evidence = _as_strings(data.get("evidence"))
        if status == OnboardingItemStatus.COMPLETE.value and not evidence:
            raise CustomerValidationError("Completing an onboarding item needs recorded evidence.")
        item["status"] = status
        item["evidence"] = evidence
        item["completed_at"] = self._clock().isoformat() if status == OnboardingItemStatus.COMPLETE.value else ""
        if "adoption_score" in data:
            adoption = data.get("adoption_score")
            if isinstance(adoption, bool):
                raise CustomerValidationError("adoption_score must be a number.")
            value = _number(adoption)
            if value is None:
                raise CustomerValidationError("adoption_score must be a number.")
            if not 0.0 <= value <= 1.0:
                raise CustomerValidationError("adoption_score must be between 0 and 1.")
            row["adoption_score"] = round(value, 4)
        if "next_follow_up" in data:
            row["next_follow_up"] = _bounded_text(data.get("next_follow_up"), "Next follow-up", 200)
        checklist = row.get("checklist") or []
        row["status"] = (
            "complete"
            if checklist and all(entry.get("status") == OnboardingItemStatus.COMPLETE.value for entry in checklist)
            else "in_progress"
        )
        row["updated_at"] = self._clock().isoformat()
        self._event(workspace, "onboarding_updated", actor=_text(actor) or self.actor, detail={"onboarding_id": onboarding_id, "status": row["status"]})
        self._persist()
        return row

    def refresh_success(self, product_id: str, customer_id: str, *, actor: str = "") -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        customer = workspace.customer(customer_id)
        if customer is None:
            raise CustomerRecordNotFound("customer", customer_id)
        tickets = [row for row in workspace.tickets if row.get("customer_id") == customer_id]
        open_tickets = [row for row in tickets if row.get("status") not in {TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value}]
        feedback = [row for row in workspace.feedback if row.get("customer_id") == customer_id]
        onboarding = [row for row in workspace.onboarding if row.get("customer_id") == customer_id]
        interactions = [row for row in workspace.interactions if row.get("related_id") == customer_id]
        adoption = max((float(row.get("adoption_score", 0) or 0) for row in onboarding), default=0.0)
        reasons: list[str] = []
        risk = RiskLevel.LOW.value
        if customer.get("status") == "churned":
            risk = RiskLevel.HIGH.value
            reasons.append("Customer is recorded as churned.")
        if open_tickets:
            risk = RiskLevel.HIGH.value if len(open_tickets) >= 2 else RiskLevel.MEDIUM.value
            reasons.append(f"{len(open_tickets)} support ticket(s) remain open.")
        if any(row.get("kind") == FeedbackKind.COMPLAINT.value for row in feedback):
            risk = RiskLevel.HIGH.value if risk != RiskLevel.LOW.value else RiskLevel.MEDIUM.value
            reasons.append("A customer complaint is recorded.")
        if any(row.get("kind") == FeedbackKind.CHURN_REASON.value for row in feedback):
            risk = RiskLevel.HIGH.value
            reasons.append("A churn reason has been recorded.")
        if onboarding and not any(row.get("status") == "complete" for row in onboarding):
            risk = RiskLevel.MEDIUM.value if risk == RiskLevel.LOW.value else risk
            reasons.append("Onboarding has not completed.")
        if not reasons:
            reasons.append("No open ticket, complaint, churn reason or incomplete onboarding is recorded.")
        row = {
            "id": f"success-{customer_id}",
            "product_id": workspace.product_id,
            "customer_id": customer_id,
            "engagement": {
                "interactions": len(interactions),
                "tickets": len(tickets),
                "open_tickets": len(open_tickets),
                "feedback_records": len(feedback),
            },
            "adoption_score": round(adoption, 4),
            "risk": risk,
            "risk_reasons": reasons,
            "certainty_note": "Risk is an explanatory indicator from recorded observations, not a prediction.",
            "renewal_date": _text(customer.get("renewal_date")),
            "next_follow_up": _text((onboarding[-1] if onboarding else {}).get("next_follow_up")),
            "updated_at": self._clock().isoformat(),
        }
        existing = workspace.customer_success(customer_id)
        if existing is None:
            workspace.success.append(row)
        else:
            existing.update(row)
        self._event(workspace, "customer_success_refreshed", actor=_text(actor) or self.actor, detail={"customer_id": customer_id, "risk": risk})
        self._persist()
        return row

    def mark_customer_churned(
        self, product_id: str, customer_id: str, *, reason: str, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        customer = workspace.customer(customer_id)
        if customer is None:
            raise CustomerRecordNotFound("customer", customer_id)
        if not _text(reason):
            raise CustomerValidationError("A churn decision needs a recorded reason.")
        if customer.get("status") == "churned":
            raise CustomerConflict("The customer is already recorded as churned.")
        self.add_feedback(
            product_id,
            {
                "customer_id": customer_id,
                "kind": FeedbackKind.CHURN_REASON.value,
                "summary": _text(reason),
                "source": "recorded churn decision",
                "verified": True,
            },
            actor=actor or self.actor,
        )
        customer["status"] = "churned"
        customer["churn_reason"] = _text(reason)
        customer["churned_at"] = self._clock().isoformat()
        customer["updated_at"] = customer["churned_at"]
        self._event(workspace, "customer_churned", actor=_text(actor) or self.actor, detail={"customer_id": customer_id})
        self._persist()
        return customer

    def erase_customer(
        self,
        product_id: str,
        customer_id: str,
        *,
        approved: bool,
        reason: str,
        actor: str = "",
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        if not _as_bool(approved, "approved"):
            raise CustomerValidationError("Customer erasure needs explicit approval.")
        if not _text(reason):
            raise CustomerValidationError("Customer erasure needs a recorded reason.")
        customer = workspace.customer(customer_id)
        if customer is None:
            raise CustomerRecordNotFound("customer", customer_id)
        leads = [row for row in workspace.leads if row.get("customer_id") == customer_id]
        deal_ids = {
            str(row.get("id"))
            for row in workspace.deals
            if row.get("customer_id") == customer_id
            or str(row.get("lead_id")) in {str(item.get("id")) for item in leads}
        }
        ticket_ids = {
            str(row.get("id"))
            for row in workspace.tickets
            if row.get("customer_id") == customer_id
        }
        lead_ids = {str(row.get("id")) for row in leads}
        related_ids = {customer_id, *lead_ids, *deal_ids, *ticket_ids}
        workspace.customers = [row for row in workspace.customers if row.get("id") != customer_id]
        workspace.leads = [row for row in workspace.leads if row.get("id") not in lead_ids]
        workspace.deals = [row for row in workspace.deals if str(row.get("id")) not in deal_ids]
        workspace.tickets = [row for row in workspace.tickets if str(row.get("id")) not in ticket_ids]
        workspace.feedback = [row for row in workspace.feedback if row.get("customer_id") != customer_id]
        workspace.onboarding = [row for row in workspace.onboarding if row.get("customer_id") != customer_id]
        workspace.success = [row for row in workspace.success if row.get("customer_id") != customer_id]
        workspace.communications = [
            row
            for row in workspace.communications
            if row.get("customer_id") != customer_id
            and not (row.get("related_type") == "lead" and str(row.get("related_id")) in lead_ids)
        ]
        workspace.interactions = [
            row for row in workspace.interactions if str(row.get("related_id")) not in related_ids
        ]
        workspace.sales_assists = [
            row
            for row in workspace.sales_assists
            if row.get("customer_id") != customer_id
            and str(row.get("lead_id")) not in lead_ids
            and str(row.get("deal_id")) not in deal_ids
        ]
        self._approval(workspace, "customer_erasure", target_id=customer_id, actor=_text(actor) or self.actor, note=_text(reason), approved=True)
        self._event(workspace, "customer_erased", actor=_text(actor) or self.actor, detail={"customer_id": customer_id, "reason": _text(reason), "related_leads": len(leads)})
        self._persist()
        return {"customer_id": customer_id, "erased": True, "reason": _text(reason)}

    # -- analytics -----------------------------------------------------------

    def analytics(self, product_id: str) -> dict[str, Any]:
        return analytics_snapshot(self._get(product_id))

    def add_acquisition_cost(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        workspace = self._get(product_id)
        self._require_active(workspace)
        data = dict(inputs or {})
        amount = data.get("amount")
        if isinstance(amount, bool):
            raise CustomerValidationError("amount must be a number.")
        value = _number(amount)
        if value is None:
            raise CustomerValidationError("amount must be a number.")
        if value < 0:
            raise CustomerValidationError("amount cannot be negative.")
        currency = _text(data.get("currency") or "USD").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CustomerValidationError("Currency must be a three-letter code.")
        source = _bounded_text(data.get("source"), "Cost source", 500, required=True)
        row = {
            "id": new_id("cost"),
            "amount": round(value, 2),
            "currency": currency,
            "source": source,
            "period": _bounded_text(data.get("period"), "Cost period", 120),
            "recorded_by": _text(actor) or self.actor,
            "created_at": self._clock().isoformat(),
        }
        workspace.acquisition_costs.append(row)
        self._event(workspace, "acquisition_cost_recorded", actor=_text(actor) or self.actor, detail={"cost_id": row["id"], "amount": value, "currency": currency})
        self._persist()
        return row


__all__ = ["CustomerService", "NAMESPACE", "SCHEMA_VERSION"]
