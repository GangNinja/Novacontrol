"""Phase 38 business operations and financial intelligence service.

One venture is one isolated document. Revenue and expense records describe
business activity; a separate append-only double-entry journal is the only
source of financial statements. The service never transfers money, executes a
payment, makes a purchase, changes banking details or mutates a finalized
journal entry.
"""

from __future__ import annotations

import inspect
import json
import math
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from novacontrol.finops.analytics import (
    expense_summary as build_expense_summary,
    metric,
    monthly_series_pair,
    operations_dashboard,
    profitability as build_profitability,
    revenue_summary as build_revenue_summary,
    unavailable,
    working_capital as build_working_capital,
)
from novacontrol.finops.errors import (
    FinOpsAuthorizationError,
    FinOpsBudgetExceeded,
    FinOpsConflict,
    FinOpsRecordNotFound,
    FinOpsValidationError,
    IdempotencyConflict,
    PeriodClosed,
    VentureNotFound,
)
from novacontrol.finops.forecasting import build_forecast, compare_scenarios
from novacontrol.finops.health import evaluate_health
from novacontrol.finops.ledger import (
    balance_sheet,
    cash_flow_statement,
    income_statement,
    signed_balance,
    trial_balance,
)
from novacontrol.finops.models import (
    BUSINESS_MODELS,
    CASH_ACCOUNT,
    EXPENSE_CATEGORIES,
    EXPENSE_CATEGORY_ACCOUNTS,
    EXPENSE_STATUSES,
    OPERATING_STATUSES,
    PAYMENT_STATUSES,
    REVENUE_SOURCES,
    REVENUE_STAGES,
    ROLES,
    ROLE_LEVEL,
    currency_code,
    fiscal_period,
    iso_date,
    mapping,
    money_text,
    money_value,
    new_id,
    new_venture,
    optional_date,
    optional_money,
    parse_date,
    parse_money,
    strings,
    text,
    utcnow,
)

SCHEMA_VERSION = 1
NAMESPACE = "business_finance"

CustomerReader = Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
ProductReader = Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
MemoryCallback = Callable[..., Any]
KnowledgeCallback = Callable[..., Any]


def _maybe_await(value: Any) -> Any:
    return value


async def _await_maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _number(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or value is None:
        raise FinOpsValidationError(f"{label} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FinOpsValidationError(f"{label} must be a number.") from exc
    if not math.isfinite(number):
        raise FinOpsValidationError(f"{label} must be finite.")
    if minimum is not None and number < minimum:
        raise FinOpsValidationError(f"{label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise FinOpsValidationError(f"{label} must be at most {maximum}.")
    return number


class FinOpsService:
    """Durable multi-venture operations and finance service."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        customers: CustomerReader | None = None,
        products: ProductReader | None = None,
        opportunities: ProductReader | None = None,
        publish: Callable[[str, Mapping[str, Any]], Any] | None = None,
        audit: Callable[..., Any] | None = None,
        memory: MemoryCallback | None = None,
        knowledge: KnowledgeCallback | None = None,
        actor: str = "operator",
        max_ventures: int = 50,
        max_journal_entries: int = 5000,
        max_records: int = 5000,
        max_events: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._customers = customers
        self._products = products
        self._opportunities = opportunities
        self._publish_callback = publish
        self._audit_callback = audit
        self._memory_callback = memory
        self._knowledge_callback = knowledge
        self.actor = actor or "operator"
        self.max_ventures = max(1, int(max_ventures))
        self.max_journal_entries = max(100, int(max_journal_entries))
        self.max_records = max(100, int(max_records))
        self.max_events = max(20, int(max_events))
        self._clock = clock or utcnow
        self._ventures: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._load_errors: list[str] = []
        self._lock = threading.RLock()

    # -- persistence and isolation -----------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        payload = self._store.read(NAMESPACE)
        rows = payload.get("ventures") if isinstance(payload, Mapping) else None
        if not isinstance(rows, (list, tuple)):
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                venture = dict(row)
                if not venture.get("id"):
                    raise ValueError("venture id is missing")
                self._ventures[str(venture["id"])] = venture
            except Exception as exc:  # noqa: BLE001 - one corrupt venture must not hide the rest
                self._load_errors.append(str(exc))

    def _persist(self) -> None:
        if self._store is None:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._clock().isoformat(),
            "ventures": list(self._ventures.values()),
        }
        with self._lock:
            self._store.write(NAMESPACE, payload)

    def _get(self, venture_id: str) -> dict[str, Any]:
        self._load()
        key = str(venture_id or "").strip()
        venture = self._ventures.get(key)
        if venture is None:
            raise VentureNotFound(key)
        return venture

    def _actor(self, actor: str | None) -> str:
        return (str(actor or "").strip() or self.actor)

    def _role(self, venture: Mapping[str, Any], actor: str) -> str:
        if actor == str(venture.get("authorization", {}).get("owner") or ""):
            return "admin"
        for member in venture.get("authorization", {}).get("members", []):
            if str(member.get("actor")) == actor:
                return str(member.get("role") or "viewer")
        return ""

    def _authorize(self, venture: Mapping[str, Any], actor: str, minimum: str = "viewer") -> str:
        role = self._role(venture, actor)
        if not role:
            raise FinOpsAuthorizationError("This actor has no role on the venture.")
        if ROLE_LEVEL.get(role, 0) < ROLE_LEVEL.get(minimum, 0):
            raise FinOpsAuthorizationError(f"Role {minimum} is required; this actor has {role}.")
        return role

    def _capacity(self, collection: Sequence[Any], maximum: int) -> None:
        if len(collection) >= maximum:
            raise FinOpsBudgetExceeded(f"The {collection} budget is exhausted.")

    def _remember(self, venture: Mapping[str, Any], event_type: str) -> None:
        if self._memory_callback is None:
            return
        try:
            result = self._memory_callback(
                key=f"finance:{venture.get('id')}:latest",
                value={
                    "venture_id": venture.get("id"),
                    "name": venture.get("name"),
                    "event": event_type,
                    "revenue_records": len(venture.get("revenue_records", [])),
                    "expense_records": len(venture.get("expense_records", [])),
                    "journal_entries": len(venture.get("journal_entries", [])),
                    "open_alerts": len([row for row in venture.get("health_alerts", []) if row.get("status") == "open"]),
                },
                text=(
                    f"{event_type} on venture {venture.get('name')}; "
                    f"{len(venture.get('journal_entries', []))} journal entries and "
                    f"{len(venture.get('revenue_records', []))} revenue records."
                ),
                importance=0.7,
            )
            if inspect.isawaitable(result):
                try:
                    import asyncio

                    asyncio.ensure_future(result)
                except RuntimeError:
                    pass
        except Exception:  # noqa: BLE001 - memory is an optional seam
            pass

    def _event(self, venture: dict[str, Any], event_type: str, actor: str, detail: Mapping[str, Any] | None = None) -> None:
        now = self._clock()
        row = {
            "id": new_id("fevt"),
            "type": event_type,
            "venture_id": venture.get("id"),
            "at": now.isoformat(),
            "actor": actor,
            "detail": dict(detail or {}),
        }
        venture.setdefault("events", []).append(row)
        venture["events"] = venture["events"][-self.max_events :]
        venture["updated_at"] = now.isoformat()
        if self._publish_callback is not None:
            try:
                result = self._publish_callback(
                    f"finance.{event_type}",
                    {"venture_id": venture.get("id"), "event_id": row["id"], "actor": actor},
                )
                if inspect.isawaitable(result):
                    try:
                        import asyncio

                        asyncio.ensure_future(result)
                    except RuntimeError:
                        pass
            except Exception:  # noqa: BLE001 - notification must not roll back a record
                pass
        if self._audit_callback is not None and event_type in {
            "venture_created",
            "member_added",
            "journal_posted",
            "journal_reversed",
            "reconciliation_recorded",
            "period_closed",
            "revenue_recorded",
            "revenue_advanced",
            "refund_recorded",
            "expense_recorded",
            "expense_approved",
            "expense_paid",
            "budget_approved",
            "forecast_recorded",
            "alert_resolved",
            "management_report_generated",
        }:
            try:
                self._audit_callback(
                    task_id=str(venture.get("id")),
                    user_request=f"finance:{venture.get('id')}:{event_type}",
                    outcome=event_type,
                    source="business_finance",
                    actions=[event_type],
                    failures=[],
                )
            except Exception:  # noqa: BLE001 - audit failure belongs to the audit layer
                pass
        if event_type in {"journal_reversed", "period_closed", "budget_approved", "management_report_generated"}:
            self._remember(venture, event_type)

    def _assert_no_secrets(self, value: Any) -> None:
        forbidden = ("password", "secret", "api_key", "apikey", "private_key", "card_number", "cvv", "access_token", "refresh_token")
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(token in lowered for token in forbidden):
                    raise FinOpsValidationError(
                        "Financial records must not contain credentials, card numbers or banking secrets."
                    )
                self._assert_no_secrets(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._assert_no_secrets(item)

    # -- venture management --------------------------------------------------

    def create_venture(self, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        # The service's configured actor is the internal analysis identity used
        # by the advisory agent. It gets a READ-ONLY membership so analysis of a
        # venture created through an HTTP principal (local/api-token) works
        # without granting the agent mutation rights or bypassing role checks.

        self._load()
        self._assert_no_secrets(payload)
        self._capacity(list(self._ventures.values()), self.max_ventures)
        who = self._actor(actor)
        name = text(payload.get("name"), "name", 160, required=True)
        model = str(payload.get("business_model") or "other").strip().lower()
        if model not in BUSINESS_MODELS:
            raise FinOpsValidationError(f"business_model must be one of: {', '.join(BUSINESS_MODELS)}")
        status = str(payload.get("operating_status") or "planning").lower()
        if status not in OPERATING_STATUSES:
            raise FinOpsValidationError(f"operating_status must be one of: {', '.join(OPERATING_STATUSES)}")
        currency = currency_code(payload.get("reporting_currency") or payload.get("currency") or "USD")
        now = self._clock()
        venture = new_venture(
            name=name,
            description=text(payload.get("description"), "description", 2000),
            business_model=model,
            products_services=strings(payload.get("products_services"), "products_services"),
            ownership=mapping(payload.get("ownership"), "ownership"),
            authorization=mapping(payload.get("authorization"), "authorization"),
            reporting_currency=currency,
            revenue_model=mapping(payload.get("revenue_model"), "revenue_model")
            if isinstance(payload.get("revenue_model"), Mapping)
            else strings(payload.get("revenue_model"), "revenue_model"),
            expense_structure=mapping(payload.get("expense_structure"), "expense_structure")
            if isinstance(payload.get("expense_structure"), Mapping)
            else strings(payload.get("expense_structure"), "expense_structure"),
            objectives=strings(payload.get("objectives"), "objectives"),
            linked_products=strings(payload.get("linked_products"), "linked_products"),
            customer_refs=strings(payload.get("customer_refs"), "customer_refs"),
            fiscal=fiscal_period(payload.get("fiscal_period")),
            actor=who,
            now=now,
        )
        if self.actor and self.actor != who and not any(
            str(member.get("actor")) == self.actor for member in venture["authorization"]["members"]
        ):
            venture["authorization"]["members"].append(
                {"actor": self.actor, "role": "viewer", "added_at": now.isoformat(), "added_by": who, "internal": True}
            )
        if payload.get("health_thresholds") is not None:
            supplied = mapping(payload.get("health_thresholds"), "health_thresholds")
            for key, value in supplied.items():
                if key in venture["health_thresholds"]:
                    venture["health_thresholds"][key] = _number(value, f"health_thresholds.{key}", minimum=0)
        self._ventures[venture["id"]] = venture
        self._event(venture, "venture_created", who, {"currency": currency, "business_model": model})
        self._persist()
        return self._public_venture(venture)

    def list_ventures(self, *, limit: int = 50, offset: int = 0, status: str = "") -> dict[str, Any]:
        self._load()
        rows = sorted(self._ventures.values(), key=lambda row: str(row.get("updated_at", "")), reverse=True)
        if status:
            rows = [row for row in rows if row.get("operating_status") == status]
        start = max(0, int(offset))
        bounded = max(1, min(int(limit), 100))
        return {
            "total": len(rows),
            "ventures": [self._venture_row(row) for row in rows[start : start + bounded]],
            "note": "Each venture has an isolated ledger and reporting currency.",
        }

    def _venture_row(self, venture: Mapping[str, Any]) -> dict[str, Any]:
        revenue = build_revenue_summary(venture, as_of=self._clock().date())
        return {
            "id": venture.get("id"),
            "name": venture.get("name"),
            "business_model": venture.get("business_model"),
            "operating_status": venture.get("operating_status"),
            "reporting_currency": venture.get("reporting_currency"),
            "recognized_revenue": revenue.get("recognized_revenue"),
            "cash_balance": money_value(signed_balance(venture, CASH_ACCOUNT)),
            "open_alerts": len([row for row in venture.get("health_alerts", []) if row.get("status") == "open"]),
            "updated_at": venture.get("updated_at"),
        }

    def _public_venture(self, venture: Mapping[str, Any]) -> dict[str, Any]:
        return dict(venture)

    def detail(self, venture_id: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who)
        today = self._clock().date()
        return {
            "venture": self._public_venture(venture),
            "summary": self._venture_row(venture),
            "operations": operations_dashboard(venture, today=today),
            "revenue_summary": build_revenue_summary(venture, as_of=today),
            "expense_summary": build_expense_summary(venture),
            "trial_balance": trial_balance(venture, as_of=today),
            "working_capital": build_working_capital(venture, as_of=today),
            "health": venture.get("health_history", [])[-1] if venture.get("health_history") else {},
            "links": self._linked_context(venture),
            "boundaries": {
                "payments": False,
                "purchases": False,
                "banking_changes": False,
                "ledger_mutation": False,
                "external_action_executed": False,
            },
        }

    def _linked_context(self, venture: Mapping[str, Any]) -> dict[str, Any]:
        products = []
        for product_id in venture.get("linked_products", [])[:10]:
            if self._products is None:
                products.append({"product_id": product_id, "available": False, "reason": "product reader not configured"})
                continue
            try:
                row = self._products(product_id)
                if inspect.isawaitable(row):
                    row.close() if hasattr(row, "close") else None
                    products.append({"product_id": product_id, "available": False, "reason": "product reader is asynchronous"})
                    continue
                summary = row.get("summary", {}) if isinstance(row, Mapping) else {}
                products.append({"product_id": product_id, "available": True, "name": summary.get("name", ""), "state": summary.get("state", "")})
            except Exception:  # noqa: BLE001 - a missing link is reported, not invented
                products.append({"product_id": product_id, "available": False, "reason": "linked product was not found"})
        customers = []
        for reference in venture.get("customer_refs", [])[:10]:
            customers.append({"customer_ref": reference, "reader": "phase37" if self._customers else "not configured"})
        return {"products": products, "customer_references": customers, "note": "These are read-only references; no Phase 35/36/37 record is copied into the ledger."}

    def set_operating_status(self, venture_id: str, status: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        value = str(status or "").lower()
        if value not in OPERATING_STATUSES:
            raise FinOpsValidationError(f"operating_status must be one of: {', '.join(OPERATING_STATUSES)}")
        venture["operating_status"] = value
        self._event(venture, "operating_status_changed", who, {"status": value})
        self._persist()
        return self._public_venture(venture)

    def add_member(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "admin")
        member = text(payload.get("actor"), "actor", 120, required=True)
        role = str(payload.get("role") or "viewer").lower()
        if role not in ROLES:
            raise FinOpsValidationError(f"role must be one of: {', '.join(ROLES)}")
        if member == venture.get("authorization", {}).get("owner"):
            raise FinOpsConflict("The owner's role cannot be replaced.")
        members = venture["authorization"].setdefault("members", [])
        for row in members:
            if row.get("actor") == member:
                row["role"] = role
                row["added_at"] = self._clock().isoformat()
                self._event(venture, "member_added", who, {"member": member, "role": role, "updated": True})
                self._persist()
                return {"members": members}
        members.append({"actor": member, "role": role, "added_at": self._clock().isoformat(), "added_by": who})
        self._event(venture, "member_added", who, {"member": member, "role": role})
        self._persist()
        return {"members": members}

    # -- operational management ---------------------------------------------

    def add_operational_objective(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        row = {
            "id": new_id("obj"),
            "title": text(payload.get("title"), "title", 200, required=True),
            "outcome": text(payload.get("outcome"), "outcome", 1000),
            "owner": text(payload.get("owner"), "owner", 120),
            "status": str(payload.get("status") or "open"),
            "priority": int(_number(payload.get("priority", 50), "priority", minimum=0, maximum=100)),
            "due_date": iso_date(optional_date(payload.get("due_date"), "due_date")),
            "linked_product": text(payload.get("linked_product"), "linked_product", 120),
            "created_at": self._clock().isoformat(),
        }
        if row["status"] not in {"open", "in_progress", "complete", "blocked", "cancelled"}:
            raise FinOpsValidationError("Unsupported objective status.")
        venture.setdefault("operational_objectives", []).append(row)
        self._event(venture, "objective_recorded", who, {"objective_id": row["id"]})
        self._persist()
        return row

    def add_operational_task(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        dependencies = strings(payload.get("dependencies"), "dependencies")
        known = {str(row.get("id")) for row in venture.get("operational_tasks", [])}
        missing = [item for item in dependencies if item not in known]
        if missing:
            raise FinOpsValidationError(f"Unknown task dependencies: {', '.join(missing)}")
        objective_id = text(payload.get("objective_id"), "objective_id", 120)
        if objective_id and objective_id not in {str(row.get("id")) for row in venture.get("operational_objectives", [])}:
            raise FinOpsValidationError("objective_id does not name a recorded operational objective.")
        task = {
            "id": new_id("task"),
            "title": text(payload.get("title"), "title", 240, required=True),
            "objective_id": objective_id,
            "owner": text(payload.get("owner"), "owner", 120),
            "status": str(payload.get("status") or "pending"),
            "priority": int(_number(payload.get("priority", 50), "priority", minimum=0, maximum=100)),
            "due_date": iso_date(optional_date(payload.get("due_date"), "due_date")),
            "dependencies": dependencies,
            "estimated_cost": money_text(optional_money(payload.get("estimated_cost"), "estimated_cost")),
            "actual_cost": money_text(optional_money(payload.get("actual_cost"), "actual_cost")),
            "resource_utilization": (
                _number(payload.get("resource_utilization"), "resource_utilization", minimum=0, maximum=1)
                if payload.get("resource_utilization") not in (None, "")
                else None
            ),
            "delivery_days": (
                _number(payload.get("delivery_days"), "delivery_days", minimum=0)
                if payload.get("delivery_days") not in (None, "")
                else None
            ),
            "linked_execution": mapping(payload.get("linked_execution"), "linked_execution"),
            "blocked_reason": text(payload.get("blocked_reason"), "blocked_reason", 500),
            "created_at": self._clock().isoformat(),
            "completed_at": "",
        }
        if task["status"] not in {"pending", "in_progress", "complete", "blocked", "cancelled"}:
            raise FinOpsValidationError("Unsupported task status.")
        if task["status"] == "complete":
            task["completed_at"] = self._clock().isoformat()
        venture.setdefault("operational_tasks", []).append(task)
        self._event(venture, "task_recorded", who, {"task_id": task["id"]})
        self._persist()
        return task

    def update_operational_task(self, venture_id: str, task_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        task = next((row for row in venture.get("operational_tasks", []) if row.get("id") == task_id), None)
        if task is None:
            raise FinOpsRecordNotFound("task", task_id)
        if "status" in payload:
            status = str(payload.get("status") or "")
            if status not in {"pending", "in_progress", "complete", "blocked", "cancelled"}:
                raise FinOpsValidationError("Unsupported task status.")
            task["status"] = status
            task["completed_at"] = self._clock().isoformat() if status == "complete" else ""
        for key in ("actual_cost", "estimated_cost"):
            if key in payload:
                task[key] = money_text(optional_money(payload.get(key), key))
        if "resource_utilization" in payload:
            task["resource_utilization"] = _number(payload["resource_utilization"], "resource_utilization", minimum=0, maximum=1)
        if "delivery_days" in payload:
            task["delivery_days"] = _number(payload["delivery_days"], "delivery_days", minimum=0)
        if "blocked_reason" in payload:
            task["blocked_reason"] = text(payload.get("blocked_reason"), "blocked_reason", 500)
        if "linked_execution" in payload:
            task["linked_execution"] = mapping(payload.get("linked_execution"), "linked_execution")
        self._event(venture, "task_updated", who, {"task_id": task_id, "status": task.get("status")})
        self._persist()
        return cast(dict[str, Any], task)

    def add_milestone(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        row = {
            "id": new_id("mile"),
            "title": text(payload.get("title"), "title", 200, required=True),
            "due_date": iso_date(optional_date(payload.get("due_date"), "due_date")),
            "status": str(payload.get("status") or "pending"),
            "owner": text(payload.get("owner"), "owner", 120),
            "created_at": self._clock().isoformat(),
        }
        venture.setdefault("milestones", []).append(row)
        self._event(venture, "milestone_recorded", who, {"milestone_id": row["id"]})
        self._persist()
        return row

    def add_workflow(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        row = {
            "id": new_id("flow"),
            "name": text(payload.get("name"), "name", 200, required=True),
            "steps": strings(payload.get("steps"), "steps"),
            "status": str(payload.get("status") or "draft"),
            "owner": text(payload.get("owner"), "owner", 120),
            "created_at": self._clock().isoformat(),
        }
        venture.setdefault("workflows", []).append(row)
        self._event(venture, "workflow_recorded", who, {"workflow_id": row["id"]})
        self._persist()
        return row

    def add_operational_risk(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        row = {
            "id": new_id("risk"),
            "title": text(payload.get("title"), "title", 240, required=True),
            "description": text(payload.get("description"), "description", 2000),
            "severity": str(payload.get("severity") or "medium").lower(),
            "likelihood": str(payload.get("likelihood") or "medium").lower(),
            "owner": text(payload.get("owner"), "owner", 120),
            "status": str(payload.get("status") or "open"),
            "created_at": self._clock().isoformat(),
        }
        if row["severity"] not in {"low", "medium", "high", "critical"}:
            raise FinOpsValidationError("severity must be low, medium, high or critical.")
        venture.setdefault("operational_risks", []).append(row)
        self._event(venture, "risk_recorded", who, {"risk_id": row["id"]})
        self._persist()
        return row

    def operations(self, venture_id: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who)
        return operations_dashboard(venture, today=self._clock().date())

    # -- revenue -------------------------------------------------------------

    def _currency_conversion(self, venture: Mapping[str, Any], payload: Mapping[str, Any], *, label: str) -> tuple[str, Decimal, str]:
        currency = currency_code(payload.get("currency") or venture.get("reporting_currency"))
        rate_raw = payload.get("fx_rate_to_reporting")
        if currency == venture.get("reporting_currency"):
            if rate_raw not in (None, "", 1, 1.0, "1", "1.0"):
                raise FinOpsValidationError("fx_rate_to_reporting must be omitted when the currency already matches the reporting currency.")
            return currency, Decimal("1"), ""
        if rate_raw in (None, ""):
            raise FinOpsValidationError(
                f"{label} is in {currency}, but the venture reports in {venture.get('reporting_currency')}; provide fx_rate_to_reporting and its source."
            )
        rate = parse_money(rate_raw, "fx_rate_to_reporting")
        source = text(payload.get("fx_rate_source"), "fx_rate_source", 240)
        if not source:
            raise FinOpsValidationError("A non-reporting-currency record requires fx_rate_source.")
        return currency, rate, source

    def _find_idempotent(self, venture: Mapping[str, Any], key: str) -> dict[str, Any] | None:
        if not key:
            return None
        for collection in ("revenue_records", "expense_records", "journal_entries"):
            for row in venture.get(collection, []):
                if str(row.get("idempotency_key") or "") == key:
                    return cast(dict[str, Any], row)
        return None

    def add_revenue(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        self._assert_no_secrets(payload)
        self._capacity(venture.setdefault("revenue_records", []), self.max_records)
        key = text(payload.get("idempotency_key"), "idempotency_key", 200)
        existing = self._find_idempotent(venture, key)
        if existing is not None:
            return {**existing, "deduplicated": True}
        currency, rate, fx_source = self._currency_conversion(venture, payload, label="revenue record")
        gross = parse_money(payload.get("gross_amount", payload.get("amount")), "gross_amount")
        discount = optional_money(payload.get("discount"), "discount")
        tax = optional_money(payload.get("tax"), "tax")
        refund = optional_money(payload.get("refund"), "refund")
        if discount > gross:
            raise FinOpsValidationError("discount cannot exceed gross_amount.")
        net = gross - discount - refund
        if net <= 0:
            raise FinOpsValidationError("net revenue must be greater than zero.")
        stage = str(payload.get("stage") or "booked").lower()
        if stage not in REVENUE_STAGES:
            raise FinOpsValidationError(f"stage must be one of: {', '.join(REVENUE_STAGES)}")
        source = str(payload.get("source") or "operator_entry")
        if source not in REVENUE_SOURCES:
            raise FinOpsValidationError(f"source must be one of: {', '.join(REVENUE_SOURCES)}")
        customer_ref = text(payload.get("customer_ref"), "customer_ref", 120)
        if source == "phase37_customer" and not customer_ref:
            raise FinOpsValidationError("A Phase 37 revenue record requires customer_ref.")
        source_ref = text(payload.get("source_ref"), "source_ref", 240)
        if source_ref:
            for row in venture.get("revenue_records", []):
                if row.get("source") == source and str(row.get("source_ref")) == source_ref:
                    raise FinOpsConflict(
                        "A revenue record with this source reference already exists; use idempotency_key only for a retry."
                    )
        transaction_date = parse_date(payload.get("transaction_date") or payload.get("date"), "transaction_date")
        booked_date = parse_date(payload.get("booked_date") or transaction_date, "booked_date")
        invoice_date = optional_date(payload.get("invoice_date"), "invoice_date")
        recognition_date = optional_date(payload.get("recognition_date"), "recognition_date")
        collection_date = optional_date(payload.get("collection_date"), "collection_date")
        if stage in {"invoiced", "recognized", "collected"} and invoice_date is None:
            invoice_date = transaction_date
        if stage in {"recognized", "collected"} and recognition_date is None:
            recognition_date = transaction_date
        if stage == "collected" and collection_date is None:
            collection_date = transaction_date
        row = {
            "id": new_id("rev"),
            "venture_id": venture.get("id"),
            "gross_amount": money_text(gross),
            "discount": money_text(discount),
            "tax": money_text(tax),
            "refund": money_text(refund),
            "currency": currency,
            "fx_rate_to_reporting": money_text(rate),
            "fx_rate_source": fx_source,
            "reporting_gross": money_text(gross * rate),
            "reporting_discount": money_text(discount * rate),
            "reporting_tax": money_text(tax * rate),
            "reporting_refund": money_text(refund * rate),
            "reporting_net": money_text(net * rate),
            "stage": stage,
            "status": "active",
            "booked_date": booked_date.isoformat(),
            "invoice_date": iso_date(invoice_date),
            "recognition_date": iso_date(recognition_date),
            "collection_date": iso_date(collection_date),
            "invoice_number": text(payload.get("invoice_number"), "invoice_number", 120),
            "due_date": iso_date(optional_date(payload.get("due_date"), "due_date")),
            "product_ref": text(payload.get("product_ref"), "product_ref", 120),
            "customer_ref": customer_ref,
            "source": source,
            "source_ref": source_ref,
            "recurring": bool(payload.get("recurring", False)),
            "description": text(payload.get("description"), "description", 500),
            "idempotency_key": key,
            "created_at": self._clock().isoformat(),
            "created_by": who,
        }
        entries = self._revenue_entry_plan(venture, row, stage, who)
        venture.setdefault("revenue_records", []).append(row)
        venture.setdefault("journal_entries", []).extend(entries)
        self._event(venture, "revenue_recorded", who, {"revenue_id": row["id"], "stage": stage, "reporting_net": row["reporting_net"]})
        self._persist()
        return {**row, "journal_entry_ids": [entry["id"] for entry in entries]}

    def _revenue_entry_plan(
        self,
        venture: Mapping[str, Any],
        row: Mapping[str, Any],
        target_stage: str,
        actor: str,
        *,
        from_stage: str = "booked",
    ) -> list[dict[str, Any]]:
        order = list(REVENUE_STAGES)
        target = order.index(target_stage)
        start = order.index(from_stage)
        net = Decimal(str(row["reporting_net"]))
        tax = Decimal(str(row["reporting_tax"]))
        total = net + tax
        entries: list[dict[str, Any]] = []
        if start < 1 <= target:
            entries.append(
                self._make_entry(
                    venture,
                    entry_date=parse_date(row["invoice_date"]),
                    memo=f"Invoice revenue {row['description'] or row['id']}",
                    lines=[
                        {"account_code": "1100", "debit": total, "credit": Decimal("0.00"), "product_ref": row.get("product_ref", ""), "customer_ref": row.get("customer_ref", "")},
                        {"account_code": "2100", "debit": Decimal("0.00"), "credit": net},
                        *([{"account_code": "2200", "debit": Decimal("0.00"), "credit": tax}] if tax > 0 else []),
                    ],
                    source="revenue",
                    source_ref=str(row["id"]),
                    idempotency_key=f"revenue:{row['id']}:invoiced",
                    cash_flow_category="operating",
                    product_ref=str(row.get("product_ref") or ""),
                    customer_ref=str(row.get("customer_ref") or ""),
                    actor=actor,
                )
            )
        if start < 2 <= target:
            entries.append(
                self._make_entry(
                    venture,
                    entry_date=parse_date(row["recognition_date"]),
                    memo=f"Recognize revenue {row['description'] or row['id']}",
                    lines=[
                        {"account_code": "2100", "debit": net, "credit": Decimal("0.00")},
                        {"account_code": "4000", "debit": Decimal("0.00"), "credit": net},
                    ],
                    source="revenue",
                    source_ref=str(row["id"]),
                    idempotency_key=f"revenue:{row['id']}:recognized",
                    cash_flow_category="operating",
                    product_ref=str(row.get("product_ref") or ""),
                    customer_ref=str(row.get("customer_ref") or ""),
                    actor=actor,
                )
            )
        if start < 3 <= target:
            entries.append(
                self._make_entry(
                    venture,
                    entry_date=parse_date(row["collection_date"]),
                    memo=f"Collect revenue {row['description'] or row['id']}",
                    lines=[
                        {"account_code": "1000", "debit": total, "credit": Decimal("0.00")},
                        {"account_code": "1100", "debit": Decimal("0.00"), "credit": total},
                    ],
                    source="revenue",
                    source_ref=str(row["id"]),
                    idempotency_key=f"revenue:{row['id']}:collected",
                    cash_flow_category="operating",
                    product_ref=str(row.get("product_ref") or ""),
                    customer_ref=str(row.get("customer_ref") or ""),
                    actor=actor,
                )
            )
        return entries

    def advance_revenue(self, venture_id: str, revenue_id: str, stage: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        row = next((item for item in venture.get("revenue_records", []) if item.get("id") == revenue_id), None)
        if row is None:
            raise FinOpsRecordNotFound("revenue", revenue_id)
        if row.get("status") == "cancelled":
            raise FinOpsConflict("A cancelled revenue record cannot be advanced.")
        target = str(stage or "").lower()
        if target not in REVENUE_STAGES:
            raise FinOpsValidationError(f"stage must be one of: {', '.join(REVENUE_STAGES)}")
        current = str(row.get("stage") or "booked")
        if REVENUE_STAGES.index(target) <= REVENUE_STAGES.index(current):
            raise FinOpsConflict(f"Revenue can only advance forward; it is already {current}.")
        today = self._clock().date()
        updated = dict(row)
        if target in {"invoiced", "recognized", "collected"} and not updated.get("invoice_date"):
            updated["invoice_date"] = today.isoformat()
        if target in {"recognized", "collected"} and not updated.get("recognition_date"):
            updated["recognition_date"] = today.isoformat()
        if target == "collected" and not updated.get("collection_date"):
            updated["collection_date"] = today.isoformat()
        updated["stage"] = target
        # Build first: a closed period or invalid account must leave the stored
        # record untouched, not half-advanced with dates that were never posted.
        entries = self._revenue_entry_plan(venture, updated, target, who, from_stage=current)
        row.update({key: updated[key] for key in ("invoice_date", "recognition_date", "collection_date", "stage")})
        venture.setdefault("journal_entries", []).extend(entries)
        self._event(venture, "revenue_advanced", who, {"revenue_id": revenue_id, "stage": target})
        self._persist()
        return {**row, "journal_entry_ids": [entry["id"] for entry in entries]}

    def refund_revenue(self, venture_id: str, revenue_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        row = next((item for item in venture.get("revenue_records", []) if item.get("id") == revenue_id), None)
        if row is None:
            raise FinOpsRecordNotFound("revenue", revenue_id)
        if row.get("stage") not in {"recognized", "collected"}:
            raise FinOpsConflict("A refund can only be recorded after revenue is recognized or collected.")
        amount = parse_money(payload.get("amount"), "refund amount")
        reason = text(payload.get("reason"), "reason", 500, required=True)
        entry_date = parse_date(payload.get("date") or self._clock().date().isoformat())
        # Retries must not double-refund. A caller-supplied key is authoritative;
        # without one, the same refund (record, amount, reason, date) is the same
        # attempt. Two genuinely identical refunds need distinct keys. Checked
        # before the limit so replaying the final refund succeeds, not conflicts.
        refund_key = text(payload.get("idempotency_key"), "idempotency_key", 200) or (
            f"refund:{revenue_id}:{money_text(amount)}:{entry_date.isoformat()}:{reason}"
        )
        existing_entry = next(
            (item for item in venture.get("journal_entries", []) if item.get("idempotency_key") == refund_key),
            None,
        )
        if existing_entry is not None:
            if existing_entry.get("source") != "refund" or existing_entry.get("source_ref") != revenue_id:
                raise FinOpsConflict("That idempotency_key already belongs to another journal entry.")
            return {**row, "journal_entry_ids": [existing_entry["id"]], "deduplicated": True}
        already = Decimal(str(row.get("reporting_refund", "0.00")))
        net = Decimal(str(row.get("reporting_net", "0.00")))
        if already + amount > net:
            raise FinOpsValidationError("The refund cannot exceed the recorded net revenue.")
        entry = self._make_entry(
            venture,
            entry_date=entry_date,
            memo=f"Refund {revenue_id}: {reason}",
            lines=[
                {"account_code": "4100", "debit": amount, "credit": Decimal("0.00")},
                {"account_code": "1000", "debit": Decimal("0.00"), "credit": amount},
            ],
            source="refund",
            source_ref=revenue_id,
            idempotency_key=refund_key,
            cash_flow_category="operating",
            product_ref=str(row.get("product_ref") or ""),
            customer_ref=str(row.get("customer_ref") or ""),
            actor=who,
        )
        row["reporting_refund"] = money_text(already + amount)
        row["refund_reason"] = reason
        # Keep each refund's date so an as-of revenue summary does not include a
        # future refund in today's totals (the running total is undated).
        events = row.setdefault("refund_events", [])
        if isinstance(events, list):
            events.append(
                {
                    "amount": money_text(amount),
                    "date": entry_date.isoformat(),
                    "reason": reason,
                    "journal_id": entry["id"],
                }
            )
        venture.setdefault("journal_entries", []).append(entry)
        self._event(venture, "refund_recorded", who, {"revenue_id": revenue_id, "amount": money_text(amount)})
        self._persist()
        return {**row, "journal_entry_ids": [entry["id"]]}

    def list_revenue(self, venture_id: str, *, actor: str | None = None) -> list[dict[str, Any]]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return list(venture.get("revenue_records", []))

    def revenue_summary(self, venture_id: str, *, actor: str | None = None, as_of: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return build_revenue_summary(venture, as_of=as_of or self._clock().date())

    # -- expenses ------------------------------------------------------------

    def add_expense(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        self._assert_no_secrets(payload)
        self._capacity(venture.setdefault("expense_records", []), self.max_records)
        key = text(payload.get("idempotency_key"), "idempotency_key", 200)
        existing = self._find_idempotent(venture, key)
        if existing is not None:
            return {**existing, "deduplicated": True}
        category = str(payload.get("category") or "other").lower()
        if category not in EXPENSE_CATEGORIES:
            raise FinOpsValidationError(f"category must be one of: {', '.join(EXPENSE_CATEGORIES)}")
        status = str(payload.get("status") or "actual").lower()
        if status not in EXPENSE_STATUSES:
            raise FinOpsValidationError(f"status must be one of: {', '.join(EXPENSE_STATUSES)}")
        # An actual is not paid just because it is actual. Paid status needs
        # explicit payment evidence; defaulting to paid invented cash outflows.
        payment_status = str(
            payload.get("payment_status") or ("unpaid" if status == "actual" else "not_applicable")
        ).lower()
        if payment_status not in PAYMENT_STATUSES:
            raise FinOpsValidationError(f"payment_status must be one of: {', '.join(PAYMENT_STATUSES)}")
        if status != "actual" and payment_status == "paid":
            raise FinOpsValidationError("Only an actual expense can be marked paid.")
        currency, rate, fx_source = self._currency_conversion(venture, payload, label="expense record")
        amount = parse_money(payload.get("amount"), "amount")
        source_ref = text(payload.get("source_ref"), "source_ref", 240)
        source = text(payload.get("source"), "source", 120) or "operator_entry"
        if source_ref:
            for row in venture.get("expense_records", []):
                if row.get("source") == source and str(row.get("source_ref")) == source_ref:
                    raise FinOpsConflict("An expense with this source reference already exists.")
        row = {
            "id": new_id("exp"),
            "venture_id": venture.get("id"),
            "category": category,
            "account_code": EXPENSE_CATEGORY_ACCOUNTS[category],
            "amount": money_text(amount),
            "currency": currency,
            "fx_rate_to_reporting": money_text(rate),
            "fx_rate_source": fx_source,
            "reporting_amount": money_text(amount * rate),
            "transaction_date": parse_date(payload.get("transaction_date") or payload.get("date"), "transaction_date").isoformat(),
            "payment_status": payment_status,
            "status": status,
            "approval_status": "pending",
            "approval_note": "",
            "approved_by": "",
            "approved_at": "",
            "source": source,
            "supporting_reference": text(payload.get("supporting_reference"), "supporting_reference", 300),
            "source_ref": source_ref,
            "vendor": text(payload.get("vendor"), "vendor", 160),
            "department": text(payload.get("department"), "department", 120),
            "product_ref": text(payload.get("product_ref"), "product_ref", 120),
            "recurrence": mapping(payload.get("recurrence"), "recurrence"),
            "description": text(payload.get("description"), "description", 500),
            "paid_amount": money_text(Decimal("0.00")),
            "idempotency_key": key,
            "created_at": self._clock().isoformat(),
            "created_by": who,
        }
        venture.setdefault("expense_records", []).append(row)
        self._event(venture, "expense_recorded", who, {"expense_id": row["id"], "status": status, "reporting_amount": row["reporting_amount"]})
        self._persist()
        return row

    def approve_expense(self, venture_id: str, expense_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        row = next((item for item in venture.get("expense_records", []) if item.get("id") == expense_id), None)
        if row is None:
            raise FinOpsRecordNotFound("expense", expense_id)
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise FinOpsValidationError("approved must be true or false.")
        note = text(payload.get("note"), "note", 500, required=approved)
        # Do not short-circuit an approved record: a previous failure may have
        # left the flag set without its accrual. The existing-entry check below
        # posts at most once, so re-approval is safe and repairs that state.
        pending_entry: dict[str, Any] | None = None
        if approved and row.get("status") == "actual":
            existing = next(
                (entry for entry in venture.get("journal_entries", []) if entry.get("idempotency_key") == f"expense:{expense_id}:accrual"),
                None,
            )
            if existing is None:
                amount = Decimal(str(row["reporting_amount"]))
                account = str(row["account_code"])
                credit_account = CASH_ACCOUNT if row.get("payment_status") == "paid" else "2000"
                pending_entry = self._make_entry(
                    venture,
                    entry_date=parse_date(row["transaction_date"]),
                    memo=f"Approve expense {row.get('vendor') or expense_id}",
                    lines=[
                        {"account_code": account, "debit": amount, "credit": Decimal("0.00"), "product_ref": row.get("product_ref", "")},
                        {"account_code": credit_account, "debit": Decimal("0.00"), "credit": amount},
                    ],
                    source="expense",
                    source_ref=expense_id,
                    idempotency_key=f"expense:{expense_id}:accrual",
                    cash_flow_category="operating",
                    product_ref=str(row.get("product_ref") or ""),
                    actor=who,
                )
        # Commit the approval only after the accrual entry validated. The old
        # order left approval_status=approved in memory when posting failed, so
        # a retry short-circuited and the accrual was never posted.
        row["approval_status"] = "approved" if approved else "rejected"
        row["approval_note"] = note
        row["approved_by"] = who
        row["approved_at"] = self._clock().isoformat()
        if pending_entry is not None:
            venture.setdefault("journal_entries", []).append(pending_entry)
            if row.get("payment_status") == "paid":
                row["paid_amount"] = row["reporting_amount"]
        self._event(venture, "expense_approved", who, {"expense_id": expense_id, "approved": approved})
        self._persist()
        return cast(dict[str, Any], row)

    def pay_expense(self, venture_id: str, expense_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        row = next((item for item in venture.get("expense_records", []) if item.get("id") == expense_id), None)
        if row is None:
            raise FinOpsRecordNotFound("expense", expense_id)
        if row.get("status") != "actual" or row.get("approval_status") != "approved":
            raise FinOpsConflict("Only an approved actual expense can be paid.")
        total = Decimal(str(row["reporting_amount"]))
        paid = Decimal(str(row.get("paid_amount", "0.00")))
        remaining = total - paid
        supplied_key = text(payload.get("idempotency_key"), "idempotency_key", 200)
        if remaining <= 0 and not supplied_key:
            # A retry of the final payment often omits the amount, leaving no
            # key to derive. Return the single recorded payment instead of
            # failing with "already paid". A deliberate extra payment remains a
            # conflict because a supplied key cannot match an earlier entry.
            recorded = [
                item
                for item in venture.get("journal_entries", [])
                if item.get("source") == "expense_payment" and item.get("source_ref") == expense_id
            ]
            if len(recorded) == 1 and not payload.get("amount"):
                return {**row, "journal_entry_ids": [recorded[0]["id"]], "deduplicated": True}
            raise FinOpsConflict("This expense is already paid.")
        if supplied_key:
            existing = next(
                (item for item in venture.get("journal_entries", []) if item.get("idempotency_key") == supplied_key),
                None,
            )
            if existing is not None:
                if existing.get("source") != "expense_payment" or existing.get("source_ref") != expense_id:
                    raise FinOpsConflict("That idempotency_key already belongs to another journal entry.")
                return {**row, "journal_entry_ids": [existing["id"]], "deduplicated": True}
        amount = parse_money(payload.get("amount", remaining), "payment amount")
        if amount > remaining:
            raise FinOpsValidationError("Payment cannot exceed the remaining expense.")
        entry_date = parse_date(payload.get("date") or self._clock().date().isoformat())
        # Same retry contract as refunds: a supplied key wins, otherwise the
        # same expense/amount/date is the same payment attempt. Distinct
        # same-day equal instalments must carry distinct keys.
        payment_key = supplied_key or (
            f"expense:{expense_id}:payment:{money_text(amount)}:{entry_date.isoformat()}"
        )
        existing_entry = next(
            (item for item in venture.get("journal_entries", []) if item.get("idempotency_key") == payment_key),
            None,
        )
        if existing_entry is not None:
            if existing_entry.get("source") != "expense_payment" or existing_entry.get("source_ref") != expense_id:
                raise FinOpsConflict("That idempotency_key already belongs to another journal entry.")
            return {**row, "journal_entry_ids": [existing_entry["id"]], "deduplicated": True}
        if remaining <= 0:
            raise FinOpsConflict("This expense is already paid.")
        entry = self._make_entry(
            venture,
            entry_date=entry_date,
            memo=f"Pay expense {row.get('vendor') or expense_id}",
            lines=[
                {"account_code": "2000", "debit": amount, "credit": Decimal("0.00")},
                {"account_code": "1000", "debit": Decimal("0.00"), "credit": amount},
            ],
            source="expense_payment",
            source_ref=expense_id,
            idempotency_key=payment_key,
            cash_flow_category="operating",
            product_ref=str(row.get("product_ref") or ""),
            actor=who,
        )
        row["paid_amount"] = money_text(paid + amount)
        row["payment_status"] = "paid" if paid + amount >= total else "partial"
        row["paid_at"] = entry["date"]
        venture.setdefault("journal_entries", []).append(entry)
        self._event(venture, "expense_paid", who, {"expense_id": expense_id, "amount": money_text(amount)})
        self._persist()
        return {**row, "journal_entry_ids": [entry["id"]]}

    def list_expenses(self, venture_id: str, *, actor: str | None = None) -> list[dict[str, Any]]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return list(venture.get("expense_records", []))

    def expense_summary(self, venture_id: str, *, actor: str | None = None, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return build_expense_summary(venture, start=start, end=end)

    # -- journal, reconciliation and closing ---------------------------------

    def _make_entry(
        self,
        venture: Mapping[str, Any],
        *,
        entry_date: date,
        memo: str,
        lines: Sequence[Mapping[str, Any]],
        source: str,
        source_ref: str,
        idempotency_key: str,
        cash_flow_category: str,
        actor: str,
        product_ref: str = "",
        customer_ref: str = "",
        reverses: str = "",
    ) -> dict[str, Any]:
        if any(
            period.get("status") == "closed"
            and period.get("start")
            and period.get("end")
            and str(period["start"]) <= entry_date.isoformat() <= str(period["end"])
            for period in venture.get("periods", [])
        ):
            raise PeriodClosed(f"The financial period containing {entry_date.isoformat()} is closed; post a reversal in an open period instead.")
        normalised: list[dict[str, Any]] = []
        debit_total = Decimal("0.00")
        credit_total = Decimal("0.00")
        for raw in lines:
            account_code = str(raw.get("account_code") or "").strip()
            account = next((row for row in venture.get("accounts", []) if str(row.get("code")) == account_code), None)
            if account is None:
                raise FinOpsValidationError(f"Unknown account code: {account_code}")
            debit = optional_money(raw.get("debit"), "debit")
            credit = optional_money(raw.get("credit"), "credit")
            if (debit > 0) == (credit > 0):
                raise FinOpsValidationError("Each journal line must have exactly one positive debit or credit.")
            debit_total += debit
            credit_total += credit
            normalised.append(
                {
                    "account_code": account_code,
                    "debit": money_text(debit),
                    "credit": money_text(credit),
                    "product_ref": str(raw.get("product_ref") or product_ref or ""),
                    "customer_ref": str(raw.get("customer_ref") or customer_ref or ""),
                }
            )
        if len(normalised) < 2:
            raise FinOpsValidationError("A journal entry needs at least two lines.")
        if debit_total != credit_total:
            raise FinOpsValidationError(f"Journal entry is not balanced: debits {debit_total}, credits {credit_total}.")
        if cash_flow_category not in {"operating", "investing", "financing"}:
            raise FinOpsValidationError("cash_flow_category must be operating, investing or financing.")
        return {
            "id": new_id("je"),
            "venture_id": venture.get("id"),
            "date": entry_date.isoformat(),
            "memo": memo,
            "lines": normalised,
            "source": source,
            "source_ref": source_ref,
            "currency": venture.get("reporting_currency"),
            "idempotency_key": idempotency_key,
            "cash_flow_category": cash_flow_category,
            "product_ref": product_ref,
            "customer_ref": customer_ref,
            "reverses": reverses,
            "posted_at": self._clock().isoformat(),
            "posted_by": actor,
        }

    def post_journal(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        self._assert_no_secrets(payload)
        self._capacity(venture.setdefault("journal_entries", []), self.max_journal_entries)
        key = text(payload.get("idempotency_key"), "idempotency_key", 200)
        existing = self._find_idempotent(venture, key)
        if existing is not None:
            return {**existing, "deduplicated": True}
        currency = currency_code(payload.get("currency") or venture.get("reporting_currency"))
        if currency != venture.get("reporting_currency"):
            raise FinOpsValidationError("The journal is kept in the venture reporting currency; convert the record with a sourced rate first.")
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise FinOpsValidationError("lines must be a non-empty list.")
        entry = self._make_entry(
            venture,
            entry_date=parse_date(payload.get("date"), "date"),
            memo=text(payload.get("memo"), "memo", 500, required=True),
            lines=raw_lines,
            source=text(payload.get("source"), "source", 120) or "manual_journal",
            source_ref=text(payload.get("source_ref"), "source_ref", 240),
            idempotency_key=key,
            cash_flow_category=str(payload.get("cash_flow_category") or "operating"),
            product_ref=text(payload.get("product_ref"), "product_ref", 120),
            customer_ref=text(payload.get("customer_ref"), "customer_ref", 120),
            actor=who,
        )
        venture.setdefault("journal_entries", []).append(entry)
        self._event(venture, "journal_posted", who, {"journal_id": entry["id"], "memo": entry["memo"]})
        self._persist()
        return entry

    def journal(self, venture_id: str, *, actor: str | None = None, limit: int = 200) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        rows = list(venture.get("journal_entries", []))[-max(1, min(limit, 1000)) :]
        reversed_ids = {entry.get("reverses") for entry in venture.get("journal_entries", []) if entry.get("reverses")}
        for row in rows:
            row["status"] = "reversed" if row.get("id") in reversed_ids else "posted"
        return {"entries": rows, "total": len(venture.get("journal_entries", [])), "note": "Entries are append-only; a reversal is a new entry."}

    def reverse_journal(self, venture_id: str, journal_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        entry = next((row for row in venture.get("journal_entries", []) if row.get("id") == journal_id), None)
        if entry is None:
            raise FinOpsRecordNotFound("journal entry", journal_id)
        if entry.get("reverses"):
            raise FinOpsConflict("A reversal entry cannot itself be reversed through this route.")
        if any(row.get("reverses") == journal_id for row in venture.get("journal_entries", [])):
            raise FinOpsConflict("This journal entry is already reversed.")
        reason = text(payload.get("reason"), "reason", 500, required=True)
        lines = [
            {
                "account_code": line["account_code"],
                "debit": line["credit"],
                "credit": line["debit"],
                "product_ref": line.get("product_ref", ""),
                "customer_ref": line.get("customer_ref", ""),
            }
            for line in entry.get("lines", [])
        ]
        reversal = self._make_entry(
            venture,
            entry_date=parse_date(payload.get("date") or self._clock().date().isoformat()),
            memo=f"Reversal of {journal_id}: {reason}",
            lines=lines,
            source="reversal",
            source_ref=journal_id,
            idempotency_key=f"reversal:{journal_id}",
            cash_flow_category=str(entry.get("cash_flow_category") or "operating"),
            product_ref=str(entry.get("product_ref") or ""),
            customer_ref=str(entry.get("customer_ref") or ""),
            actor=who,
            reverses=journal_id,
        )
        venture.setdefault("journal_entries", []).append(reversal)
        self._event(venture, "journal_reversed", who, {"journal_id": journal_id, "reversal_id": reversal["id"], "reason": reason})
        self._persist()
        return {"original": {**entry, "status": "reversed"}, "reversal": reversal}

    def reconcile(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        code = text(payload.get("account_code"), "account_code", 20, required=True)
        if not any(str(row.get("code")) == code for row in venture.get("accounts", [])):
            raise FinOpsValidationError(f"Unknown account code: {code}")
        as_of = parse_date(payload.get("as_of") or self._clock().date().isoformat(), "as_of")
        raw_statement = payload.get("statement_balance")
        try:
            statement_balance = parse_money(raw_statement, "statement_balance", allow_negative=True, allow_zero=True)
        except ValueError as exc:
            raise FinOpsValidationError(str(exc)) from exc
        ledger_balance = signed_balance(venture, code, as_of=as_of)
        difference = statement_balance - ledger_balance
        row = {
            "id": new_id("rec"),
            "account_code": code,
            "as_of": as_of.isoformat(),
            "statement_balance": money_value(statement_balance),
            "ledger_balance": money_value(ledger_balance),
            "difference": money_value(difference),
            "status": "matched" if abs(difference) <= Decimal("0.01") else "unmatched",
            "note": text(payload.get("note"), "note", 500),
            "reconciled_by": who,
            "created_at": self._clock().isoformat(),
        }
        venture.setdefault("reconciliations", []).append(row)
        self._event(venture, "reconciliation_recorded", who, {"account_code": code, "status": row["status"]})
        self._persist()
        return row

    def close_period(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        start = parse_date(payload.get("start"), "start")
        end = parse_date(payload.get("end"), "end")
        if end < start:
            raise FinOpsValidationError("end cannot be before start.")
        label = text(payload.get("label"), "label", 120) or f"{start.isoformat()}..{end.isoformat()}"
        for period in venture.get("periods", []):
            if period.get("status") != "closed":
                continue
            if str(period.get("start")) <= end.isoformat() and start.isoformat() <= str(period.get("end")):
                raise FinOpsConflict("The requested period overlaps an already closed period.")
        statement = income_statement(venture, start=start, end=end)
        row = {
            "id": new_id("period"),
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": "closed",
            "closed_at": self._clock().isoformat(),
            "closed_by": who,
            "snapshot": {
                "income_statement": statement,
                "trial_balance": trial_balance(venture, as_of=end),
            },
        }
        venture.setdefault("periods", []).append(row)
        self._event(venture, "period_closed", who, {"period_id": row["id"], "label": label})
        self._persist()
        return row

    def trial_balance(self, venture_id: str, *, actor: str | None = None, as_of: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return trial_balance(venture, as_of=as_of or self._clock().date())

    # -- statements and analytics -------------------------------------------

    def income_statement(self, venture_id: str, *, actor: str | None = None, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        today = self._clock().date()
        return income_statement(venture, start=start or today.replace(day=1), end=end or today)

    def balance_sheet(self, venture_id: str, *, actor: str | None = None, as_of: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return balance_sheet(venture, as_of=as_of or self._clock().date())

    def cash_flow(self, venture_id: str, *, actor: str | None = None, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        today = self._clock().date()
        return cash_flow_statement(venture, start=start or today.replace(day=1), end=end or today)

    def working_capital(self, venture_id: str, *, actor: str | None = None, as_of: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return build_working_capital(venture, as_of=as_of or self._clock().date())

    def profitability(self, venture_id: str, *, actor: str | None = None, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        today = self._clock().date()
        customer = self._customer_snapshot_sync(venture)
        return build_profitability(
            venture,
            start=start or today.replace(day=1),
            end=end or today,
            customer_snapshot=customer,
        )

    def period_comparison(self, venture_id: str, *, actor: str | None = None, period_a: Sequence[str] | None = None, period_b: Sequence[str] | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        today = self._clock().date()
        a_start, a_end = (parse_date(x) for x in (period_a or [today.replace(day=1).isoformat(), today.isoformat()]))
        b_end = a_start - timedelta(days=1)
        b_start = b_end.replace(day=1)
        if period_b:
            b_start, b_end = (parse_date(x) for x in period_b)
        first = income_statement(venture, start=a_start, end=a_end)
        second = income_statement(venture, start=b_start, end=b_end)
        return {
            "period_a": first,
            "period_b": second,
            "changes": {
                "revenue": round(float(first.get("revenue") or 0) - float(second.get("revenue") or 0), 2),
                "operating_profit": round(float(first.get("operating_profit") or 0) - float(second.get("operating_profit") or 0), 2),
                "net_profit": round(float(first.get("net_profit") or 0) - float(second.get("net_profit") or 0), 2),
            },
            "note": "Comparison is arithmetic over posted entries; it does not assert causation.",
        }

    # -- budgets -------------------------------------------------------------

    def _normalize_budget_lines(self, raw_lines: Any) -> list[dict[str, Any]]:
        """ONE validator for budget creation and revision.

        Both paths must apply identical line rules, but a revision must not
        create a transient budget (and a false budget_created event) on the way.
        """
        if not isinstance(raw_lines, list) or not raw_lines:
            raise FinOpsValidationError("A budget needs at least one line.")
        lines: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_lines, start=1):
            if not isinstance(raw, Mapping):
                raise FinOpsValidationError("Each budget line must be an object.")
            amount = parse_money(raw.get("amount"), f"lines[{index}].amount", allow_zero=True)
            lines.append(
                {
                    "id": new_id("bline"),
                    "label": text(raw.get("label"), "label", 200) or f"Line {index}",
                    "account_code": text(raw.get("account_code"), "account_code", 20),
                    "category": str(raw.get("category") or "other").lower(),
                    "product_ref": text(raw.get("product_ref"), "product_ref", 120),
                    "department": text(raw.get("department"), "department", 120),
                    "amount": money_text(amount),
                    "threshold_pct": _number(raw.get("threshold_pct", 1.0), "threshold_pct", minimum=0.01),
                }
            )
        return lines

    def create_budget(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        start = parse_date(payload.get("period_start"), "period_start")
        end = parse_date(payload.get("period_end"), "period_end")
        if end < start:
            raise FinOpsValidationError("period_end cannot be before period_start.")
        lines = self._normalize_budget_lines(payload.get("lines"))
        row = {
            "id": new_id("bud"),
            "venture_id": venture.get("id"),
            "name": text(payload.get("name"), "name", 200, required=True),
            "scope": str(payload.get("scope") or "venture").lower(),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "status": "draft",
            "version": 1,
            "revises": "",
            "lines": lines,
            "total_budget": money_text(sum(Decimal(str(line["amount"])) for line in lines)),
            "approved_by": "",
            "approval_note": "",
            "created_by": who,
            "created_at": self._clock().isoformat(),
        }
        if row["scope"] not in {"venture", "department", "category", "product", "marketing", "development", "infrastructure"}:
            raise FinOpsValidationError("Unsupported budget scope.")
        venture.setdefault("budgets", []).append(row)
        self._event(venture, "budget_created", who, {"budget_id": row["id"], "total": row["total_budget"]})
        self._persist()
        return row

    def approve_budget(self, venture_id: str, budget_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        budget = next((row for row in venture.get("budgets", []) if row.get("id") == budget_id), None)
        if budget is None:
            raise FinOpsRecordNotFound("budget", budget_id)
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise FinOpsValidationError("approved must be true or false.")
        note = text(payload.get("note"), "note", 500, required=True)
        budget["status"] = "approved" if approved else "rejected"
        budget["approved_by"] = who
        budget["approval_note"] = note
        budget["approved_at"] = self._clock().isoformat()
        self._event(venture, "budget_approved", who, {"budget_id": budget_id, "approved": approved})
        self._persist()
        return cast(dict[str, Any], budget)

    def revise_budget(self, venture_id: str, budget_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "approver")
        budget = next((row for row in venture.get("budgets", []) if row.get("id") == budget_id), None)
        if budget is None:
            raise FinOpsRecordNotFound("budget", budget_id)
        reason = text(payload.get("reason"), "reason", 500, required=True)
        revised = dict(budget)
        revised["id"] = new_id("bud")
        revised["status"] = "draft"
        revised["version"] = int(budget.get("version", 1)) + 1
        revised["revises"] = budget_id
        revised["revision_reason"] = reason
        revised["created_by"] = who
        revised["created_at"] = self._clock().isoformat()
        revised["approved_by"] = ""
        revised["approval_note"] = ""
        revised["approved_at"] = ""
        if isinstance(payload.get("lines"), list):
            # Revalidate through the same line rules rather than trusting a copy.
            # Build in place: create_budget would persist and emit a
            # budget_created event for a row that is immediately discarded.
            revised["lines"] = self._normalize_budget_lines(payload["lines"])
            revised["total_budget"] = money_text(
                sum(Decimal(str(line["amount"])) for line in revised["lines"])
            )
        venture.setdefault("budgets", []).append(revised)
        self._event(venture, "budget_revised", who, {"budget_id": revised["id"], "revises": budget_id})
        self._persist()
        return revised

    def list_budgets(self, venture_id: str, *, actor: str | None = None) -> list[dict[str, Any]]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return list(venture.get("budgets", []))

    def budget_report(self, venture_id: str, budget_id: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        budget = next((row for row in venture.get("budgets", []) if row.get("id") == budget_id), None)
        if budget is None:
            raise FinOpsRecordNotFound("budget", budget_id)
        start = parse_date(budget["period_start"])
        end = parse_date(budget["period_end"])
        expenses = [
            row
            for row in venture.get("expense_records", [])
            if row.get("status") == "actual"
            and row.get("approval_status") == "approved"
            and (transaction := parse_date(row.get("transaction_date")))
            and start <= transaction <= end
        ]
        lines = []
        total_actual = Decimal("0.00")
        total_budget = Decimal("0.00")
        for line in budget.get("lines", []):
            actual = Decimal("0.00")
            for expense in expenses:
                if expense.get("account_code") != line.get("account_code"):
                    continue
                if line.get("category") not in (None, "", "other") and expense.get("category") != line.get("category"):
                    continue
                if line.get("product_ref") and expense.get("product_ref") != line.get("product_ref"):
                    continue
                if line.get("department") and expense.get("department") != line.get("department"):
                    continue
                actual += Decimal(str(expense.get("reporting_amount", "0.00")))
            budget_amount = Decimal(str(line.get("amount", "0.00")))
            variance = budget_amount - actual
            total_actual += actual
            total_budget += budget_amount
            lines.append(
                {
                    "line_id": line.get("id"),
                    "label": line.get("label"),
                    "account_code": line.get("account_code"),
                    "category": line.get("category"),
                    "product_ref": line.get("product_ref"),
                    "budget": money_value(budget_amount),
                    "actual": money_value(actual),
                    "variance": money_value(variance),
                    "variance_pct": round(float(actual / budget_amount), 6) if budget_amount > 0 else None,
                    "remaining": money_value(variance),
                    "over_budget": actual > budget_amount * Decimal(str(line.get("threshold_pct", 1.0))),
                }
            )
        return {
            "budget_id": budget_id,
            "name": budget.get("name"),
            "status": budget.get("status"),
            "approved": budget.get("status") == "approved",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "currency": venture.get("reporting_currency"),
            "lines": lines,
            "total_budget": money_value(total_budget),
            "total_actual": money_value(total_actual),
            "total_variance": money_value(total_budget - total_actual),
            "available": budget.get("status") == "approved",
            "note": "Actuals include only approved actual expense records. Draft or rejected budgets are comparisons, not authorized spending.",
        }

    # -- forecasts and scenarios --------------------------------------------

    def create_forecast(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        horizon = int(_number(payload.get("horizon_months", 6), "horizon_months", minimum=1, maximum=36))
        scenario = str(payload.get("scenario") or "base").lower()
        if scenario not in {"conservative", "base", "optimistic", "custom"}:
            raise FinOpsValidationError("scenario must be conservative, base, optimistic or custom.")
        assumptions = mapping(payload.get("assumptions"), "assumptions")
        cash = money_value(signed_balance(venture, CASH_ACCOUNT))
        forecast = build_forecast(
            venture,
            horizon_months=horizon,
            scenario=scenario,
            assumptions=assumptions,
            as_of=self._clock().date(),
            cash_balance=cash,
        )
        forecast.update(
            {
                "id": new_id("fcst"),
                "venture_id": venture.get("id"),
                "created_by": who,
                "created_at": self._clock().isoformat(),
                "last_updated": self._clock().isoformat(),
                "assumptions_input": assumptions,
            }
        )
        venture.setdefault("forecasts", []).append(forecast)
        self._event(venture, "forecast_recorded", who, {"forecast_id": forecast["id"], "scenario": forecast.get("scenario")})
        self._persist()
        return forecast

    def list_forecasts(self, venture_id: str, *, actor: str | None = None) -> list[dict[str, Any]]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return list(venture.get("forecasts", []))

    def scenario_analysis(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        horizon = int(_number(payload.get("horizon_months", 6), "horizon_months", minimum=1, maximum=36))
        assumptions = mapping(payload.get("assumptions"), "assumptions")
        custom = mapping(payload.get("custom"), "custom")
        result = compare_scenarios(
            venture,
            horizon_months=horizon,
            assumptions=assumptions,
            as_of=self._clock().date(),
            cash_balance=money_value(signed_balance(venture, CASH_ACCOUNT)),
            custom=custom,
        )
        result.update(
            {
                "id": new_id("scen"),
                "venture_id": venture.get("id"),
                "created_by": who,
                "created_at": self._clock().isoformat(),
                "last_updated": self._clock().isoformat(),
            }
        )
        venture.setdefault("scenarios", []).append(result)
        self._event(venture, "scenario_analysis_recorded", who, {"scenario_id": result["id"]})
        self._persist()
        return result

    # -- health --------------------------------------------------------------

    def _customer_snapshot_sync(self, venture: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._customers is None:
            return None
        refs = list(venture.get("customer_refs", [])) or list(venture.get("linked_products", []))
        for ref in refs[:1]:
            try:
                result = self._customers(str(ref))
                if inspect.isawaitable(result):
                    # The synchronous API path cannot follow an async reader;
                    # report that as unavailable rather than blocking a request.
                    result.close() if hasattr(result, "close") else None
                    continue
                if isinstance(result, Mapping):
                    return dict(result)
            except Exception:  # noqa: BLE001 - missing Phase 37 state is not fabricated
                continue
        return None

    async def _customer_snapshot(self, venture: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._customers is None:
            return None
        refs = list(venture.get("customer_refs", [])) or list(venture.get("linked_products", []))
        for ref in refs[:1]:
            try:
                result = self._customers(str(ref))
                result = await _await_maybe(result)
                if isinstance(result, Mapping):
                    return dict(result)
            except Exception:  # noqa: BLE001 - missing Phase 37 state is not fabricated
                continue
        return None

    def refresh_health(self, venture_id: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        today = self._clock().date()
        customer = self._customer_snapshot_sync(venture)
        budget_reports = [
            self.budget_report(venture_id, row["id"], actor=who)
            for row in venture.get("budgets", [])
            if row.get("status") == "approved"
        ]
        previous = venture.get("health_history", [])[-1] if venture.get("health_history") else None
        result = evaluate_health(
            venture,
            today=today,
            customer_snapshot=customer,
            budget_reports=budget_reports,
            previous_history=previous,
        )
        result["venture_id"] = venture.get("id")
        result["refreshed_by"] = who
        result["refreshed_at"] = self._clock().isoformat()
        history = venture.setdefault("health_history", [])
        history.append(
            {
                "at": result["refreshed_at"],
                "as_of": result["as_of"],
                "data_fresh": result["data_fresh"],
                "metrics": result.get("metrics", {}),
                "alert_count": len([row for row in result.get("alerts", []) if row.get("severity") != "info"]),
            }
        )
        del history[:-24]
        alerts = venture.setdefault("health_alerts", [])
        for candidate in result.get("alerts", []):
            if candidate.get("severity") == "info":
                continue
            existing = next(
                (row for row in alerts if row.get("status") == "open" and row.get("metric") == candidate.get("metric")),
                None,
            )
            if existing is not None:
                existing["observed_value"] = candidate["observed_value"]
                existing["threshold"] = candidate["threshold"]
                existing["severity"] = candidate["severity"]
                existing["evidence"] = candidate["evidence"]
                existing["recommended_investigation"] = candidate["recommended_investigation"]
                existing["timestamp"] = candidate["timestamp"]
                existing["updates"] = list(existing.get("updates", []))[-9:] + [
                    {"at": candidate["timestamp"], "observed_value": candidate["observed_value"]}
                ]
            else:
                candidate["id"] = new_id("alert")
                alerts.append(candidate)
        del alerts[:-int(venture.get("resource_limits", {}).get("alerts", 500))]
        self._event(venture, "health_refreshed", who, {"alert_count": len(result.get("alerts", []))})
        self._persist()
        return {**result, "alerts": [row for row in result.get("alerts", []) if row.get("severity") != "info"], "stored_open_alerts": len([row for row in alerts if row.get("status") == "open"])}

    def list_alerts(self, venture_id: str, *, actor: str | None = None, status: str = "") -> list[dict[str, Any]]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        rows = list(venture.get("health_alerts", []))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def resolve_alert(self, venture_id: str, alert_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        alert = next((row for row in venture.get("health_alerts", []) if row.get("id") == alert_id), None)
        if alert is None:
            raise FinOpsRecordNotFound("alert", alert_id)
        note = text(payload.get("note"), "note", 500, required=True)
        alert["status"] = "resolved"
        alert["resolved_at"] = self._clock().isoformat()
        alert["resolved_by"] = who
        alert["resolution_note"] = note
        self._event(venture, "alert_resolved", who, {"alert_id": alert_id})
        self._persist()
        return cast(dict[str, Any], alert)

    # -- reports -------------------------------------------------------------

    def generate_management_report(self, venture_id: str, payload: Mapping[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        who = self._actor(actor)
        self._authorize(venture, who, "analyst")
        today = self._clock().date()
        start = parse_date(payload.get("period_start") or today.replace(day=1).isoformat(), "period_start")
        end = parse_date(payload.get("period_end") or today.isoformat(), "period_end")
        revenue = build_revenue_summary(venture, as_of=end)
        expenses = build_expense_summary(venture, start=start, end=end)
        profit = build_profitability(venture, start=start, end=end, customer_snapshot=self._customer_snapshot_sync(venture))
        capital = build_working_capital(venture, as_of=end)
        operations = operations_dashboard(venture, today=today)
        income = income_statement(venture, start=start, end=end)
        cash = cash_flow_statement(venture, start=start, end=end)
        budgets = [self.budget_report(venture_id, row["id"], actor=who) for row in venture.get("budgets", [])]
        forecast = venture.get("forecasts", [])[-1] if venture.get("forecasts") else {}
        risks = list(venture.get("operational_risks", []))
        open_alerts = [row for row in venture.get("health_alerts", []) if row.get("status") == "open"]
        recommendations: list[dict[str, Any]] = []
        if revenue.get("outstanding_invoices", 0) > 0:
            recommendations.append(
                {
                    "action": "Review outstanding invoices and their due dates.",
                    "evidence": [f"Outstanding invoices: {revenue['outstanding_invoices']} {revenue['currency']}.", f"Overdue receivables: {revenue['overdue_receivables']} {revenue['currency']}."],
                    "kind": "recommendation",
                    "requires_human_authorization": True,
                }
            )
        assigned = [
            row
            for row in revenue.get("by_customer", [])
            if str(row.get("customer_ref") or "") not in {"", "unassigned"}
        ]
        if revenue.get("recognized_revenue", 0) > 0 and assigned:
            top = max(assigned, key=lambda row: row["recognized_revenue"])
            share = top["recognized_revenue"] / revenue["recognized_revenue"]
            if share > float(venture.get("health_thresholds", {}).get("revenue_concentration", 0.6)):
                recommendations.append(
                    {
                        "action": "Diversify revenue away from the largest customer reference.",
                        "evidence": [f"{top['customer_ref']} is {round(share * 100, 1)}% of recognized revenue."],
                        "kind": "recommendation",
                        "requires_human_authorization": True,
                    }
                )
        if expenses.get("unapproved_actual", 0) > 0:
            recommendations.append(
                {
                    "action": "Review and approve or reject pending actual expenses.",
                    "evidence": [f"Unapproved actual expenses: {expenses['unapproved_actual']} {expenses['currency']}.", "They are excluded from actuals until approved."],
                    "kind": "recommendation",
                    "requires_human_authorization": True,
                }
            )
        report = {
            "id": new_id("report"),
            "venture_id": venture.get("id"),
            "title": text(payload.get("title"), "title", 200) or f"{venture.get('name')} management report",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "currency": venture.get("reporting_currency"),
            "executive_summary": (
                f"{venture.get('name')} recognized {revenue.get('recognized_revenue')} "
                f"{revenue.get('currency')} in the selected period. Approved actual expenses were "
                f"{expenses.get('actual')} {expenses.get('currency')}. "
                f"Net profit from posted entries was {profit.get('net_profit')} {profit.get('currency')}. "
                "All figures are actuals only where recorded; unavailable metrics remain unavailable."
            ),
            "revenue": revenue,
            "expenses": expenses,
            "income_statement": income,
            "cash_flow": cash,
            "profitability": profit,
            "working_capital": capital,
            "budgets": budgets,
            "forecast": forecast,
            "operations": operations,
            "risks": risks,
            "sections": [
                {
                    "key": "executive_summary",
                    "title": "Executive summary",
                    "kind": "observation",
                    "content": "Revenue, expenses, profit and cash are stated only from posted entries or approved actual expenses.",
                },
                {
                    "key": "revenue",
                    "title": "Revenue",
                    "kind": "actual",
                    "content": f"Recognized {revenue.get('recognized_revenue')}; collected {revenue.get('collected_cash')}; refunds {revenue.get('refunds')}. Booked and invoiced amounts are not revenue.",
                },
                {
                    "key": "profit_and_loss",
                    "title": "Profit and loss",
                    "kind": "actual",
                    "content": f"Revenue {income.get('revenue')}; gross profit {income.get('gross_profit')}; net profit {income.get('net_profit')}.",
                },
                {
                    "key": "cash",
                    "title": "Cash flow",
                    "kind": "actual",
                    "content": f"Net cash change {cash.get('net_change')}; closing cash {cash.get('closing_cash')}.",
                },
                {
                    "key": "working_capital",
                    "title": "Working capital",
                    "kind": "actual",
                    "content": f"Working capital {capital.get('working_capital')}; current ratio {capital.get('current_ratio', {}).get('value') if isinstance(capital.get('current_ratio'), Mapping) else None}.",
                },
                {
                    "key": "budgets",
                    "title": "Budget versus actual",
                    "kind": "actual",
                    "content": f"{len(budgets)} budget(s) recorded; only approved actual expenses are compared.",
                },
                {
                    "key": "forecast",
                    "title": "Forecast",
                    "kind": "forecast",
                    "content": (
                        "No forecast has been generated."
                        if not forecast
                        else f"{forecast.get('scenario')} scenario over {forecast.get('horizon_months')} month(s); modelled, not actual."
                    ),
                },
                {
                    "key": "operations",
                    "title": "Operational performance",
                    "kind": "observation",
                    "content": f"{operations.get('completed', 0)}/{operations.get('tasks', 0)} task(s) complete; {operations.get('blocked', 0)} blocked.",
                },
                {
                    "key": "risks",
                    "title": "Risks and alerts",
                    "kind": "observation",
                    "content": f"{len([row for row in risks if row.get('status') == 'open'])} open risk(s); {len(open_alerts)} open alert(s).",
                },
            ],
            "alerts": open_alerts,
            "recommendations": recommendations,
            "methodology": (
                "Built deterministically from posted journal entries, approved actual expenses, "
                "recognized revenue, recorded operations and Phase 37 customer analytics where available."
            ),
            "limitations": [
                "No payment, purchase, bank change or external financial commitment is executed.",
                "Forecasts are not included as actuals.",
                "Customer metrics are unavailable when Phase 37 has no recorded source.",
            ],
            "generated_by": who,
            "generated_at": self._clock().isoformat(),
        }
        venture.setdefault("management_reports", []).append(report)
        self._event(venture, "management_report_generated", who, {"report_id": report["id"]})
        self._persist()
        return report

    def list_reports(self, venture_id: str, *, actor: str | None = None) -> list[dict[str, Any]]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        return list(venture.get("management_reports", []))

    # -- dashboard and status ------------------------------------------------

    def dashboard(self, venture_id: str, *, actor: str | None = None) -> dict[str, Any]:
        venture = self._get(venture_id)
        self._authorize(venture, self._actor(actor))
        today = self._clock().date()
        revenue = build_revenue_summary(venture, as_of=today)
        expenses = build_expense_summary(venture)
        capital = build_working_capital(venture, as_of=today)
        profit = build_profitability(venture, start=today.replace(day=1), end=today, customer_snapshot=self._customer_snapshot_sync(venture))
        budgets = [self.budget_report(venture_id, row["id"], actor=actor) for row in venture.get("budgets", [])]
        latest_forecast = venture.get("forecasts", [])[-1] if venture.get("forecasts") else {}
        latest_health = venture.get("health_history", [])[-1] if venture.get("health_history") else {}
        return {
            "venture": self._venture_row(venture),
            "revenue": revenue,
            "expenses": expenses,
            "cash_flow": cash_flow_statement(venture, start=today.replace(day=1), end=today),
            "income_statement": income_statement(venture, start=today.replace(day=1), end=today),
            "balance_sheet": balance_sheet(venture, as_of=today),
            "trial_balance": trial_balance(venture, as_of=today),
            "working_capital": capital,
            "profitability": profit,
            "operations": operations_dashboard(venture, today=today),
            "monthly_series": monthly_series_pair(venture, months=6, as_of=today),
            "budgets": budgets,
            "forecast": latest_forecast,
            "health": latest_health,
            "open_alerts": [row for row in venture.get("health_alerts", []) if row.get("status") == "open"],
            "management_reports": len(venture.get("management_reports", [])),
            "boundaries": {
                "payments": False,
                "purchases": False,
                "banking_changes": False,
                "external_action_executed": False,
            },
            "legend": {
                "actual": "Posted ledger entries or approved actual expense records",
                "estimated": "Recorded estimates and ratios that are not posted actuals",
                "projected": "Committed/projected expense records, never incurred",
                "forecast": "Modelled scenario, never actual or recognized revenue",
            },
        }

    def status(self) -> dict[str, Any]:
        self._load()
        ventures = list(self._ventures.values())
        return {
            "service": "business_finance",
            "schema_version": SCHEMA_VERSION,
            "ventures": len(ventures),
            "journal_entries": sum(len(row.get("journal_entries", [])) for row in ventures),
            "revenue_records": sum(len(row.get("revenue_records", [])) for row in ventures),
            "expense_records": sum(len(row.get("expense_records", [])) for row in ventures),
            "open_alerts": sum(
                len([alert for alert in row.get("health_alerts", []) if alert.get("status") == "open"])
                for row in ventures
            ),
            "configured": {
                "customer_reader": self._customers is not None,
                "product_reader": self._products is not None,
                "opportunity_reader": self._opportunities is not None,
            },
            "limits": {
                "ventures": self.max_ventures,
                "records": self.max_records,
                "journal_entries": self.max_journal_entries,
            },
            "boundaries": {
                "payments": False,
                "purchases": False,
                "banking_integration": False,
                "ledger_mutation": False,
                "external_commitments": False,
            },
            "load_errors": list(self._load_errors),
        }


__all__ = ["FinOpsService", "NAMESPACE", "SCHEMA_VERSION"]
