"""Phase 39 portfolio registry, lifecycle gates and Phase 35–38 snapshots.

The registry is deliberately additive. A portfolio stores a reference to an
existing Phase 38 venture and never a duplicate ledger, product, customer or
opportunity. Snapshots are read-only projections with source timestamps; report
snapshots are the only place historical values are frozen.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from novacontrol.portfolio.errors import (
    ApprovalRequired,
    LifecycleGateError,
    PortfolioAuthorizationError,
    PortfolioConflict,
    PortfolioNotFound,
    PortfolioValidationError,
    PortfolioVentureNotFound,
)
from novacontrol.portfolio.models import (
    DEFAULT_EVALUATION_POLICIES,
    DEFAULT_MONITORING,
    LIFECYCLE_STATUSES,
    MANAGEMENT_LIFECYCLES,
    PORTFOLIO_STATUSES,
    ROLE_LEVEL,
    ROLES,
    bounded_rows,
    currency,
    new_id,
    number,
    optional_day,
    text,
    utcnow,
)

NAMESPACE = "portfolio_management"
SCHEMA_VERSION = 1


class PortfolioRegistry:
    """Durable portfolio registry and read-only source snapshot builder."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        finance: Any | None = None,
        products: Callable[[str], Any] | None = None,
        opportunities: Callable[[str], Any] | None = None,
        customers: Callable[[str], Any] | None = None,
        publish: Callable[[str, Mapping[str, Any]], Any] | None = None,
        audit: Callable[..., Any] | None = None,
        memory: Callable[..., Any] | None = None,
        knowledge: Callable[..., Any] | None = None,
        actor: str = "operator",
        clock: Callable[[], datetime] | None = None,
        max_portfolios: int = 25,
        max_ventures: int = 100,
        max_records: int = 500,
    ) -> None:
        self._store = store
        self._finance = finance
        self._products = products
        self._opportunities = opportunities
        self._customers = customers
        self._publish_callback = publish
        self._audit_callback = audit
        self._memory_callback = memory
        self._knowledge_callback = knowledge
        self.actor = (actor or "operator").strip()
        self._clock = clock or utcnow
        self.max_portfolios = max(1, int(max_portfolios))
        self.max_ventures = max(1, int(max_ventures))
        self.max_records = max(20, int(max_records))
        self._portfolios: dict[str, dict[str, Any]] = {}
        self._bindings: dict[str, str] = {}
        self._loaded = False
        self._load_errors: list[str] = []
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        payload = self._store.read(NAMESPACE)
        if not isinstance(payload, Mapping):
            return
        rows = payload.get("portfolios")
        if isinstance(rows, Mapping):
            for key, row in rows.items():
                if not isinstance(row, Mapping):
                    continue
                try:
                    portfolio = dict(row)
                    pid = str(portfolio.get("id") or key)
                    if not pid:
                        raise ValueError("portfolio id is missing")
                    portfolio["id"] = pid
                    self._portfolios[pid] = portfolio
                except Exception as exc:  # noqa: BLE001
                    self._load_errors.append(str(exc))
        bindings = payload.get("venture_bindings")
        if isinstance(bindings, Mapping):
            for venture_id, portfolio_id in bindings.items():
                if str(venture_id) and str(portfolio_id) in self._portfolios:
                    self._bindings[str(venture_id)] = str(portfolio_id)
        # Repair missing bindings and remove duplicate references. A stored
        # document with the same venture in two portfolios would otherwise be
        # counted twice by consolidation, contradicting the registration rule.
        for pid, portfolio in self._portfolios.items():
            kept: list[Any] = []
            seen: set[str] = set()
            for ref in portfolio.get("ventures") or []:
                if not isinstance(ref, Mapping) or not ref.get("id"):
                    kept.append(ref)
                    continue
                venture_id = str(ref["id"])
                bound = self._bindings.get(venture_id)
                if venture_id in seen:
                    self._load_errors.append(
                        f"duplicate venture reference {venture_id!r} in portfolio {pid!r} ignored"
                    )
                    continue
                if bound and bound != pid:
                    self._load_errors.append(
                        f"venture {venture_id!r} is already bound to portfolio {bound!r}; "
                        f"the duplicate in {pid!r} was ignored"
                    )
                    continue
                seen.add(venture_id)
                kept.append(ref)
                self._bindings.setdefault(venture_id, pid)
            portfolio["ventures"] = kept
        for portfolio in self._portfolios.values():
            self._normalise_portfolio(portfolio)

    def _normalise_portfolio(self, portfolio: dict[str, Any]) -> None:
        for key, default in (
            ("members", []),
            ("ventures", []),
            ("events", []),
            ("evaluations", []),
            ("evaluation_policies", list(DEFAULT_EVALUATION_POLICIES.values())),
            ("risks", []),
            ("manual_risks", []),
            ("risk_resolutions", []),
            ("allocation_batches", []),
            ("scenarios", []),
            ("recommendations", []),
            ("decisions", []),
            ("resources", []),
            ("dependencies", []),
            ("knowledge", []),
            ("alerts", []),
            ("reports", []),
            ("fx_rates", {}),
            ("objectives", []),
        ):
            expected = dict if isinstance(default, dict) else list
            if not isinstance(portfolio.get(key), expected):
                portfolio[key] = default
        if not isinstance(portfolio.get("monitoring"), Mapping):
            portfolio["monitoring"] = dict(DEFAULT_MONITORING)
        if not isinstance(portfolio.get("risk_preferences"), Mapping):
            portfolio["risk_preferences"] = {
                "max_single_venture_share": 0.50,
                "min_cash_runway_months": 3.0,
                "max_budget_overrun": 0.10,
            }
        if not isinstance(portfolio.get("resource_policy"), Mapping):
            portfolio["resource_policy"] = {
                "require_approval": True,
                "allow_automatic_reallocation": False,
            }
        portfolio.setdefault("base_currency", "USD")
        portfolio.setdefault("status", "active")
        portfolio.setdefault("reporting_period", {"kind": "monthly"})

    def _persist(self) -> None:
        if self._store is None:
            return
        self._store.write(
            NAMESPACE,
            {
                "schema_version": SCHEMA_VERSION,
                "portfolios": self._portfolios,
                "venture_bindings": self._bindings,
            },
        )

    # -- auth and common checks ---------------------------------------------
    def _actor(self, actor: str | None) -> str:
        return (actor or "").strip() or self.actor

    def _get(self, portfolio_id: str) -> dict[str, Any]:
        self._load()
        portfolio = self._portfolios.get(str(portfolio_id))
        if portfolio is None:
            raise PortfolioNotFound(str(portfolio_id))
        return portfolio

    @staticmethod
    def _member(portfolio: Mapping[str, Any], actor: str) -> dict[str, Any] | None:
        for row in portfolio.get("members") or []:
            if isinstance(row, Mapping) and str(row.get("actor")) == actor:
                return dict(row)
        return None

    def _role(self, portfolio: Mapping[str, Any], actor: str) -> str:
        if actor == str(portfolio.get("owner")):
            return "admin"
        member = self._member(portfolio, actor)
        return str(member.get("role") or "") if member else ""

    def _authorize(
        self,
        portfolio: Mapping[str, Any],
        actor: str,
        minimum: str = "viewer",
    ) -> str:
        role = self._role(portfolio, actor)
        if not role or ROLE_LEVEL.get(role, 0) < ROLE_LEVEL[minimum]:
            raise PortfolioAuthorizationError(
                f"{actor!r} needs the {minimum} portfolio role for {portfolio.get('id')!r}."
            )
        return role

    def _venture_ref(self, portfolio: Mapping[str, Any], venture_id: str) -> dict[str, Any]:
        for row in portfolio.get("ventures") or []:
            if isinstance(row, dict) and str(row.get("id")) == str(venture_id):
                return row
        raise PortfolioVentureNotFound(str(venture_id))

    def _can_view_venture(
        self, portfolio: Mapping[str, Any], actor: str, ref: Mapping[str, Any]
    ) -> bool:
        role = self._role(portfolio, actor)
        if role == "admin":
            return True
        if not role:
            return False
        if str(ref.get("visibility") or "portfolio") == "portfolio":
            return True
        allowed = {str(item) for item in ref.get("allowed_members") or []}
        return actor in allowed

    def _visible_refs(self, portfolio: Mapping[str, Any], actor: str) -> list[dict[str, Any]]:
        return [
            row
            for row in portfolio.get("ventures") or []
            if isinstance(row, dict) and self._can_view_venture(portfolio, actor, row)
        ]

    def _visible_venture_ids(self, portfolio: Mapping[str, Any], actor: str) -> set[str]:
        """The venture ids this actor may see, used to filter portfolio-owned records."""
        return {str(ref.get("id")) for ref in self._visible_refs(portfolio, actor)}

    def _portfolio_summary(self, portfolio: Mapping[str, Any]) -> dict[str, Any]:
        """A user-facing portfolio header without raw venture, alert or report records."""
        return {
            "id": portfolio.get("id"),
            "name": portfolio.get("name"),
            "description": portfolio.get("description", ""),
            "owner": portfolio.get("owner"),
            "base_currency": portfolio.get("base_currency"),
            "reporting_period": portfolio.get("reporting_period", {}),
            "status": portfolio.get("status"),
            "objectives": list(portfolio.get("objectives") or []),
            "monitoring": dict(portfolio.get("monitoring") or {}),
            "risk_preferences": dict(portfolio.get("risk_preferences") or {}),
            "resource_policy": dict(portfolio.get("resource_policy") or {}),
            "created_at": portfolio.get("created_at"),
            "updated_at": portfolio.get("updated_at"),
        }

    def _assert_no_secrets(self, value: Any) -> None:
        # Every persisted free-form payload also gets the customer-PII guard;
        # portfolio records carry references, not contact details.
        self._assert_no_customer_data(value)
        forbidden = (
            "password",
            "secret",
            "api_key",
            "apikey",
            "private_key",
            "card_number",
            "cvv",
            "access_token",
            "refresh_token",
            "bank_account",
        )
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(token in lowered for token in forbidden):
                    raise PortfolioValidationError(
                        "Portfolio records must not contain credentials or banking secrets."
                    )
                self._assert_no_secrets(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._assert_no_secrets(item)

    @staticmethod
    def _assert_no_customer_data(value: Any) -> None:
        forbidden = {"email", "phone", "address", "contact_email", "contact_phone"}
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).lower() in forbidden:
                    raise PortfolioValidationError(
                        "Do not copy customer contact data into portfolio knowledge; "
                        "store a reference and classification instead."
                    )
                PortfolioRegistry._assert_no_customer_data(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                PortfolioRegistry._assert_no_customer_data(item)

    @staticmethod
    def _capacity(rows: Sequence[Any], maximum: int, label: str) -> None:
        if len(rows) >= maximum:
            raise PortfolioConflict(f"{label} limit ({maximum}) has been reached.")

    def _remember(self, portfolio: Mapping[str, Any], event_type: str) -> None:
        if self._memory_callback is None:
            return
        try:
            result = self._memory_callback(
                key=f"portfolio:{portfolio.get('id')}:latest",
                value={
                    "portfolio_id": portfolio.get("id"),
                    "name": portfolio.get("name"),
                    "event": event_type,
                    "ventures": len(portfolio.get("ventures") or []),
                    "open_alerts": len(
                        [
                            row
                            for row in portfolio.get("alerts") or []
                            if isinstance(row, Mapping) and row.get("status") == "open"
                        ]
                    ),
                },
                text=f"{event_type} on portfolio {portfolio.get('name')}",
                importance=0.7,
            )
            if inspect.isawaitable(result):
                try:
                    import asyncio

                    asyncio.ensure_future(result)
                except RuntimeError:
                    result.close() if hasattr(result, "close") else None
        except Exception:  # noqa: BLE001
            pass

    def _ingest_knowledge(self, title: str, body: str, project: str) -> None:
        if self._knowledge_callback is None:
            return
        try:
            result = self._knowledge_callback(body, title)
            if inspect.isawaitable(result):
                try:
                    import asyncio

                    asyncio.ensure_future(result)
                except RuntimeError:
                    result.close() if hasattr(result, "close") else None
        except Exception:  # noqa: BLE001
            pass

    def _event(
        self,
        portfolio: dict[str, Any],
        event_type: str,
        actor: str,
        detail: Mapping[str, Any] | None = None,
        *,
        audit: bool = True,
    ) -> dict[str, Any]:
        now = self._clock()
        row = {
            "id": new_id("pevt"),
            "type": event_type,
            "portfolio_id": portfolio.get("id"),
            "at": now.isoformat(),
            "actor": actor,
            "detail": dict(detail or {}),
        }
        portfolio.setdefault("events", []).append(row)
        portfolio["events"] = portfolio["events"][-self.max_records :]
        portfolio["updated_at"] = now.isoformat()
        if self._publish_callback is not None:
            try:
                result = self._publish_callback(
                    f"portfolio.{event_type}",
                    {
                        "portfolio_id": portfolio.get("id"),
                        "event_id": row["id"],
                        "actor": actor,
                    },
                )
                if inspect.isawaitable(result):
                    try:
                        import asyncio

                        asyncio.ensure_future(result)
                    except RuntimeError:
                        result.close() if hasattr(result, "close") else None
            except Exception:  # noqa: BLE001
                pass
        if audit and self._audit_callback is not None:
            try:
                self._audit_callback(
                    task_id=str(portfolio.get("id")),
                    user_request=f"portfolio:{portfolio.get('id')}:{event_type}",
                    outcome=event_type,
                    source="portfolio_manager",
                    actions=[event_type],
                    failures=[],
                )
            except Exception:  # noqa: BLE001
                pass
        if event_type in {
            "decision_approved",
            "report_generated",
            "allocation_decided",
            "knowledge_promoted",
            "alerts_refreshed",
        }:
            self._remember(portfolio, event_type)
        return row

    def _row(
        self, portfolio: Mapping[str, Any], *, visible_venture_ids: set[str] | None = None
    ) -> dict[str, Any]:
        alerts = [
            row
            for row in portfolio.get("alerts") or []
            if isinstance(row, Mapping)
            and row.get("status") == "open"
            and (
                visible_venture_ids is None
                or not row.get("venture_id")
                or str(row.get("venture_id")) in visible_venture_ids
            )
        ]
        return {
            "id": portfolio.get("id"),
            "name": portfolio.get("name"),
            "description": portfolio.get("description", ""),
            "owner": portfolio.get("owner"),
            "base_currency": portfolio.get("base_currency"),
            "reporting_period": portfolio.get("reporting_period", {}),
            "status": portfolio.get("status"),
            "objectives": list(portfolio.get("objectives") or []),
            "ventures": len(portfolio.get("ventures") or []),
            "open_alerts": len(alerts),
            "created_at": portfolio.get("created_at"),
            "updated_at": portfolio.get("updated_at"),
        }

    # -- source readers ------------------------------------------------------
    @staticmethod
    def _awaitable_or_value(value: Any) -> Any:
        if inspect.isawaitable(value):
            try:
                value.close() if hasattr(value, "close") else None
            except Exception:  # noqa: BLE001
                pass
            return {}
        return value

    def _finance_call(
        self, method: str, *args: Any, actor: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if self._finance is None:
            return {}
        bound = getattr(self._finance, method, None)
        if bound is None:
            return {}
        call_kwargs = dict(kwargs)
        call_kwargs["actor"] = self._actor(actor)
        try:
            try:
                result = bound(*args, **call_kwargs)
            except TypeError as exc:
                if "actor" not in str(exc):
                    raise
                call_kwargs.pop("actor", None)
                result = bound(*args, **call_kwargs)
            result = self._awaitable_or_value(result)
            if isinstance(result, Mapping):
                return dict(result)
            if isinstance(result, (list, tuple)):
                return {"__list__": list(result)}
            return {}
        except Exception as exc:  # noqa: BLE001
            return {"__error__": str(exc)}

    def _reader_value(self, reader: Callable[[str], Any] | None, identifier: str) -> dict[str, Any]:
        if reader is None or not identifier:
            return {}
        try:
            result = self._awaitable_or_value(reader(identifier))
            return dict(result) if isinstance(result, Mapping) else {}
        except Exception as exc:  # noqa: BLE001
            return {"__error__": str(exc), "id": identifier}

    def _intervals(self, start: date | None, end: date | None) -> tuple[date, date, date, date]:
        today = self._clock().date()
        finish = end or today
        begin = start or finish.replace(day=1)
        if finish < begin:
            raise PortfolioValidationError("period end cannot be before period start.")
        span = (finish - begin).days + 1
        prior_end = begin - timedelta(days=1)
        prior_start = prior_end - timedelta(days=span - 1)
        return begin, finish, prior_start, prior_end

    def _snapshot(
        self,
        portfolio: Mapping[str, Any],
        ref: Mapping[str, Any],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        begin, finish, prior_start, prior_end = self._intervals(start, end)
        venture_id = str(ref.get("id"))
        detail = self._finance_call("detail", venture_id)
        raw = detail.get("venture")
        venture = dict(raw) if isinstance(raw, Mapping) else {}
        error = str(detail.get("__error__") or "")
        source_updated = str(venture.get("updated_at") or ref.get("updated_at") or "")
        income = self._finance_call("income_statement", venture_id, start=begin, end=finish)
        prior = self._finance_call("income_statement", venture_id, start=prior_start, end=prior_end)
        expenses = self._finance_call("expense_summary", venture_id, start=begin, end=finish)
        cash_flow = self._finance_call("cash_flow", venture_id, start=begin, end=finish)
        capital = self._finance_call("working_capital", venture_id, as_of=finish)
        budgets = self._finance_call("list_budgets", venture_id)
        budget_rows = budgets.get("__list__")
        if budget_rows is None:
            budget_rows = budgets.get("budgets", [])
        approved_budgets = [
            dict(row)
            for row in (budget_rows or [])
            if isinstance(row, Mapping) and row.get("status") == "approved"
        ][:10]
        budget_reports: list[dict[str, Any]] = []
        for budget in approved_budgets:
            report = self._finance_call("budget_report", venture_id, str(budget.get("id")))
            if report and not report.get("__error__"):
                budget_reports.append(report)

        product_id = str(ref.get("product_id") or "")
        if not product_id:
            linked = venture.get("linked_products")
            if isinstance(linked, (list, tuple)) and linked:
                product_id = str(linked[0])
        product = self._reader_value(self._products, product_id)
        opportunity_id = str(
            ref.get("opportunity_id")
            or product.get("opportunity_id")
            or venture.get("opportunity_id")
            or ""
        )
        opportunity = self._reader_value(self._opportunities, opportunity_id)
        customer_id = str(ref.get("customer_product_id") or product_id)
        customer = self._reader_value(self._customers, customer_id)
        customer_summary_value = customer.get("summary")
        customer_summary = (
            dict(customer_summary_value) if isinstance(customer_summary_value, Mapping) else {}
        )
        revenue_summary_value = detail.get("revenue_summary")
        revenue_summary = (
            dict(revenue_summary_value) if isinstance(revenue_summary_value, Mapping) else {}
        )
        by_customer = [
            row
            for row in revenue_summary.get("by_customer") or []
            if isinstance(row, Mapping)
            and str(row.get("customer_ref") or "") not in {"", "unassigned"}
        ]
        recognized_total = float(revenue_summary.get("recognized_revenue") or 0)
        max_customer = (
            max(float(row.get("recognized_revenue") or 0) for row in by_customer) / recognized_total
            if by_customer and recognized_total > 0
            else None
        )
        concentration = {
            "max_customer_share": max_customer,
            "customer_ref": (
                max(by_customer, key=lambda row: float(row.get("recognized_revenue") or 0)).get(
                    "customer_ref"
                )
                if by_customer
                else ""
            ),
            "assigned_customers": len(by_customer),
            "basis": "recognized revenue by named customer reference, current to as-of date",
        }
        operations_value = detail.get("operations")
        operations = dict(operations_value) if isinstance(operations_value, Mapping) else {}
        raw_risks = venture.get("operational_risks")
        health = detail.get("health")
        health_map = health if isinstance(health, Mapping) else {}
        open_alerts = [
            row
            for row in venture.get("health_alerts") or []
            if isinstance(row, Mapping) and row.get("status") == "open"
        ]
        now = self._clock()
        source_dt = None
        if source_updated:
            try:
                source_dt = datetime.fromisoformat(source_updated.replace("Z", "+00:00"))
                if source_dt.tzinfo is None:
                    source_dt = source_dt.replace(tzinfo=now.tzinfo)
            except ValueError:
                source_dt = None
        stale_hours = float((portfolio.get("monitoring") or {}).get("stale_after_hours", 24))
        fresh = bool(
            not error
            and source_dt is not None
            and (now - source_dt) <= timedelta(hours=stale_hours)
        )
        missing: list[str] = []
        if error:
            missing.append(f"Phase 38 venture could not be read: {error}")
        if not income:
            missing.append("income statement unavailable")
        elif income.get("__error__"):
            missing.append(f"income statement reader: {income['__error__']}")
        if not expenses:
            missing.append("expense summary unavailable")
        elif expenses.get("__error__"):
            missing.append(f"expense summary reader: {expenses['__error__']}")
        if not capital:
            missing.append("working-capital snapshot unavailable")
        elif capital.get("__error__"):
            missing.append(f"working-capital reader: {capital['__error__']}")
        if not product_id:
            missing.append("no Phase 36 product is linked")
        elif not product:
            missing.append(f"Phase 36 product {product_id!r} is unavailable")
        elif product.get("__error__"):
            missing.append(f"Phase 36 reader: {product['__error__']}")
        if not opportunity_id:
            missing.append("no Phase 35 opportunity is linked")
        elif not opportunity:
            missing.append(f"Phase 35 opportunity {opportunity_id!r} is unavailable")
        if not customer:
            missing.append("no Phase 37 customer workspace is available")
        return {
            "venture_id": venture_id,
            "name": str(venture.get("name") or ref.get("name") or venture_id),
            "currency": str(
                venture.get("reporting_currency")
                or ref.get("reporting_currency")
                or portfolio.get("base_currency")
                or "USD"
            ),
            "operating_status": str(venture.get("operating_status") or ""),
            "lifecycle_status": str(ref.get("lifecycle_status") or ""),
            "source_updated_at": source_updated,
            "fetched_at": now.isoformat(),
            "fresh": fresh,
            "error": error,
            "finance": {
                "detail": detail,
                "venture": venture,
                "period": {"start": begin.isoformat(), "end": finish.isoformat()},
                "income": income,
                "prior_income": prior,
                "expenses": expenses,
                "cash_flow": cash_flow,
                "working_capital": capital,
                "budgets": approved_budgets,
                "budget_reports": budget_reports,
                "operations": operations,
                "health": health_map,
                "open_alerts": open_alerts,
                "operational_risks": list(raw_risks or []),
                "concentration": concentration,
            },
            "prior_revenue": prior.get("revenue"),
            "product_id": product_id,
            "product": product,
            "opportunity_id": opportunity_id,
            "opportunity": opportunity,
            "customer_product_id": customer_id,
            "customer": customer,
            "customer_summary": customer_summary,
            "strategic_inputs": dict(ref.get("strategic_inputs") or {}),
            "missing": missing,
        }

    # -- lifecycle gates -----------------------------------------------------
    def _lifecycle_evidence(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        product_value = snapshot.get("product")
        product: Mapping[str, Any] = product_value if isinstance(product_value, Mapping) else {}
        opportunity_value = snapshot.get("opportunity")
        opportunity: Mapping[str, Any] = (
            opportunity_value if isinstance(opportunity_value, Mapping) else {}
        )
        customer_value = snapshot.get("customer")
        customer: Mapping[str, Any] = customer_value if isinstance(customer_value, Mapping) else {}
        finance = snapshot.get("finance")
        finance_map: Mapping[str, Any] = finance if isinstance(finance, Mapping) else {}
        venture_value = finance_map.get("venture")
        venture: Mapping[str, Any] = venture_value if isinstance(venture_value, Mapping) else {}
        income_value = finance_map.get("income")
        income: Mapping[str, Any] = income_value if isinstance(income_value, Mapping) else {}
        expenses_value = finance_map.get("expenses")
        expenses: Mapping[str, Any] = expenses_value if isinstance(expenses_value, Mapping) else {}
        state = str(product.get("state") or "")
        readiness_value = product.get("readiness")
        readiness: Mapping[str, Any] = (
            readiness_value if isinstance(readiness_value, Mapping) else {}
        )
        summary_value = product.get("summary")
        summary: Mapping[str, Any] = summary_value if isinstance(summary_value, Mapping) else {}
        stage = str(opportunity.get("stage") or "").lower()
        validation_state = str(product.get("validation_state") or "").lower()
        validated = stage in {"validated", "conditionally_validated"} or validation_state in {
            "validated",
            "conditionally_validated",
        }
        customer_snapshot_value = customer.get("product_snapshot")
        customer_snapshot: Mapping[str, Any] = (
            customer_snapshot_value if isinstance(customer_snapshot_value, Mapping) else {}
        )
        product_launch = product.get("launch_approval")
        readiness_launch = readiness.get("launch_approval")
        launch_authorized = bool(
            customer.get("launch_authorized")
            or customer_snapshot.get("launch_authorized")
            or (isinstance(product_launch, Mapping) and product_launch.get("approved") is True)
            or (isinstance(readiness_launch, Mapping) and readiness_launch.get("approved") is True)
        )
        release_approved = bool(
            readiness.get("release_approved")
            or any(
                isinstance(row, Mapping) and row.get("status") == "approved"
                for row in product.get("releases") or []
            )
        )
        tests = product.get("tests") or []
        has_finance_activity = bool(
            float(income.get("revenue") or 0) != 0
            or float(expenses.get("actual") or 0) != 0
            or venture.get("revenue_records")
            or venture.get("expense_records")
        )
        has_customer_workspace = bool(customer and not customer.get("__error__"))
        return {
            "opportunity_stage": stage,
            "opportunity_id": snapshot.get("opportunity_id", ""),
            "validation_passed": validated,
            "product_id": snapshot.get("product_id", ""),
            "product_state": state,
            "product_tests": len(tests) if isinstance(tests, (list, tuple)) else 0,
            "release_approved": release_approved,
            "readiness_status": str(readiness.get("status") or summary.get("readiness") or ""),
            "launch_authorized": launch_authorized,
            "customer_workspace": has_customer_workspace,
            "finance_activity": has_finance_activity,
            "source_updated_at": snapshot.get("source_updated_at", ""),
            "missing": list(snapshot.get("missing") or []),
        }

    def lifecycle_evidence(
        self, portfolio_id: str, venture_id: str, *, actor: str | None = None
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        ref = self._venture_ref(portfolio, venture_id)
        if not self._can_view_venture(portfolio, who, ref):
            raise PortfolioAuthorizationError("The venture is restricted from this actor.")
        return self._lifecycle_evidence(self._snapshot(portfolio, ref))

    def _gates(
        self, target: str, evidence: Mapping[str, Any], *, approved: bool
    ) -> tuple[bool, list[str]]:
        gaps: list[str] = []
        if target in MANAGEMENT_LIFECYCLES or target == "ARCHIVED":
            if target in {"PAUSED", "RESTRUCTURING", "WINDING_DOWN"} and not evidence.get(
                "source_updated_at"
            ):
                gaps.append(f"moving to {target} needs a current source snapshot")
            if target in {"PAUSED", "RESTRUCTURING", "WINDING_DOWN", "ARCHIVED"} and not approved:
                gaps.append(f"moving to {target} needs an explicit approval")
            return not gaps, gaps
        if target == "OPPORTUNITY_DISCOVERY":
            if not evidence.get("opportunity_id"):
                gaps.append("register or link a Phase 35 opportunity")
        elif target == "VALIDATION":
            if not evidence.get("opportunity_id"):
                gaps.append("a Phase 35 opportunity is required")
            if not evidence.get("opportunity_stage"):
                gaps.append("Phase 35 opportunity evidence is unavailable")
        elif target == "PRODUCT_DEVELOPMENT":
            if not evidence.get("validation_passed"):
                gaps.append(
                    "Phase 35 has not recorded a validated or conditionally validated verdict"
                )
            if not evidence.get("product_id"):
                gaps.append("a Phase 36 product is required")
        elif target == "INTERNAL_TESTING":
            if not evidence.get("product_id"):
                gaps.append("a Phase 36 product is required")
            if str(evidence.get("product_state")) not in {
                "INTERNAL_TESTING",
                "QA_IN_PROGRESS",
                "READY_FOR_RELEASE_REVIEW",
                "APPROVED_FOR_LAUNCH",
            }:
                gaps.append("Phase 36 has not reached internal testing")
            if not evidence.get("product_tests"):
                gaps.append("no Phase 36 test record exists")
        elif target == "READY_FOR_LAUNCH":
            if not evidence.get("product_id"):
                gaps.append("a Phase 36 product is required")
            if str(evidence.get("readiness_status")) not in {
                "READY_FOR_RELEASE_REVIEW",
                "APPROVED_FOR_LAUNCH",
            }:
                gaps.append("Phase 36 launch readiness has not reached release review")
            if not evidence.get("release_approved"):
                gaps.append("Phase 36 release has not been approved")
        elif target == "CUSTOMER_ACQUISITION":
            if not evidence.get("launch_authorized"):
                gaps.append("Phase 36 launch has not been human-authorized")
            if not evidence.get("release_approved"):
                gaps.append("Phase 36 release has not been approved")
            if not evidence.get("customer_workspace"):
                gaps.append("a Phase 37 customer workspace is required")
            if not approved:
                gaps.append("starting customer acquisition needs an explicit approval")
        elif target == "OPERATING":
            if not evidence.get("launch_authorized"):
                gaps.append("Phase 36 launch has not been human-authorized")
            if not evidence.get("release_approved"):
                gaps.append("Phase 36 release has not been approved")
            if not evidence.get("customer_workspace") and not evidence.get("finance_activity"):
                gaps.append("no Phase 37 workspace or Phase 38 actual activity exists")
            if not approved:
                gaps.append("operating status needs an explicit approval")
        return not gaps, gaps

    # -- portfolios ----------------------------------------------------------
    def create_portfolio(
        self, payload: Mapping[str, Any], *, actor: str | None = None
    ) -> dict[str, Any]:
        self._load()
        self._assert_no_secrets(payload)
        self._capacity(list(self._portfolios.values()), self.max_portfolios, "Portfolio")
        who = self._actor(actor)
        name = text(payload.get("name"), "name", 160, required=True)
        now = self._clock().isoformat()
        objectives = [
            text(item, "objective", 300, required=True)
            for item in bounded_rows(payload.get("objectives"), "objectives", 20)
        ]
        base = currency(payload.get("base_currency") or "USD")
        reporting = payload.get("reporting_period")
        reporting_period = (
            dict(reporting)
            if isinstance(reporting, Mapping)
            else {
                "kind": "monthly",
                "start": str(payload.get("period_start") or ""),
                "end": str(payload.get("period_end") or ""),
            }
        )
        risk_preferences_value = payload.get("risk_preferences")
        risk_preferences = (
            dict(risk_preferences_value) if isinstance(risk_preferences_value, Mapping) else {}
        )
        resource_policy_value = payload.get("resource_policy")
        resource_policy = (
            dict(resource_policy_value) if isinstance(resource_policy_value, Mapping) else {}
        )
        portfolio: dict[str, Any] = {
            "id": new_id("pf"),
            "name": name,
            "description": text(payload.get("description"), "description", 2000),
            "owner": who,
            "base_currency": base,
            "reporting_period": reporting_period,
            "objectives": objectives,
            "risk_preferences": {
                "max_single_venture_share": number(
                    risk_preferences.get("max_single_venture_share"),
                    "risk_preferences.max_single_venture_share",
                    minimum=0,
                    maximum=1,
                    default=0.50,
                ),
                "min_cash_runway_months": number(
                    risk_preferences.get("min_cash_runway_months"),
                    "risk_preferences.min_cash_runway_months",
                    minimum=0,
                    default=3.0,
                ),
                "max_budget_overrun": number(
                    risk_preferences.get("max_budget_overrun"),
                    "risk_preferences.max_budget_overrun",
                    minimum=0,
                    default=0.10,
                ),
            },
            # Automatic reallocation is a Phase 39 boundary, so a creation
            # payload cannot switch it on (the update path already forces it).
            "resource_policy": {
                **resource_policy,
                "require_approval": True,
                "allow_automatic_reallocation": False,
            },
            "status": "active",
            "members": [
                {
                    "actor": who,
                    "role": "admin",
                    "added_at": now,
                    "added_by": who,
                    "all_ventures": True,
                }
            ],
            "ventures": [],
            "events": [],
            "evaluation_policies": [dict(row) for row in DEFAULT_EVALUATION_POLICIES.values()],
            "evaluations": [],
            "risks": [],
            "manual_risks": [],
            "risk_resolutions": [],
            "allocation_batches": [],
            "scenarios": [],
            "recommendations": [],
            "decisions": [],
            "resources": [],
            "dependencies": [],
            "knowledge": [],
            "alerts": [],
            "reports": [],
            "fx_rates": {base: {"rate": 1.0, "as_of": now[:10], "source": "base_currency"}},
            "monitoring": dict(DEFAULT_MONITORING),
            "created_at": now,
            "updated_at": now,
        }
        if self.actor and self.actor != who:
            portfolio["members"].append(
                {
                    "actor": self.actor,
                    "role": "viewer",
                    "added_at": now,
                    "added_by": who,
                    "all_ventures": False,
                    "internal": True,
                }
            )
        self._portfolios[portfolio["id"]] = portfolio
        self._event(portfolio, "portfolio_created", who, {"currency": base})
        self._persist()
        return dict(portfolio)

    def list_portfolios(
        self, *, actor: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self._load()
        who = self._actor(actor)
        rows = [row for row in self._portfolios.values() if self._role(row, who)]
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        total = len(rows)
        page = rows[max(0, offset) : max(0, offset) + max(1, min(100, limit))]
        return {
            "total": total,
            "portfolios": [
                self._row(row, visible_venture_ids=self._visible_venture_ids(row, who))
                for row in page
            ],
            "note": "Only portfolios where the actor has a membership are listed.",
        }

    def detail(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        role = self._authorize(portfolio, who)
        visible = self._visible_refs(portfolio, who)
        visible_ids = {str(ref.get("id")) for ref in visible}
        open_alerts = [
            row
            for row in portfolio.get("alerts") or []
            if isinstance(row, Mapping)
            and row.get("status") == "open"
            and (not row.get("venture_id") or str(row.get("venture_id")) in visible_ids)
        ]
        return {
            "portfolio": self._portfolio_summary(portfolio),
            "role": role,
            "venture_summary": [self._venture_row(portfolio, ref) for ref in visible],
            "restricted_ventures": len(portfolio.get("ventures") or []) - len(visible),
            "counts": {
                "ventures": len(portfolio.get("ventures") or []),
                "visible_ventures": len(visible),
                "open_alerts": len(open_alerts),
                "decisions_pending": len(
                    [
                        row
                        for row in portfolio.get("decisions") or []
                        if isinstance(row, Mapping) and row.get("status") == "proposed"
                    ]
                ),
            },
            "access": {
                "role": role,
                "venture_isolation": "restricted ventures are omitted unless explicitly allowed",
                "bounds": {
                    "transfer_funds": False,
                    "purchase": False,
                    "change_finalized_ledger": False,
                    "close_venture": False,
                    "publish": False,
                },
            },
        }

    def update_portfolio(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "admin")
        self._assert_no_secrets(payload)
        if "name" in payload:
            portfolio["name"] = text(payload.get("name"), "name", 160, required=True)
        if "description" in payload:
            portfolio["description"] = text(payload.get("description"), "description", 2000)
        if "objectives" in payload:
            portfolio["objectives"] = [
                text(item, "objective", 300, required=True)
                for item in bounded_rows(payload.get("objectives"), "objectives", 20)
            ]
        if "reporting_period" in payload:
            data = payload.get("reporting_period")
            if not isinstance(data, Mapping):
                raise PortfolioValidationError("reporting_period must be an object.")
            portfolio["reporting_period"] = dict(data)
        if "risk_preferences" in payload:
            data = payload.get("risk_preferences")
            if not isinstance(data, Mapping):
                raise PortfolioValidationError("risk_preferences must be an object.")
            current = dict(portfolio.get("risk_preferences") or {})
            for key, minimum, maximum in (
                ("max_single_venture_share", 0.0, 1.0),
                ("min_cash_runway_months", 0.0, None),
                ("max_budget_overrun", 0.0, None),
            ):
                if key in data:
                    current[key] = number(
                        data.get(key),
                        f"risk_preferences.{key}",
                        minimum=minimum,
                        maximum=maximum,
                    )
            portfolio["risk_preferences"] = current
        if "resource_policy" in payload:
            data = payload.get("resource_policy")
            if not isinstance(data, Mapping):
                raise PortfolioValidationError("resource_policy must be an object.")
            portfolio["resource_policy"] = {
                **portfolio.get("resource_policy", {}),
                **dict(data),
                "require_approval": True,
                "allow_automatic_reallocation": False,
            }
        self._event(portfolio, "portfolio_updated", who, {})
        self._persist()
        return dict(portfolio)

    def set_status(
        self,
        portfolio_id: str,
        status: str,
        *,
        approved: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "admin")
        value = str(status or "").lower()
        if value not in PORTFOLIO_STATUSES:
            raise PortfolioValidationError(
                f"status must be one of: {', '.join(PORTFOLIO_STATUSES)}"
            )
        if value in {"paused", "archived"} and not approved:
            raise ApprovalRequired("Pausing or archiving a portfolio needs explicit approval.")
        portfolio["status"] = value
        self._event(portfolio, "portfolio_status_changed", who, {"status": value})
        self._persist()
        return dict(portfolio)

    def add_member(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "admin")
        member = text(payload.get("actor"), "actor", 120, required=True)
        role = str(payload.get("role") or "viewer").lower()
        if role not in ROLES:
            raise PortfolioValidationError(f"role must be one of: {', '.join(ROLES)}")
        if member == str(portfolio.get("owner")) and role != "admin":
            raise PortfolioValidationError("The portfolio owner cannot lose the admin role.")
        existing = next(
            (
                row
                for row in portfolio.get("members") or []
                if isinstance(row, dict) and str(row.get("actor")) == member
            ),
            None,
        )
        if existing is not None:
            existing["role"] = role
            existing["updated_at"] = self._clock().isoformat()
            existing["all_ventures"] = role == "admin"
        else:
            portfolio.setdefault("members", []).append(
                {
                    "actor": member,
                    "role": role,
                    "added_at": self._clock().isoformat(),
                    "added_by": who,
                    "all_ventures": role == "admin",
                }
            )
        self._event(portfolio, "member_added", who, {"member": member, "role": role})
        self._persist()
        return next(row for row in portfolio["members"] if row.get("actor") == member)

    def add_fx_rate(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        self._assert_no_secrets(payload)
        code = currency(payload.get("currency"))
        if code == portfolio.get("base_currency"):
            raise PortfolioValidationError("The base currency already has an implicit rate of 1.")
        rate = number(payload.get("rate"), "rate", minimum=0.000001)
        as_of = optional_day(payload.get("as_of"), "as_of")
        if as_of is None:
            raise PortfolioValidationError("An FX rate needs an as_of date.")
        source = text(payload.get("source"), "source", 300, required=True)
        portfolio.setdefault("fx_rates", {})[code] = {
            "rate": rate,
            "as_of": as_of.isoformat(),
            "source": source,
            "recorded_by": who,
            "recorded_at": self._clock().isoformat(),
        }
        self._event(portfolio, "fx_rate_recorded", who, {"currency": code})
        self._persist()
        return dict(portfolio["fx_rates"][code])

    def set_monitoring(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        current = dict(portfolio.get("monitoring") or DEFAULT_MONITORING)
        for key in DEFAULT_MONITORING:
            if key not in payload:
                continue
            value = number(payload.get(key), f"monitoring.{key}", minimum=0)
            current[key] = value
        portfolio["monitoring"] = current
        self._event(
            portfolio,
            "monitoring_configured",
            who,
            {"interval_minutes": current.get("interval_minutes")},
        )
        self._persist()
        return dict(current)

    # -- venture registration and lifecycle ---------------------------------
    def add_venture(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "admin")
        self._assert_no_secrets(payload)
        self._capacity(
            list(portfolio.get("ventures") or []), self.max_ventures, "Portfolio venture"
        )
        venture_id = text(
            payload.get("venture_id") or payload.get("id"), "venture_id", 160, required=True
        )
        bound = self._bindings.get(venture_id)
        if bound:
            raise PortfolioConflict(
                f"Venture {venture_id!r} is already bound to portfolio {bound!r}; "
                "a venture cannot be counted in two portfolios."
            )
        if any(str(row.get("id")) == venture_id for row in portfolio.get("ventures") or []):
            raise PortfolioConflict(f"Venture {venture_id!r} is already registered.")
        finance = self._finance_call("detail", venture_id, actor=self.actor)
        if finance.get("__error__"):
            raise PortfolioValidationError(
                f"Phase 38 venture {venture_id!r} could not be read: {finance['__error__']}"
            )
        raw = finance.get("venture")
        if not isinstance(raw, Mapping) or not raw.get("id"):
            raise PortfolioValidationError(f"Phase 38 venture {venture_id!r} was not found.")
        product_id = text(payload.get("product_id"), "product_id", 160)
        product = self._reader_value(self._products, product_id) if product_id else {}
        if product_id and (not product or product.get("__error__")):
            raise PortfolioValidationError(f"Phase 36 product {product_id!r} was not found.")
        opportunity_id = text(
            payload.get("opportunity_id") or product.get("opportunity_id"),
            "opportunity_id",
            160,
        )
        opportunity = (
            self._reader_value(self._opportunities, opportunity_id) if opportunity_id else {}
        )
        if opportunity_id and (not opportunity or opportunity.get("__error__")):
            raise PortfolioValidationError(
                f"Phase 35 opportunity {opportunity_id!r} was not found."
            )
        if (
            product.get("opportunity_id")
            and opportunity_id
            and str(product["opportunity_id"]) != opportunity_id
        ):
            raise PortfolioValidationError(
                "The linked Phase 36 product belongs to a different Phase 35 opportunity."
            )
        visibility = str(payload.get("visibility") or "portfolio").lower()
        if visibility not in {"portfolio", "restricted"}:
            raise PortfolioValidationError("visibility must be portfolio or restricted.")
        allowed = [
            text(item, "allowed_member", 120, required=True)
            for item in bounded_rows(payload.get("allowed_members"), "allowed_members", 50)
        ]
        strategic_raw = payload.get("strategic_inputs")
        strategic = dict(strategic_raw) if isinstance(strategic_raw, Mapping) else {}
        strategic_inputs: dict[str, float] = {}
        for key in ("alignment", "scalability", "market_opportunity", "competitive_position"):
            if strategic.get(key) is not None:
                strategic_inputs[key] = number(
                    strategic.get(key), f"strategic_inputs.{key}", minimum=0, maximum=1
                )
        requested = str(payload.get("lifecycle_status") or "OPPORTUNITY_DISCOVERY").upper()
        if requested not in LIFECYCLE_STATUSES:
            raise PortfolioValidationError(
                f"lifecycle_status must be one of: {', '.join(LIFECYCLE_STATUSES)}"
            )
        now = self._clock().isoformat()
        ref: dict[str, Any] = {
            "id": venture_id,
            "name": str(raw.get("name") or venture_id),
            "description": text(payload.get("description"), "description", 1000),
            "opportunity_id": opportunity_id,
            "product_id": product_id,
            "customer_product_id": text(
                payload.get("customer_product_id") or product_id,
                "customer_product_id",
                160,
            ),
            "lifecycle_status": "OPPORTUNITY_DISCOVERY",
            "lifecycle_history": [],
            "priority": int(
                number(payload.get("priority"), "priority", minimum=1, maximum=5, default=3)
            ),
            "visibility": visibility,
            "allowed_members": allowed,
            "strategic_inputs": strategic_inputs,
            "tags": [
                text(item, "tag", 80, required=True)
                for item in bounded_rows(payload.get("tags"), "tags", 20)
            ],
            "reporting_currency": str(
                raw.get("reporting_currency") or portfolio.get("base_currency")
            ),
            "added_at": now,
            "added_by": who,
            "updated_at": now,
            "registered_snapshot": {
                "operating_status": raw.get("operating_status"),
                "phase38_updated_at": raw.get("updated_at"),
                "product_state": product.get("state", ""),
                "opportunity_stage": opportunity.get("stage", ""),
            },
        }
        portfolio.setdefault("ventures", []).append(ref)
        if requested != "OPPORTUNITY_DISCOVERY":
            try:
                self._apply_lifecycle(
                    portfolio,
                    ref,
                    requested,
                    reason=text(payload.get("reason"), "reason", 500) or "initial registration",
                    actor=who,
                    approved=bool(payload.get("approved", False)),
                )
            except Exception:
                portfolio["ventures"].remove(ref)
                raise
        else:
            ref["lifecycle_history"].append(
                {
                    "id": new_id("plc"),
                    "from": "",
                    "to": "OPPORTUNITY_DISCOVERY",
                    "at": now,
                    "actor": who,
                    "reason": "venture registered",
                    "evidence": self._lifecycle_evidence(self._snapshot(portfolio, ref)),
                    "approved": False,
                }
            )
        self._bindings[venture_id] = str(portfolio.get("id"))
        self._event(
            portfolio,
            "venture_registered",
            who,
            {"venture_id": venture_id, "lifecycle": ref["lifecycle_status"]},
        )
        self._persist()
        return self._venture_row(portfolio, ref)

    def list_ventures(self, portfolio_id: str, *, actor: str | None = None) -> list[dict[str, Any]]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        return [self._venture_row(portfolio, ref) for ref in self._visible_refs(portfolio, who)]

    def _venture_row(self, portfolio: Mapping[str, Any], ref: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": ref.get("id"),
            "name": ref.get("name"),
            "lifecycle_status": ref.get("lifecycle_status"),
            "priority": ref.get("priority"),
            "visibility": ref.get("visibility"),
            "opportunity_id": ref.get("opportunity_id"),
            "product_id": ref.get("product_id"),
            "reporting_currency": ref.get("reporting_currency"),
            "tags": list(ref.get("tags") or []),
            "lifecycle_changes": len(ref.get("lifecycle_history") or []),
            "added_at": ref.get("added_at"),
            "updated_at": ref.get("updated_at"),
            "drill_down": f"/finance/ventures/{ref.get('id')}",
            "portfolio_id": portfolio.get("id"),
        }

    def venture_detail(
        self,
        portfolio_id: str,
        venture_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        from novacontrol.portfolio.engines import venture_metrics

        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        role = self._authorize(portfolio, who)
        ref = self._venture_ref(portfolio, venture_id)
        if not self._can_view_venture(portfolio, who, ref):
            raise PortfolioAuthorizationError("The venture is restricted from this actor.")
        snapshot = self._snapshot(portfolio, ref, start=start, end=end)
        indicators = self._lifecycle_evidence(snapshot)
        return {
            "portfolio_id": portfolio.get("id"),
            "role": role,
            "venture": dict(ref),
            "lifecycle": {
                "current": ref.get("lifecycle_status"),
                "evidence": indicators,
                "available_transitions": self._available_transitions(ref, indicators),
                "history": list(ref.get("lifecycle_history") or []),
            },
            "metrics": venture_metrics(snapshot),
            "snapshot": snapshot,
            "source_timestamps": {
                "phase38": snapshot.get("source_updated_at"),
                "fetched_at": snapshot.get("fetched_at"),
                "fresh": snapshot.get("fresh"),
            },
            "missing": snapshot.get("missing", []),
            "drill_down": f"/finance/ventures/{venture_id}",
        }

    def _available_transitions(
        self, ref: Mapping[str, Any], evidence: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        current = str(ref.get("lifecycle_status") or "")
        rows: list[dict[str, Any]] = []
        if current == "ARCHIVED":
            return rows
        for target in LIFECYCLE_STATUSES:
            if target == current:
                continue
            ok, gaps = self._gates(target, evidence, approved=False)
            # Approval-required targets are still advertised as available; the
            # transition route records the approval explicitly.
            if target in {"CUSTOMER_ACQUISITION", "OPERATING", "ARCHIVED"}:
                ok = ok or all("approval" in gap for gap in gaps)
            rows.append(
                {
                    "target": target,
                    "eligible": ok,
                    "gaps": gaps,
                    "approval_required": target
                    in {
                        "CUSTOMER_ACQUISITION",
                        "OPERATING",
                        "PAUSED",
                        "RESTRUCTURING",
                        "WINDING_DOWN",
                        "ARCHIVED",
                    },
                }
            )
        return rows

    def transition_lifecycle(
        self,
        portfolio_id: str,
        venture_id: str,
        target: str,
        *,
        reason: str,
        approved: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        self._assert_no_secrets({"reason": reason})
        ref = self._venture_ref(portfolio, venture_id)
        value = str(target or "").upper()
        if value not in LIFECYCLE_STATUSES:
            raise PortfolioValidationError(
                f"target must be one of: {', '.join(LIFECYCLE_STATUSES)}"
            )
        if str(ref.get("lifecycle_status")) == "ARCHIVED":
            raise LifecycleGateError("An archived venture is terminal in this portfolio.")
        self._apply_lifecycle(
            portfolio,
            ref,
            value,
            reason=text(reason, "reason", 500, required=True),
            actor=who,
            approved=approved,
        )
        self._event(
            portfolio,
            "lifecycle_changed",
            who,
            {"venture_id": venture_id, "status": value},
        )
        self._persist()
        return self._venture_row(portfolio, ref)

    def _apply_lifecycle(
        self,
        portfolio: dict[str, Any],
        ref: dict[str, Any],
        target: str,
        *,
        reason: str,
        actor: str,
        approved: bool,
    ) -> None:
        snapshot = self._snapshot(portfolio, ref)
        evidence = self._lifecycle_evidence(snapshot)
        ok, gaps = self._gates(target, evidence, approved=approved)
        if not ok:
            if gaps and all("approval" in gap for gap in gaps):
                raise ApprovalRequired("; ".join(gaps))
            raise LifecycleGateError(
                "The lifecycle transition is not supported: " + "; ".join(gaps)
            )
        now = self._clock().isoformat()
        previous = str(ref.get("lifecycle_status") or "")
        ref["lifecycle_status"] = target
        ref["updated_at"] = now
        ref.setdefault("lifecycle_history", []).append(
            {
                "id": new_id("plc"),
                "from": previous,
                "to": target,
                "at": now,
                "actor": actor,
                "reason": reason,
                "approved": approved,
                "evidence": evidence,
            }
        )
        ref["lifecycle_history"] = ref["lifecycle_history"][-self.max_records :]
        if target in {"UNDER_REVIEW", "PAUSED", "RESTRUCTURING", "WINDING_DOWN", "ARCHIVED"}:
            portfolio.setdefault("alerts", []).append(
                {
                    "id": new_id("alert"),
                    "portfolio_id": portfolio.get("id"),
                    "venture_id": ref.get("id"),
                    "metric": "lifecycle_change",
                    "observed": target,
                    "severity": "medium" if target in {"PAUSED", "WINDING_DOWN"} else "low",
                    "evidence": [
                        f"Lifecycle changed from {previous or 'none'} to {target}: {reason}"
                    ],
                    "timestamp": now,
                    "recommended_action": "Review the approved lifecycle decision and next steps.",
                    "status": "open",
                    "kind": "lifecycle",
                }
            )

    # -- status/history ------------------------------------------------------
    def history(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)

        def _visible(row: Mapping[str, Any], *keys: str) -> bool:
            return all(
                str(row.get(key)) in visible for key in keys if row.get(key) not in (None, "")
            )

        all_refs = [
            str(ref.get("id"))
            for ref in portfolio.get("ventures") or []
            if isinstance(ref, Mapping) and ref.get("id")
        ]
        reports = []
        for row in portfolio.get("reports") or []:
            if not isinstance(row, Mapping):
                continue
            stored = row.get("visible_venture_ids")
            if stored is None:
                stored = all_refs
            if {str(item) for item in stored if item not in (None, "")} <= visible:
                reports.append(
                    {
                        "id": row.get("id"),
                        "title": row.get("title"),
                        "period": row.get("period"),
                        "generated_at": row.get("generated_at"),
                    }
                )

        def _event_visible(row: Mapping[str, Any]) -> bool:
            detail = row.get("detail")
            return _visible(detail if isinstance(detail, Mapping) else {}, "venture_id")

        return {
            "portfolio_id": portfolio_id,
            "events": [
                dict(row)
                for row in portfolio.get("events") or []
                if isinstance(row, Mapping) and _event_visible(row)
            ],
            "decisions": [
                dict(row)
                for row in portfolio.get("decisions") or []
                if isinstance(row, Mapping) and _visible(row, "venture_id")
            ],
            "reports": reports,
        }

    def status(self) -> dict[str, Any]:
        self._load()
        portfolios = list(self._portfolios.values())
        return {
            "service": "portfolio_manager",
            "namespace": NAMESPACE,
            "schema_version": SCHEMA_VERSION,
            "portfolios": len(portfolios),
            "ventures": sum(len(row.get("ventures") or []) for row in portfolios),
            "open_alerts": sum(
                len(
                    [
                        alert
                        for alert in row.get("alerts") or []
                        if isinstance(alert, Mapping) and alert.get("status") == "open"
                    ]
                )
                for row in portfolios
            ),
            "pending_decisions": sum(
                len(
                    [
                        decision
                        for decision in row.get("decisions") or []
                        if isinstance(decision, Mapping) and decision.get("status") == "proposed"
                    ]
                )
                for row in portfolios
            ),
            "configured": {
                "finance_reader": self._finance is not None,
                "product_reader": self._products is not None,
                "opportunity_reader": self._opportunities is not None,
                "customer_reader": self._customers is not None,
            },
            "limits": {
                "portfolios": self.max_portfolios,
                "ventures_per_portfolio": self.max_ventures,
                "records": self.max_records,
            },
            "boundaries": {
                "funds_transfer": False,
                "purchases": False,
                "contracts": False,
                "external_launch": False,
                "paid_campaigns": False,
                "ledger_mutation": False,
                "venture_closure": False,
                "automatic_decision": False,
            },
            "load_errors": list(self._load_errors),
        }


__all__ = ["NAMESPACE", "SCHEMA_VERSION", "PortfolioRegistry"]
