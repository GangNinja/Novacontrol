"""Portfolio intelligence layered over the Phase 39 registry.

The methods here are deliberately deterministic. They consume read-only source
snapshots, produce labelled evidence, and persist only portfolio recommendations,
assessments, alerts and reports. None of them transfers funds, changes a Phase 38
budget, executes a lifecycle decision or contacts a customer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from novacontrol.portfolio.engines import (
    allocation_recommendation,
    build_scenario,
    correlated_risks,
    evaluate_venture,
    rank_evaluations,
    recommendation_from_metrics,
    venture_metrics,
    venture_risks,
)
from novacontrol.portfolio.errors import (
    PortfolioAuthorizationError,
    PortfolioConflict,
    PortfolioRecordNotFound,
    PortfolioValidationError,
)
from novacontrol.portfolio.models import (
    DECISION_KINDS,
    DEFAULT_MONITORING,
    KNOWLEDGE_KINDS,
    REPORT_PERIODS,
    RISK_CATEGORIES,
    SCENARIO_TYPES,
    SEVERITIES,
    bounded_rows,
    currency,
    metric,
    new_id,
    number,
    optional_day,
    text,
)
from novacontrol.portfolio.service import PortfolioRegistry


class PortfolioIntelligenceMixin(PortfolioRegistry):
    """Portfolio intelligence over the shared registry helpers."""

    # -- selection helpers ---------------------------------------------------
    def _snapshots_for(
        self,
        portfolio: Mapping[str, Any],
        actor: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self._snapshot(portfolio, ref, start=start, end=end)
            for ref in self._visible_refs(portfolio, actor)
        ]

    @staticmethod
    def _record_visible(
        row: Mapping[str, Any],
        visible: set[str],
        keys: Sequence[str] = ("venture_id", "target_venture_id", "source_venture_id"),
    ) -> bool:
        """True when every venture id a stored record names is visible to the actor."""
        return all(str(row.get(key)) in visible for key in keys if row.get(key) not in (None, ""))

    @classmethod
    def _filter_batch(
        cls,
        batch: Mapping[str, Any],
        visible: set[str],
        nested: str,
        *,
        keys: Sequence[str] = ("venture_id", "target_venture_id", "source_venture_id"),
    ) -> dict[str, Any]:
        """Copy a stored batch and remove entries naming a hidden venture."""
        row = dict(batch)
        original = [item for item in batch.get(nested) or [] if isinstance(item, Mapping)]
        kept = [dict(item) for item in original if cls._record_visible(item, visible, keys)]
        row[nested] = kept
        if isinstance(row.get("reallocation"), Mapping) and not cls._record_visible(
            row["reallocation"], visible, keys
        ):
            row["reallocation"] = None
        row["filtered_for_visibility"] = len(kept) != len(original)
        return row

    @classmethod
    def _scenario_visible(cls, scenario: Mapping[str, Any], visible: set[str]) -> bool:
        if not cls._record_visible(scenario, visible):
            return False
        return all(
            cls._record_visible(row, visible)
            for row in scenario.get("ventures") or []
            if isinstance(row, Mapping)
        )

    def _rates(self, portfolio: Mapping[str, Any]) -> Mapping[str, Any]:
        rates = portfolio.get("fx_rates")
        return rates if isinstance(rates, Mapping) else {}

    def _rate(self, portfolio: Mapping[str, Any], code: str, end: date) -> tuple[float | None, str]:
        base = str(portfolio.get("base_currency") or "USD")
        if code == base:
            return 1.0, "base currency"
        row = self._rates(portfolio).get(code)
        if not isinstance(row, Mapping):
            return None, f"no explicit FX rate for {code} to {base}"
        try:
            raw_rate = row.get("rate")
            if raw_rate is None:
                raise ValueError("missing rate")
            rate = float(raw_rate)
            as_of = date.fromisoformat(str(row.get("as_of"))[:10])
            if rate <= 0:
                raise ValueError("non-positive rate")
        except (TypeError, ValueError):
            return None, f"the FX rate for {code} is malformed"
        if as_of > end:
            return None, f"the FX rate for {code} is dated after the reporting period"
        return (
            rate,
            f"explicit rate recorded {as_of.isoformat()} from {row.get('source', 'unspecified')}",
        )

    # -- consolidated analytics ---------------------------------------------
    def portfolio_analytics(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        begin, finish, _prior_start, _prior_end = self._intervals(start, end)
        snapshots = self._snapshots_for(portfolio, who, start=begin, end=finish)
        base = str(portfolio.get("base_currency") or "USD")
        rows: list[dict[str, Any]] = []
        by_currency: dict[str, dict[str, Decimal]] = {}
        converted = {
            "revenue": Decimal("0.00"),
            "expenses": Decimal("0.00"),
            "net_profit": Decimal("0.00"),
            "cash": Decimal("0.00"),
            "net_cash_flow": Decimal("0.00"),
        }
        metric_complete = dict.fromkeys(converted, True)
        metric_labels = {
            "revenue": "posted revenue",
            "expenses": "approved actual expenses",
            "net_profit": "posted net profit",
            "cash": "posted cash balance",
            "net_cash_flow": "posted net cash movement",
        }
        excluded: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        conversions: list[dict[str, Any]] = []
        lifecycle: dict[str, int] = {}
        customer_revenue: dict[str, Decimal] = {}
        product_revenue: dict[str, Decimal] = {}
        currency_missing: dict[str, set[str]] = {}
        concentration_complete = True
        for snapshot in snapshots:
            metrics = venture_metrics(snapshot)
            technical = metrics.get("technical")
            t = technical if isinstance(technical, Mapping) else {}
            code = str(snapshot.get("currency") or base)
            rate, rate_note = self._rate(portfolio, code, finish)
            raw_values = {
                "revenue": t.get("revenue"),
                "expenses": t.get("actual_expenses"),
                "net_profit": t.get("net_profit"),
                "cash": t.get("cash"),
                "net_cash_flow": t.get("net_cash"),
            }
            values = {
                name: None if value is None else float(value) for name, value in raw_values.items()
            }
            missing_metrics = [name for name, value in values.items() if value is None]
            bucket = by_currency.setdefault(
                code,
                {
                    "revenue": Decimal("0.00"),
                    "expenses": Decimal("0.00"),
                    "net_profit": Decimal("0.00"),
                    "cash": Decimal("0.00"),
                    "net_cash_flow": Decimal("0.00"),
                    "ventures": Decimal("0"),
                },
            )
            for name, value in values.items():
                if value is None:
                    currency_missing.setdefault(code, set()).add(name)
                else:
                    bucket[name] += Decimal(str(value))
            bucket["ventures"] += Decimal("1")
            converted_row = None
            if rate is None:
                excluded.append(
                    {
                        "venture_id": snapshot.get("venture_id"),
                        "currency": code,
                        "reason": rate_note,
                    }
                )
                concentration_complete = False
            else:
                for name, value in values.items():
                    if value is None:
                        metric_complete[name] = False
                    else:
                        converted[name] += Decimal(str(value * rate))
                conversions.append(
                    {
                        "venture_id": snapshot.get("venture_id"),
                        "from": code,
                        "to": base,
                        "rate": rate,
                        "basis": rate_note,
                    }
                )
                if values["revenue"] is not None:
                    converted_row = round(values["revenue"] * rate, 2)
                if missing_metrics:
                    incomplete.append(
                        {
                            "venture_id": snapshot.get("venture_id"),
                            "currency": code,
                            "missing_metrics": missing_metrics,
                            "reason": "one or more actual source metrics are unavailable",
                        }
                    )
            row = {
                "venture_id": snapshot.get("venture_id"),
                "venture_name": snapshot.get("name"),
                "lifecycle_status": snapshot.get("lifecycle_status"),
                "currency": code,
                "fx_rate": rate,
                "fx_note": rate_note,
                "revenue_base": converted_row,
                "revenue": values["revenue"],
                "actual_expenses": values["expenses"],
                "net_profit": values["net_profit"],
                "gross_margin": t.get("gross_margin"),
                "operating_margin": t.get("operating_margin"),
                "cash": values["cash"],
                "net_cash_flow": values["net_cash_flow"],
                "runway_months": t.get("runway"),
                "revenue_growth": t.get("growth"),
                "customers": t.get("customers"),
                "approved_budget": t.get("approved_budget"),
                "budget_actual": t.get("budget_actual"),
                "eligible_for_consolidation": rate is not None and not missing_metrics,
                "missing_metrics": missing_metrics,
                "source_updated_at": snapshot.get("source_updated_at"),
                "fresh": snapshot.get("fresh"),
                "missing": snapshot.get("missing", []),
                "metrics": metrics,
            }
            rows.append(row)
            lifecycle[str(snapshot.get("lifecycle_status") or "UNKNOWN")] = (
                lifecycle.get(str(snapshot.get("lifecycle_status") or "UNKNOWN"), 0) + 1
            )
            detail = snapshot.get("finance", {}).get("detail", {})
            revenue_summary = detail.get("revenue_summary") if isinstance(detail, Mapping) else {}
            if isinstance(revenue_summary, Mapping):
                for customer_row in revenue_summary.get("by_customer") or []:
                    if not isinstance(customer_row, Mapping):
                        continue
                    ref = str(customer_row.get("customer_ref") or "")
                    if ref in {"", "unassigned"} or rate is None:
                        continue
                    # Scope the key by venture: identical customer references
                    # in independent ventures must not be silently merged.
                    key = f"{snapshot.get('venture_id')}::{ref}"
                    customer_revenue[key] = customer_revenue.get(key, Decimal("0.00")) + Decimal(
                        str(customer_row.get("recognized_revenue") or 0)
                    ) * Decimal(str(rate))
                for product_row in revenue_summary.get("by_product") or []:
                    if not isinstance(product_row, Mapping):
                        continue
                    ref = str(product_row.get("product_ref") or "")
                    if ref in {"", "unassigned"} or rate is None:
                        continue
                    key = f"{snapshot.get('venture_id')}::{ref}"
                    product_revenue[key] = product_revenue.get(key, Decimal("0.00")) + Decimal(
                        str(product_row.get("recognized_revenue") or 0)
                    ) * Decimal(str(rate))
        complete = bool(rows) and not excluded and all(metric_complete.values())
        available = complete

        def _total(name: str) -> tuple[float | None, str]:
            if not rows:
                return None, "No accessible venture has data for this period."
            if excluded:
                return None, (
                    "At least one venture has no explicit FX conversion; no mixed-currency "
                    "total is presented."
                )
            if not metric_complete[name]:
                return None, (
                    f"At least one visible venture has no {metric_labels[name]} data; "
                    "missing source data is not consolidated as zero."
                )
            return float(converted[name]), ""

        total_revenue, revenue_reason = _total("revenue")
        total_expenses, expenses_reason = _total("expenses")
        total_profit, profit_reason = _total("net_profit")
        total_cash, cash_reason = _total("cash")
        total_net_cash, net_cash_reason = _total("net_cash_flow")
        concentration: float | None = None
        concentration_ref = ""
        assigned_total = sum(customer_revenue.values())
        if assigned_total > 0 and concentration_complete:
            top_key, top_value = max(customer_revenue.items(), key=lambda item: item[1])
            concentration = float(top_value / assigned_total)
            concentration_ref = top_key
        return {
            "portfolio_id": portfolio.get("id"),
            "portfolio_name": portfolio.get("name"),
            "base_currency": base,
            "period": {"start": begin.isoformat(), "end": finish.isoformat()},
            "ventures": rows,
            "venture_count": len(rows),
            "hidden_venture_count": len(portfolio.get("ventures") or []) - len(rows),
            "lifecycle_distribution": lifecycle,
            "totals": {
                "available": available,
                "complete": complete,
                "reason": (
                    "No accessible venture has data for this period."
                    if not rows
                    else "At least one venture has no explicit FX conversion; no mixed-currency total is presented."
                    if excluded
                    else (
                        "At least one visible venture has missing source data; missing values are "
                        "not consolidated as zero."
                        if not complete
                        else ""
                    )
                ),
                "revenue": metric(
                    total_revenue,
                    kind="actual",
                    method="sum of converted posted revenue",
                    source="phase38",
                    reason=revenue_reason,
                ),
                "expenses": metric(
                    total_expenses,
                    kind="actual",
                    method="sum of converted approved actual expenses",
                    source="phase38",
                    reason=expenses_reason,
                ),
                "net_profit": metric(
                    total_profit,
                    kind="actual",
                    method="sum of converted posted net profit",
                    source="phase38",
                    reason=profit_reason,
                ),
                "cash": metric(
                    total_cash,
                    kind="actual",
                    method="sum of converted posted cash balances",
                    source="phase38",
                    reason=cash_reason,
                ),
                "net_cash_flow": metric(
                    total_net_cash,
                    kind="actual",
                    method="sum of converted cash-flow net change",
                    source="phase38",
                    reason=net_cash_reason,
                ),
                "profitability": metric(
                    (total_profit / total_revenue)
                    if total_revenue and total_profit is not None
                    else None,
                    kind="actual",
                    method="converted net profit / converted revenue",
                    reason=(
                        "revenue is unavailable, zero or incomplete"
                        if total_revenue in (None, 0) or total_profit is None
                        else ""
                    ),
                ),
            },
            "by_currency": {
                key: {
                    **{name: float(value) for name, value in bucket.items()},
                    "ventures": int(bucket["ventures"]),
                    "complete": not currency_missing.get(key),
                    "missing_metrics": sorted(currency_missing.get(key, set())),
                }
                for key, bucket in sorted(by_currency.items())
            },
            "conversions": conversions,
            "excluded_from_consolidation": excluded,
            "incomplete_data": incomplete,
            "concentration": {
                "max_named_customer_share": concentration,
                "reference": concentration_ref,
                "complete": concentration_complete,
                "reason": (
                    ""
                    if concentration_complete
                    else "At least one visible venture has no explicit FX conversion."
                ),
                "basis": (
                    "Named customer revenue is scoped by venture and converted only with explicit rates. "
                    "Unassigned revenue is excluded from the share."
                ),
            },
            "customer_revenue": [
                {"reference": key, "converted_revenue": float(value)}
                for key, value in sorted(
                    customer_revenue.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "product_revenue": [
                {"reference": key, "converted_revenue": float(value)}
                for key, value in sorted(
                    product_revenue.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "inter_venture": {
                "policy": "separate_legal_entities_no_elimination",
                "treatment": (
                    "Ventures are distinct Phase 38 ledgers. No inter-venture transaction is recorded "
                    "in this portfolio, so no elimination is applied and the same venture is never counted twice."
                ),
                "duplicate_venture_binding": "rejected at registration",
            },
            "data_completeness": {
                "included": sum(1 for row in rows if row.get("eligible_for_consolidation")),
                "excluded": len(excluded),
                "incomplete": len(incomplete),
                "incomplete_ventures": incomplete,
                "hidden": len(portfolio.get("ventures") or []) - len(rows),
                "missing": [item for row in rows for item in row.get("missing", [])],
                "source_timestamps": [
                    {
                        "venture_id": row.get("venture_id"),
                        "source_updated_at": row.get("source_updated_at"),
                        "fresh": row.get("fresh"),
                    }
                    for row in rows
                ],
            },
            "limitations": [
                "Only actual posted Phase 38 values are consolidated; pipeline, forecasts and budget plans are not revenue.",
                "A missing explicit FX rate excludes the venture from converted totals rather than assuming a rate.",
                "An unavailable actual metric makes the affected total unavailable rather than zero.",
                "Restricted ventures not visible to the actor are omitted and counted as hidden.",
                "No inter-venture elimination is invented where no transaction exists.",
            ],
        }

    def venture_comparison(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        analytics = self.portfolio_analytics(portfolio_id, actor=actor, start=start, end=end)
        return {
            "portfolio_id": analytics["portfolio_id"],
            "period": analytics["period"],
            "base_currency": analytics["base_currency"],
            "ventures": [
                {
                    key: row.get(key)
                    for key in (
                        "venture_id",
                        "venture_name",
                        "lifecycle_status",
                        "currency",
                        "revenue",
                        "actual_expenses",
                        "net_profit",
                        "gross_margin",
                        "operating_margin",
                        "cash",
                        "net_cash_flow",
                        "runway_months",
                        "revenue_growth",
                        "customers",
                        "approved_budget",
                        "budget_actual",
                        "eligible_for_consolidation",
                        "fresh",
                    )
                }
                for row in analytics["ventures"]
            ],
            "currency_rule": "Amounts stay in each venture's reporting currency; revenue_base is only populated with an explicit FX rate.",
            "note": "Comparison does not rank missing data as zero.",
        }

    # -- evaluation ----------------------------------------------------------
    def _policy(self, portfolio: Mapping[str, Any], name: str) -> dict[str, Any]:
        wanted = str(name or "balanced").lower()
        for row in portfolio.get("evaluation_policies") or []:
            if isinstance(row, Mapping) and str(row.get("name")).lower() == wanted:
                return dict(row)
        raise PortfolioValidationError(f"Evaluation policy {wanted!r} is not defined.")

    def define_evaluation_policy(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "admin")
        name = text(payload.get("name"), "name", 80, required=True).lower()
        weights_raw = payload.get("weights")
        if not isinstance(weights_raw, Mapping):
            raise PortfolioValidationError("weights must be an object of dimension weights.")
        weights = {
            str(key): number(value, f"weights.{key}", minimum=0)
            for key, value in weights_raw.items()
        }
        valid = {"financial", "market", "operational", "strategic"}
        if set(weights) - valid:
            raise PortfolioValidationError(
                "weights may only contain financial, market, operational and strategic."
            )
        if sum(weights.values()) <= 0:
            raise PortfolioValidationError("weights must sum to a positive value.")
        policy = {
            "name": name,
            "description": text(payload.get("description"), "description", 500),
            "weights": weights,
            "min_coverage": number(
                payload.get("min_coverage"), "min_coverage", minimum=0, maximum=1, default=0.5
            ),
            "created_by": who,
            "created_at": self._clock().isoformat(),
        }
        existing = [
            row for row in portfolio.get("evaluation_policies") or [] if row.get("name") != name
        ]
        portfolio["evaluation_policies"] = existing + [policy]
        self._event(portfolio, "evaluation_policy_defined", who, {"policy": name})
        self._persist()
        return policy

    def _evaluate_rows(
        self,
        portfolio: Mapping[str, Any],
        snapshots: Sequence[Mapping[str, Any]],
        policy_name: str,
    ) -> dict[str, Any]:
        policy = self._policy(portfolio, policy_name)
        evaluations = [
            evaluate_venture(snapshot, policy, policy_name=policy_name) for snapshot in snapshots
        ]
        return {
            "policy": policy,
            "evaluations": evaluations,
            "ranking": rank_evaluations(evaluations),
        }

    def evaluate(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        data = payload or {}
        policy = str(data.get("policy") or "balanced")
        snapshots = self._snapshots_for(portfolio, who, start=start, end=end)
        result = self._evaluate_rows(portfolio, snapshots, policy)
        batch = {
            "id": new_id("peval"),
            "policy": policy,
            "period": {
                "start": self._intervals(start, end)[0].isoformat(),
                "end": self._intervals(start, end)[1].isoformat(),
            },
            "evaluations": result["evaluations"],
            "ranking": result["ranking"],
            "methodology": (
                "Missing components are excluded from the raw weighted score and then "
                "penalized by evidence coverage; low-coverage ventures are not ranked."
            ),
            "generated_by": who,
            "generated_at": self._clock().isoformat(),
        }
        portfolio.setdefault("evaluations", []).append(batch)
        portfolio["evaluations"] = portfolio["evaluations"][-50:]
        self._event(
            portfolio, "ventures_evaluated", who, {"evaluation_id": batch["id"], "policy": policy}
        )
        self._persist()
        return batch

    def ranking(
        self,
        portfolio_id: str,
        *,
        policy: str = "",
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        chosen = str(policy or "")
        visible = self._visible_venture_ids(portfolio, who)
        candidates = [
            row
            for row in portfolio.get("evaluations") or []
            if isinstance(row, Mapping) and (not chosen or row.get("policy") == chosen)
        ]
        latest = candidates[-1] if candidates else None
        persisted_visible = False
        if isinstance(latest, Mapping):
            persisted_visible = all(
                str(row.get("venture_id")) in visible
                for row in latest.get("evaluations") or []
                if isinstance(row, Mapping)
            )
        if latest is None or not persisted_visible:
            snapshots = self._snapshots_for(portfolio, who)
            policy_name = chosen
            if not policy_name and isinstance(latest, Mapping):
                policy_name = str(latest.get("policy") or "balanced")
            live = self._evaluate_rows(portfolio, snapshots, policy_name or "balanced")
            return {
                "portfolio_id": portfolio_id,
                "policy": live["policy"],
                "ranking": live["ranking"],
                "evaluations": live["evaluations"],
                "persisted": False,
                "generated_at": self._clock().isoformat(),
            }
        return {
            "portfolio_id": portfolio_id,
            "policy": latest.get("policy"),
            "evaluation_id": latest.get("id"),
            "generated_at": latest.get("generated_at"),
            "ranking": latest.get("ranking", []),
            "evaluations": latest.get("evaluations", []),
            "persisted": True,
        }

    # -- risks ---------------------------------------------------------------
    def _compute_risks(
        self,
        portfolio: Mapping[str, Any],
        snapshots: Sequence[Mapping[str, Any]],
        *,
        visible: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        thresholds = dict(DEFAULT_MONITORING)
        thresholds.update(portfolio.get("monitoring") or {})
        thresholds["cash_runway_months"] = float(
            (portfolio.get("risk_preferences") or {}).get("min_cash_runway_months", 3.0)
        )
        thresholds["budget_overrun"] = float(
            (portfolio.get("risk_preferences") or {}).get("max_budget_overrun", 0.10)
        )
        risks: list[dict[str, Any]] = []
        resolved = {
            str(row.get("risk_id"))
            for row in portfolio.get("risk_resolutions") or []
            if isinstance(row, Mapping) and row.get("status") == "resolved"
        }
        for snapshot in snapshots:
            for risk in venture_risks(snapshot, thresholds=thresholds):
                risk["portfolio_id"] = portfolio.get("id")
                risk["created_at"] = risk.get("created_at") or snapshot.get("fetched_at", "")
                risk["updated_at"] = snapshot.get("fetched_at", "")
                if risk["id"] in resolved:
                    risk["status"] = "resolved"
                risks.append(risk)
        for row in portfolio.get("manual_risks") or []:
            if not isinstance(row, Mapping):
                continue
            if visible is not None and not self._record_visible(row, visible):
                continue
            risk = dict(row)
            risk["portfolio_id"] = portfolio.get("id")
            if risk.get("id") in resolved:
                risk["status"] = "resolved"
            risks.append(risk)
        return risks, correlated_risks(risks)

    def assess_risks(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        snapshots = self._snapshots_for(portfolio, who, start=start, end=end)
        visible = self._visible_venture_ids(portfolio, who)
        risks, correlated = self._compute_risks(portfolio, snapshots, visible=visible)
        # Keep hidden ventures' stored assessments; a restricted analyst must
        # not erase records they cannot see.
        hidden_risks = [
            dict(row)
            for row in portfolio.get("risks") or []
            if isinstance(row, Mapping) and not self._record_visible(row, visible)
        ]
        by_severity: dict[str, int] = {}
        for row in risks:
            if row.get("status") == "resolved":
                continue
            by_severity[str(row.get("severity"))] = by_severity.get(str(row.get("severity")), 0) + 1
        portfolio["risks"] = hidden_risks + risks
        portfolio["last_risk_assessment"] = self._clock().isoformat()
        self._event(
            portfolio, "risks_assessed", who, {"risks": len(risks), "correlated": len(correlated)}
        )
        self._persist()
        return {
            "portfolio_id": portfolio_id,
            "generated_at": portfolio["last_risk_assessment"],
            "risks": risks,
            "correlated_risks": correlated,
            "by_severity": by_severity,
            "diversification": {
                "claimed": False,
                "reason": "Portfolio risk is described from shared evidence, not a statistical diversification model.",
            },
            "methodology": (
                "Risks derive from posted financial actuals, Phase 38 operations/alerts, "
                "Phase 36 delivery evidence and Phase 37 customer analytics. Missing inputs "
                "create no risk claim."
            ),
        }

    def record_risk(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        self._assert_no_secrets(payload)
        category = str(payload.get("category") or "").lower()
        if category not in RISK_CATEGORIES:
            raise PortfolioValidationError(f"category must be one of: {', '.join(RISK_CATEGORIES)}")
        severity = str(payload.get("severity") or "medium").lower()
        if severity not in SEVERITIES:
            raise PortfolioValidationError(f"severity must be one of: {', '.join(SEVERITIES)}")
        evidence = [
            text(item, "evidence", 500, required=True)
            for item in bounded_rows(payload.get("evidence"), "evidence", 20)
        ]
        if not evidence:
            raise PortfolioValidationError("A recorded risk needs at least one evidence item.")
        venture_id = text(payload.get("venture_id"), "venture_id", 160)
        if venture_id:
            ref = self._venture_ref(portfolio, venture_id)
            if not self._can_view_venture(portfolio, who, ref):
                raise PortfolioAuthorizationError("The venture is restricted from this actor.")
        risk = {
            "id": new_id("prisk"),
            "portfolio_id": portfolio_id,
            "venture_id": venture_id,
            "venture_name": (
                self._venture_ref(portfolio, venture_id).get("name") if venture_id else ""
            ),
            "category": category,
            "scope": "venture" if venture_id else "portfolio",
            "severity": severity,
            "likelihood": text(payload.get("likelihood"), "likelihood", 100) or "recorded",
            "impact": text(payload.get("impact"), "impact", 300) or "recorded",
            "observed": text(payload.get("observed"), "observed", 500, required=True),
            "evidence": evidence,
            "mitigation": text(payload.get("mitigation"), "mitigation", 1000, required=True),
            "status": "open",
            "source": "manual",
            "created_at": self._clock().isoformat(),
            "updated_at": self._clock().isoformat(),
            "kind": "risk",
        }
        portfolio.setdefault("manual_risks", []).append(risk)
        self._event(portfolio, "risk_recorded", who, {"risk_id": risk["id"], "category": category})
        self._persist()
        return risk

    def list_risks(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        risks: list[dict[str, Any]] = [
            dict(row)
            for row in portfolio.get("risks") or []
            if isinstance(row, Mapping) and self._record_visible(row, visible)
        ]
        for row in portfolio.get("manual_risks") or []:
            if isinstance(row, Mapping) and self._record_visible(row, visible):
                risks.append(dict(row))
        resolved = {
            str(row.get("risk_id"))
            for row in portfolio.get("risk_resolutions") or []
            if isinstance(row, Mapping) and row.get("status") == "resolved"
        }
        for row in risks:
            if row.get("id") in resolved:
                row["status"] = "resolved"
        return {
            "portfolio_id": portfolio_id,
            "assessed_at": portfolio.get("last_risk_assessment", ""),
            "risks": risks,
            "correlated_risks": correlated_risks(risks),
        }

    def resolve_risk(
        self,
        portfolio_id: str,
        risk_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        found = any(
            isinstance(row, Mapping) and str(row.get("id")) == risk_id
            for row in list(portfolio.get("risks") or [])
            + list(portfolio.get("manual_risks") or [])
        )
        if not found:
            raise PortfolioRecordNotFound("risk", risk_id)
        self._assert_no_secrets(payload)
        visible = self._visible_venture_ids(portfolio, who)
        target_rows = [
            row
            for row in list(portfolio.get("risks") or [])
            + list(portfolio.get("manual_risks") or [])
            if isinstance(row, Mapping) and str(row.get("id")) == risk_id
        ]
        if target_rows and not self._record_visible(target_rows[0], visible):
            raise PortfolioAuthorizationError("The risk is restricted from this actor.")
        note = text(payload.get("note"), "note", 1000, required=True)
        row = {
            "risk_id": risk_id,
            "status": "resolved",
            "note": note,
            "resolved_by": who,
            "resolved_at": self._clock().isoformat(),
        }
        portfolio.setdefault("risk_resolutions", []).append(row)
        for item in portfolio.get("risks") or []:
            if isinstance(item, dict) and item.get("id") == risk_id:
                item["status"] = "resolved"
        for item in portfolio.get("manual_risks") or []:
            if isinstance(item, dict) and item.get("id") == risk_id:
                item["status"] = "resolved"
        self._event(portfolio, "risk_resolved", who, {"risk_id": risk_id})
        self._persist()
        return row

    # -- allocation ----------------------------------------------------------
    def _allocation_items(
        self,
        portfolio: Mapping[str, Any],
        snapshots: Sequence[Mapping[str, Any]],
        *,
        visible: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        thresholds = dict(DEFAULT_MONITORING)
        thresholds["cash_runway_months"] = float(
            (portfolio.get("risk_preferences") or {}).get("min_cash_runway_months", 3.0)
        )
        thresholds["budget_overrun"] = float(
            (portfolio.get("risk_preferences") or {}).get("max_budget_overrun", 0.10)
        )
        items = []
        for snapshot in snapshots:
            risks, _ = self._compute_risks(portfolio, [snapshot], visible=visible)
            item = allocation_recommendation(snapshot, thresholds=thresholds)
            item["portfolio_id"] = portfolio.get("id")
            item["supporting_risks"] = [
                {
                    "id": row.get("id"),
                    "category": row.get("category"),
                    "severity": row.get("severity"),
                }
                for row in risks
                if row.get("status") != "resolved"
            ]
            items.append(item)
        return items

    def propose_allocations(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        snapshots = self._snapshots_for(portfolio, who, start=start, end=end)
        visible = self._visible_venture_ids(portfolio, who)
        items = self._allocation_items(portfolio, snapshots, visible=visible)
        improvements = [row for row in items if row.get("action") == "increase"]
        reductions = [row for row in items if row.get("action") in {"reduce", "pause"}]
        reallocation = None
        if improvements and reductions:
            source = reductions[0]
            target = improvements[0]
            reallocation = {
                "id": new_id("alloc"),
                "portfolio_id": portfolio_id,
                "venture_id": target.get("venture_id"),
                "venture_name": f"{source.get('venture_name')} → {target.get('venture_name')}",
                "action": "reallocate",
                "rationale": (
                    "A venture with weaker recorded evidence is proposed for reduced allocation "
                    "while a venture with stronger observed growth is a candidate for additional "
                    "approved budget."
                ),
                "evidence": list(source.get("evidence") or []) + list(target.get("evidence") or []),
                "expected_benefits": [
                    "Directs future approved budget toward better-evidenced growth."
                ],
                "estimated_costs": [
                    "No amount is set by this proposal; a Phase 38 budget decision is required."
                ],
                "risks": [
                    "Reallocation can damage the reduced venture if executed without a transition plan."
                ],
                "assumptions": ["Both ventures remain separate legal ledgers; no funds move here."],
                "alternatives": ["maintain", "delay", "validate"],
                "confidence": "low",
                "approval_required": True,
                "approval_policy": "Human portfolio approver and a separate Phase 38 budget approval.",
                "applied": False,
                "source_venture_id": source.get("venture_id"),
                "target_venture_id": target.get("venture_id"),
                "created_at": self._clock().isoformat(),
                "kind": "recommendation",
            }
        batch = {
            "id": new_id("palloc"),
            "portfolio_id": portfolio_id,
            "items": items + ([reallocation] if reallocation else []),
            "reallocation": reallocation,
            "generated_by": who,
            "generated_at": self._clock().isoformat(),
            "status": "proposed",
            "note": "No recommendation changes a Phase 38 budget or moves money. Each item needs human approval.",
        }
        portfolio.setdefault("allocation_batches", []).append(batch)
        portfolio["allocation_batches"] = portfolio["allocation_batches"][-50:]
        self._event(portfolio, "allocations_proposed", who, {"batch_id": batch["id"]})
        self._persist()
        return batch

    def list_allocations(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        batches = [
            self._filter_batch(row, visible, "items")
            for row in portfolio.get("allocation_batches") or []
            if isinstance(row, Mapping)
        ]
        return {
            "portfolio_id": portfolio_id,
            "batches": [
                row for row in batches if row.get("items") or not row.get("filtered_for_visibility")
            ],
            "resources": self._resource_status(portfolio, visible=visible),
            "note": "Proposed allocations are recommendations, not authorized spending.",
        }

    def decide_allocation(
        self,
        portfolio_id: str,
        batch_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        batch = next(
            (
                row
                for row in portfolio.get("allocation_batches") or []
                if isinstance(row, dict) and row.get("id") == batch_id
            ),
            None,
        )
        if batch is None:
            raise PortfolioRecordNotFound("allocation batch", batch_id)
        visible = self._visible_venture_ids(portfolio, who)
        if any(
            isinstance(item, Mapping) and not self._record_visible(item, visible)
            for item in batch.get("items") or []
        ):
            raise PortfolioAuthorizationError(
                "The allocation batch contains a venture restricted from this actor."
            )
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise PortfolioValidationError("approved must be true or false.")
        self._assert_no_secrets(payload)
        note = text(payload.get("note"), "note", 1000, required=True)
        batch["status"] = "approved" if approved else "rejected"
        batch["decided_by"] = who
        batch["decided_at"] = self._clock().isoformat()
        batch["decision_note"] = note
        batch["applied"] = False
        batch["application_path"] = (
            "An approved allocation is still only a recommendation. Use the Phase 38 budget "
            "workflow for any actual budget change; no funds were moved."
        )
        for item in batch.get("items") or []:
            if isinstance(item, dict):
                item["status"] = "approved" if approved else "rejected"
                item["applied"] = False
        self._event(
            portfolio, "allocation_decided", who, {"batch_id": batch_id, "approved": approved}
        )
        self._persist()
        return batch

    # -- shared resources and coordination ----------------------------------
    def _resource_status(
        self, portfolio: Mapping[str, Any], *, visible: set[str] | None = None
    ) -> dict[str, Any]:
        rows = []
        conflicts = []
        for resource in portfolio.get("resources") or []:
            if not isinstance(resource, Mapping):
                continue
            capacity = float(resource.get("capacity") or 0.0)
            allocations = [
                dict(row)
                for row in resource.get("allocations") or []
                if isinstance(row, Mapping)
                and (
                    visible is None
                    or not row.get("venture_id")
                    or str(row.get("venture_id")) in visible
                )
            ]
            assigned = sum(float(row.get("quantity") or 0.0) for row in allocations)
            row = {
                "id": resource.get("id"),
                "name": resource.get("name"),
                "unit": resource.get("unit"),
                "capacity": capacity,
                "allocated": round(assigned, 4),
                "utilization": round(assigned / capacity, 4) if capacity > 0 else None,
                "allocations": allocations,
                "cost_per_unit": resource.get("cost_per_unit"),
                "currency": resource.get("currency"),
                "cost_note": "Cost is an input for planning, not a recorded actual.",
            }
            rows.append(row)
            if capacity > 0 and assigned > capacity:
                conflicts.append(
                    {
                        "resource_id": resource.get("id"),
                        "name": resource.get("name"),
                        "over_by": round(assigned - capacity, 4),
                        "ventures": [item.get("venture_id") for item in allocations],
                    }
                )
        return {
            "resources": rows,
            "conflicts": conflicts,
            "utilization_available": any(row.get("utilization") is not None for row in rows),
        }

    def add_resource(
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
        name = text(payload.get("name"), "name", 160, required=True)
        capacity = number(payload.get("capacity"), "capacity", minimum=0.000001)
        unit = text(payload.get("unit"), "unit", 40) or "unit"
        currency_code = currency(payload.get("currency") or portfolio.get("base_currency"))
        row = {
            "id": new_id("res"),
            "name": name,
            "capacity": capacity,
            "unit": unit,
            "cost_per_unit": number(
                payload.get("cost_per_unit"), "cost_per_unit", minimum=0, default=0
            ),
            "currency": currency_code,
            "allocations": [],
            "created_at": self._clock().isoformat(),
            "created_by": who,
        }
        portfolio.setdefault("resources", []).append(row)
        self._event(portfolio, "resource_added", who, {"resource_id": row["id"]})
        self._persist()
        return row

    def allocate_resource(
        self,
        portfolio_id: str,
        resource_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "admin")
        self._assert_no_secrets(payload)
        resource = next(
            (
                row
                for row in portfolio.get("resources") or []
                if isinstance(row, dict) and row.get("id") == resource_id
            ),
            None,
        )
        if resource is None:
            raise PortfolioRecordNotFound("resource", resource_id)
        venture_id = text(payload.get("venture_id"), "venture_id", 160, required=True)
        self._venture_ref(portfolio, venture_id)
        quantity = number(payload.get("quantity"), "quantity", minimum=0)
        capacity = float(resource.get("capacity") or 0.0)
        assigned = sum(
            float(row.get("quantity") or 0.0)
            for row in resource.get("allocations") or []
            if isinstance(row, Mapping)
        )
        if assigned + quantity > capacity:
            raise PortfolioConflict(
                f"Allocating {quantity} {resource.get('unit')} would exceed "
                f"{resource.get('name')} capacity ({capacity})."
            )
        existing = next(
            (
                row
                for row in resource["allocations"]
                if isinstance(row, dict) and row.get("venture_id") == venture_id
            ),
            None,
        )
        now = self._clock().isoformat()
        if existing:
            existing["quantity"] = round(float(existing.get("quantity") or 0.0) + quantity, 4)
            existing["updated_at"] = now
        else:
            resource["allocations"].append(
                {
                    "venture_id": venture_id,
                    "quantity": quantity,
                    "allocated_at": now,
                    "allocated_by": who,
                }
            )
        self._event(
            portfolio,
            "resource_allocated",
            who,
            {"resource_id": resource_id, "venture_id": venture_id},
        )
        self._persist()
        return self._resource_status(portfolio)

    def add_dependency(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        self._assert_no_secrets(payload)
        source = text(payload.get("from_venture_id"), "from_venture_id", 160, required=True)
        target = text(payload.get("to_venture_id"), "to_venture_id", 160, required=True)
        if source == target:
            raise PortfolioValidationError(
                "A cross-venture dependency needs two different ventures."
            )
        source_ref = self._venture_ref(portfolio, source)
        target_ref = self._venture_ref(portfolio, target)
        if not self._can_view_venture(portfolio, who, source_ref) or not self._can_view_venture(
            portfolio, who, target_ref
        ):
            raise PortfolioAuthorizationError(
                "A dependency endpoint is restricted from this actor."
            )
        description = text(payload.get("description"), "description", 500, required=True)
        if any(
            isinstance(row, Mapping)
            and row.get("from_venture_id") == source
            and row.get("to_venture_id") == target
            and row.get("status") == "open"
            for row in portfolio.get("dependencies") or []
        ):
            raise PortfolioConflict("That cross-venture dependency is already recorded.")
        row = {
            "id": new_id("dep"),
            "from_venture_id": source,
            "to_venture_id": target,
            "description": description,
            "owner": text(payload.get("owner"), "owner", 120),
            "status": "open",
            "created_at": self._clock().isoformat(),
            "created_by": who,
        }
        portfolio.setdefault("dependencies", []).append(row)
        self._event(portfolio, "cross_venture_dependency_added", who, {"dependency_id": row["id"]})
        self._persist()
        return row

    @staticmethod
    def _dependency_cycles(dependencies: Sequence[Mapping[str, Any]]) -> list[list[str]]:
        graph: dict[str, list[str]] = {}
        for row in dependencies:
            if row.get("status") != "open":
                continue
            graph.setdefault(str(row.get("from_venture_id")), []).append(
                str(row.get("to_venture_id"))
            )
        cycles: list[list[str]] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def walk(node: str) -> None:
            if node in visiting:
                start = visiting.index(node)
                cycles.append(visiting[start:] + [node])
                return
            if node in visited:
                return
            visiting.append(node)
            for child in graph.get(node, []):
                walk(child)
            visiting.pop()
            visited.add(node)

        for node in list(graph):
            walk(node)
        unique: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for cycle in cycles:
            key = tuple(sorted(cycle))
            if key not in seen:
                seen.add(key)
                unique.append(cycle)
        return unique

    def coordination(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        snapshots = self._snapshots_for(portfolio, who, start=start, end=end)
        visible = self._visible_venture_ids(portfolio, who)
        risks, _ = self._compute_risks(portfolio, snapshots, visible=visible)
        dependencies = [
            dict(row)
            for row in portfolio.get("dependencies") or []
            if isinstance(row, Mapping)
            and self._record_visible(row, visible, ("from_venture_id", "to_venture_id"))
        ]
        tasks = []
        for snapshot in snapshots:
            operations = (
                snapshot.get("finance", {}).get("operations", {})
                if isinstance(snapshot.get("finance"), Mapping)
                else {}
            )
            tasks.append(
                {
                    "venture_id": snapshot.get("venture_id"),
                    "venture_name": snapshot.get("name"),
                    "priority": self._priority(portfolio, str(snapshot.get("venture_id"))),
                    "tasks": operations.get("tasks", 0),
                    "completed": operations.get("completed", 0),
                    "blocked": operations.get("blocked", 0),
                    "overdue": operations.get("overdue", 0),
                    "dependency_issues": operations.get("dependency_issues", []),
                    "source": "phase38.operations",
                }
            )
        responsibilities = {
            "portfolio_manager": {
                "role": "business",
                "assigned_agent": "portfolio-manager-agent",
                "responsibility": "portfolio synthesis, ranking and decision preparation",
            },
            "venture_operations_manager": {
                "role": "project_manager",
                "assigned_agent": "project-manager-agent",
                "responsibility": "venture task coordination and progress aggregation",
            },
            "financial_analyst": {
                "role": "data_analysis",
                "assigned_agent": "financial-intelligence-agent",
                "responsibility": "posted actuals, budgets, cash and runway",
            },
            "product_manager": {
                "role": "planning",
                "assigned_agent": "product-development-agent",
                "responsibility": "Phase 36 readiness and delivery evidence",
            },
            "growth_analyst": {
                "role": "business",
                "assigned_agent": "customer-growth-agent",
                "responsibility": "Phase 37 acquisition, conversion and retention",
            },
            "risk_analyst": {
                "role": "review",
                "assigned_agent": "review-agent",
                "responsibility": "risk evidence and mitigation tracking",
            },
            "strategy_analyst": {
                "role": "planning",
                "assigned_agent": "coordinator-agent",
                "responsibility": "scenario comparison and strategic alternatives",
            },
        }
        return {
            "portfolio_id": portfolio_id,
            "responsibilities": responsibilities,
            "ventures": tasks,
            "dependencies": dependencies,
            "dependency_cycles": self._dependency_cycles(dependencies),
            "resources": self._resource_status(
                portfolio, visible=self._visible_venture_ids(portfolio, who)
            ),
            "escalations": [
                row
                for row in risks
                if row.get("status") != "resolved" and row.get("severity") in {"high", "critical"}
            ],
            "priority_order": sorted(
                tasks,
                key=lambda row: (int(row.get("priority") or 5), row.get("venture_name") or ""),
            ),
            "isolation": {
                "actor": who,
                "visible_ventures": len(snapshots),
                "hidden_ventures": len(portfolio.get("ventures") or []) - len(snapshots),
                "restricted_data_shared": False,
            },
            "note": (
                "Responsibilities reuse existing agent roles; this is a coordination view, "
                "not a second agent runtime. Restricted venture details are not returned."
            ),
        }

    def _priority(self, portfolio: Mapping[str, Any], venture_id: str) -> int:
        for row in portfolio.get("ventures") or []:
            if isinstance(row, Mapping) and str(row.get("id")) == venture_id:
                return int(row.get("priority") or 3)
        return 5

    # -- scenarios -----------------------------------------------------------
    def simulate_scenario(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        scenario_type = str(payload.get("type") or "custom").lower()
        if scenario_type not in SCENARIO_TYPES:
            raise PortfolioValidationError(f"type must be one of: {', '.join(SCENARIO_TYPES)}")
        target = text(payload.get("venture_id"), "venture_id", 160)
        if target:
            self._venture_ref(portfolio, target)
        assumptions = payload.get("assumptions")
        if assumptions is not None and not isinstance(assumptions, Mapping):
            raise PortfolioValidationError("assumptions must be an object.")
        self._assert_no_secrets(payload)
        horizon = int(
            number(
                payload.get("horizon_months"), "horizon_months", minimum=1, maximum=36, default=12
            )
        )
        snapshots = self._snapshots_for(portfolio, who)
        result = build_scenario(
            snapshots,
            scenario_type=scenario_type,
            target_venture_id=target,
            horizon_months=horizon,
            assumptions=dict(assumptions or {}),
            base_currency=str(portfolio.get("base_currency") or "USD"),
            fx_rates=self._rates(portfolio),
            thresholds=portfolio.get("monitoring") or DEFAULT_MONITORING,
        )
        result["id"] = new_id("pscen")
        result["portfolio_id"] = portfolio_id
        result["created_by"] = who
        result["created_at"] = self._clock().isoformat()
        portfolio.setdefault("scenarios", []).append(result)
        portfolio["scenarios"] = portfolio["scenarios"][-50:]
        self._event(
            portfolio,
            "scenario_simulated",
            who,
            {"scenario_id": result["id"], "type": scenario_type},
        )
        self._persist()
        return result

    def list_scenarios(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        return {
            "portfolio_id": portfolio_id,
            "scenarios": [
                dict(row)
                for row in portfolio.get("scenarios") or []
                if isinstance(row, Mapping) and self._scenario_visible(row, visible)
            ],
            "note": "Every scenario is simulated; none is actual performance.",
        }

    # -- strategic recommendations and decisions -----------------------------
    def generate_recommendations(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        snapshots = self._snapshots_for(portfolio, who, start=start, end=end)
        visible = self._visible_venture_ids(portfolio, who)
        allocations = self._allocation_items(portfolio, snapshots, visible=visible)
        by_venture = {row.get("venture_id"): row for row in allocations}
        recommendations = []
        for snapshot in snapshots:
            risks, _ = self._compute_risks(portfolio, [snapshot], visible=visible)
            recommendation = recommendation_from_metrics(
                snapshot,
                allocation=by_venture.get(snapshot.get("venture_id"), {}),
                risks=risks,
            )
            recommendation["portfolio_id"] = portfolio_id
            recommendations.append(recommendation)
        batch = {
            "id": new_id("prec"),
            "portfolio_id": portfolio_id,
            "recommendations": recommendations,
            "generated_by": who,
            "generated_at": self._clock().isoformat(),
            "note": "Recommendations are advisory and create no decision or financial action.",
        }
        portfolio.setdefault("recommendations", []).append(batch)
        portfolio["recommendations"] = portfolio["recommendations"][-50:]
        self._event(portfolio, "recommendations_generated", who, {"batch_id": batch["id"]})
        self._persist()
        return batch

    def list_recommendations(
        self, portfolio_id: str, *, actor: str | None = None
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        batches = [
            self._filter_batch(row, visible, "recommendations", keys=("venture_id",))
            for row in portfolio.get("recommendations") or []
            if isinstance(row, Mapping)
        ]
        return {
            "portfolio_id": portfolio_id,
            "batches": [
                row
                for row in batches
                if row.get("recommendations") or not row.get("filtered_for_visibility")
            ],
            "note": "A recommendation is not a decision and changes no resource allocation.",
        }

    def propose_decision(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        self._assert_no_secrets(payload)
        kind = str(payload.get("kind") or "").lower()
        if kind not in DECISION_KINDS:
            raise PortfolioValidationError(f"kind must be one of: {', '.join(DECISION_KINDS)}")
        venture_id = text(payload.get("venture_id"), "venture_id", 160)
        if venture_id:
            ref = self._venture_ref(portfolio, venture_id)
            if not self._can_view_venture(portfolio, who, ref):
                raise PortfolioAuthorizationError("The venture is restricted from this actor.")
        evidence = [
            text(item, "evidence", 500, required=True)
            for item in bounded_rows(payload.get("evidence"), "evidence", 30)
        ]
        if not evidence:
            raise PortfolioValidationError("A strategic decision proposal needs evidence.")
        row = {
            "id": new_id("pdec"),
            "portfolio_id": portfolio_id,
            "venture_id": venture_id,
            "kind": kind,
            "title": text(payload.get("title"), "title", 200, required=True),
            "context": text(payload.get("context"), "context", 2000),
            "evidence": evidence,
            "financial_implications": [
                text(item, "financial implication", 500)
                for item in bounded_rows(
                    payload.get("financial_implications"), "financial_implications", 20
                )
            ],
            "operational_implications": [
                text(item, "operational implication", 500)
                for item in bounded_rows(
                    payload.get("operational_implications"), "operational_implications", 20
                )
            ],
            "risks": [
                text(item, "risk", 500) for item in bounded_rows(payload.get("risks"), "risks", 20)
            ],
            "alternatives": [
                text(item, "alternative", 500)
                for item in bounded_rows(payload.get("alternatives"), "alternatives", 20)
            ],
            "assumptions": [
                text(item, "assumption", 500)
                for item in bounded_rows(payload.get("assumptions"), "assumptions", 20)
            ],
            "expected_outcomes": [
                text(item, "outcome", 500)
                for item in bounded_rows(payload.get("expected_outcomes"), "expected_outcomes", 20)
            ],
            "confidence": text(payload.get("confidence"), "confidence", 40) or "low",
            "status": "proposed",
            "approval_required": True,
            "approval_policy": "A named portfolio approver must approve or reject; no automatic execution.",
            "recommendation_id": text(payload.get("recommendation_id"), "recommendation_id", 160),
            "created_by": who,
            "created_at": self._clock().isoformat(),
            "applied": False,
        }
        portfolio.setdefault("decisions", []).append(row)
        portfolio["decisions"] = portfolio["decisions"][-self.max_records :]
        self._event(portfolio, "decision_proposed", who, {"decision_id": row["id"], "kind": kind})
        self._persist()
        return row

    def list_decisions(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        decisions = [
            dict(row)
            for row in portfolio.get("decisions") or []
            if isinstance(row, Mapping) and self._record_visible(row, visible)
        ]
        return {
            "portfolio_id": portfolio_id,
            "decisions": decisions,
            "pending": [row for row in decisions if row.get("status") == "proposed"],
        }

    def decide(
        self,
        portfolio_id: str,
        decision_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        row = next(
            (
                item
                for item in portfolio.get("decisions") or []
                if isinstance(item, dict) and item.get("id") == decision_id
            ),
            None,
        )
        if row is None:
            raise PortfolioRecordNotFound("decision", decision_id)
        if not self._record_visible(row, self._visible_venture_ids(portfolio, who)):
            raise PortfolioAuthorizationError("The decision is restricted from this actor.")
        if row.get("status") != "proposed":
            raise PortfolioConflict("Only a proposed decision can be approved or rejected.")
        self._assert_no_secrets(payload)
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise PortfolioValidationError("approved must be true or false.")
        note = text(payload.get("note"), "note", 1000, required=True)
        row["status"] = "approved" if approved else "rejected"
        row["decided_by"] = who
        row["decided_at"] = self._clock().isoformat()
        row["decision_note"] = note
        row["applied"] = False
        row["application_note"] = (
            "This authorizes the portfolio decision only. A lifecycle transition, budget change "
            "or external action still needs its own route and approval; nothing was executed."
        )
        self._event(
            portfolio,
            "decision_approved" if approved else "decision_rejected",
            who,
            {"decision_id": decision_id},
        )
        self._persist()
        return row

    # -- knowledge -----------------------------------------------------------
    def add_knowledge(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        self._assert_no_secrets(payload)
        self._assert_no_customer_data(payload)
        kind = str(payload.get("kind") or "").lower()
        if kind not in KNOWLEDGE_KINDS:
            raise PortfolioValidationError(f"kind must be one of: {', '.join(KNOWLEDGE_KINDS)}")
        venture_id = text(payload.get("venture_id"), "venture_id", 160)
        if venture_id:
            ref = self._venture_ref(portfolio, venture_id)
            if not self._can_view_venture(portfolio, who, ref):
                raise PortfolioAuthorizationError("The venture is restricted from this actor.")
        visibility = str(payload.get("visibility") or "portfolio").lower()
        if visibility not in {"portfolio", "venture"}:
            raise PortfolioValidationError("visibility must be portfolio or venture.")
        classification = str(payload.get("classification") or "internal").lower()
        if classification not in {"internal", "confidential_customer"}:
            raise PortfolioValidationError(
                "classification must be internal or confidential_customer."
            )
        evidence = [
            text(item, "evidence", 500)
            for item in bounded_rows(payload.get("evidence"), "evidence", 20)
        ]
        if kind == "verified_fact" and not evidence:
            raise PortfolioValidationError("A verified fact needs at least one evidence item.")
        row: dict[str, Any] = {
            "id": new_id("pknow"),
            "portfolio_id": portfolio_id,
            "venture_id": venture_id,
            "kind": kind,
            "title": text(payload.get("title"), "title", 200, required=True),
            "body": text(payload.get("body"), "body", 4000, required=True),
            "evidence": evidence,
            "source": text(payload.get("source"), "source", 500),
            "visibility": visibility,
            "classification": classification,
            "authorized_for_cross_venture": bool(
                payload.get("authorized_for_cross_venture", False)
            ),
            "created_by": who,
            "created_at": self._clock().isoformat(),
            "promoted": False,
            "provenance": {
                "kind": kind,
                "source": text(payload.get("source"), "source", 500),
                "recorded_by": who,
                "recorded_at": self._clock().isoformat(),
            },
        }
        portfolio.setdefault("knowledge", []).append(row)
        portfolio["knowledge"] = portfolio["knowledge"][-self.max_records :]
        self._event(portfolio, "knowledge_recorded", who, {"knowledge_id": row["id"], "kind": kind})
        self._persist()
        self._ingest_knowledge(row["title"], row["body"], "portfolio")
        return row

    def list_knowledge(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        venture_id: str = "",
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible_ventures = {str(ref.get("id")) for ref in self._visible_refs(portfolio, who)}
        rows = []
        for row in portfolio.get("knowledge") or []:
            if not isinstance(row, Mapping):
                continue
            if venture_id and row.get("venture_id") != venture_id:
                continue
            if row.get("venture_id") and str(row.get("venture_id")) not in visible_ventures:
                # An explicitly promoted cross-venture record may still be
                # shared, but never with its source venture id attached.
                if not (
                    row.get("promoted") is True and row.get("authorized_for_cross_venture") is True
                ):
                    continue
                redacted = dict(row)
                redacted["venture_id"] = ""
                redacted["source_venture_restricted"] = True
                rows.append(redacted)
                continue
            if (
                row.get("classification") == "confidential_customer"
                and self._role(portfolio, who) != "admin"
            ):
                continue
            rows.append(dict(row))
        return {
            "portfolio_id": portfolio_id,
            "knowledge": rows,
            "provenance_rule": (
                "Verified facts, observations, interpretations, hypotheses and recommendations "
                "are distinct labels. A recommendation is never a verified fact."
            ),
        }

    def promote_knowledge(
        self,
        portfolio_id: str,
        knowledge_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "approver")
        row = next(
            (
                item
                for item in portfolio.get("knowledge") or []
                if isinstance(item, dict) and item.get("id") == knowledge_id
            ),
            None,
        )
        if row is None:
            raise PortfolioRecordNotFound("knowledge", knowledge_id)
        self._assert_no_secrets(payload)
        if not self._record_visible(row, self._visible_venture_ids(portfolio, who)):
            raise PortfolioAuthorizationError("The knowledge record is restricted from this actor.")
        authorized = payload.get("authorized_for_cross_venture")
        if row.get("classification") == "confidential_customer" and authorized is not True:
            raise PortfolioAuthorizationError(
                "Confidential customer knowledge needs explicit authorization before cross-venture use."
            )
        note = text(payload.get("note"), "note", 1000, required=True)
        row["promoted"] = True
        row["visibility"] = "portfolio"
        row["authorized_for_cross_venture"] = authorized is True
        row["promotion_note"] = note
        row["promoted_by"] = who
        row["promoted_at"] = self._clock().isoformat()
        self._event(portfolio, "knowledge_promoted", who, {"knowledge_id": knowledge_id})
        self._persist()
        return row

    # -- alerts and monitoring ----------------------------------------------
    def _alert_row(
        self,
        portfolio: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        metric_name: str,
        observed: str,
        severity: str,
        evidence: Sequence[str],
        action: str,
    ) -> dict[str, Any]:
        now = self._clock().isoformat()
        return {
            "id": new_id("palert"),
            "portfolio_id": portfolio.get("id"),
            "venture_id": snapshot.get("venture_id"),
            "metric": metric_name,
            "observed": observed,
            "severity": severity,
            "evidence": list(evidence),
            "timestamp": now,
            "recommended_action": action,
            "status": "open",
            "kind": "threshold",
            "first_seen": now,
            "last_seen": now,
            "occurrences": 1,
        }

    def refresh_alerts(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
        system: bool = False,
    ) -> dict[str, Any]:
        """Refresh derived alerts.

        ``system=True`` is reserved for the runtime's scheduled monitor. It is
        deliberately not reachable from an HTTP route: every request path must
        still carry an authenticated analyst-or-higher actor.
        """
        portfolio = self._get(portfolio_id)
        if system:
            who = "system:portfolio-monitor"
            refs = [row for row in portfolio.get("ventures") or [] if isinstance(row, dict)]
            visible = {str(row.get("id")) for row in refs}
            snapshots = [self._snapshot(portfolio, ref, start=start, end=end) for ref in refs]
        else:
            who = self._actor(actor)
            self._authorize(portfolio, who, "analyst")
            visible = self._visible_venture_ids(portfolio, who)
            snapshots = self._snapshots_for(portfolio, who, start=start, end=end)
        risks, _ = self._compute_risks(portfolio, snapshots, visible=visible)
        thresholds = dict(DEFAULT_MONITORING)
        thresholds.update(portfolio.get("monitoring") or {})
        existing: dict[tuple[str, str], dict[str, Any]] = {}
        for row in portfolio.get("alerts") or []:
            if isinstance(row, dict) and row.get("status") == "open":
                # Keep the stored row, not a copy: an occurrence update must
                # reach the persisted alert rather than a throwaway mapping.
                existing[(str(row.get("venture_id")), str(row.get("metric")))] = row
        created = updated = suppressed = 0
        for snapshot in snapshots:
            if not snapshot.get("fresh"):
                suppressed += 1
                continue
            for risk in risks:
                if risk.get("venture_id") != snapshot.get("venture_id"):
                    continue
                if risk.get("status") == "resolved" or risk.get("severity") not in {
                    "high",
                    "critical",
                }:
                    continue
                key = (str(snapshot.get("venture_id")), str(risk.get("category")))
                if key in existing:
                    alert = existing[key]
                    alert["observed"] = risk.get("observed")
                    alert["severity"] = risk.get("severity")
                    alert["evidence"] = risk.get("evidence")
                    alert["last_seen"] = self._clock().isoformat()
                    alert["occurrences"] = int(alert.get("occurrences") or 1) + 1
                    updated += 1
                    continue
                alert = self._alert_row(
                    portfolio,
                    snapshot,
                    metric_name=str(risk.get("category")),
                    observed=str(risk.get("observed") or ""),
                    severity=str(risk.get("severity") or "high"),
                    evidence=[str(item) for item in risk.get("evidence") or []],
                    action=str(risk.get("mitigation") or "Review the recorded risk."),
                )
                alert["risk_id"] = risk.get("id")
                portfolio.setdefault("alerts", []).append(alert)
                existing[key] = alert
                created += 1
            metrics = venture_metrics(snapshot)
            technical = metrics.get("technical")
            t = technical if isinstance(technical, Mapping) else {}
            conversion = t.get("conversion")
            if conversion is not None and float(conversion) < float(
                thresholds.get("sales_conversion", 0.10)
            ):
                key = (str(snapshot.get("venture_id")), "sales_performance")
                if key not in existing:
                    portfolio.setdefault("alerts", []).append(
                        self._alert_row(
                            portfolio,
                            snapshot,
                            metric_name="sales_performance",
                            observed=f"recorded conversion {float(conversion):.1%}",
                            severity="medium",
                            evidence=[
                                f"Phase 37 conversion {float(conversion):.1%} is below the configured threshold."
                            ],
                            action="Review qualification and sales evidence; do not launch an unapproved campaign.",
                        )
                    )
                    existing[key] = portfolio["alerts"][-1]
                    created += 1
            overdue = (
                snapshot.get("finance", {}).get("operations", {}).get("overdue")
                if isinstance(snapshot.get("finance"), Mapping)
                else None
            )
            if overdue is not None and int(overdue) > 0:
                key = (str(snapshot.get("venture_id")), "delivery_delay")
                if key not in existing:
                    portfolio.setdefault("alerts", []).append(
                        self._alert_row(
                            portfolio,
                            snapshot,
                            metric_name="delivery_delay",
                            observed=f"{int(overdue)} overdue operational task(s)",
                            severity="high"
                            if int(overdue) >= int(thresholds.get("product_delay_days", 14))
                            else "medium",
                            evidence=[f"Phase 38 reports {int(overdue)} overdue task(s)."],
                            action="Escalate the task owner and review the dependency.",
                        )
                    )
                    existing[key] = portfolio["alerts"][-1]
                    created += 1
        portfolio["alerts"] = portfolio["alerts"][-self.max_records :]
        summary = self.alert_summary(portfolio, visible=visible)
        summary.update(
            {
                "created": created,
                "updated": updated,
                "suppressed_stale_ventures": suppressed,
                "checked_at": self._clock().isoformat(),
                "source_timestamps": [
                    {
                        "venture_id": row.get("venture_id"),
                        "source_updated_at": row.get("source_updated_at"),
                    }
                    for row in snapshots
                ],
                "note": "Stale source data produces no threshold alert; the suppression count is reported.",
            }
        )
        self._event(
            portfolio, "alerts_refreshed", who, {"created": created, "open": summary["open"]}
        )
        self._persist()
        return summary

    def alert_summary(
        self, portfolio: Mapping[str, Any], *, visible: set[str] | None = None
    ) -> dict[str, Any]:
        rows = [
            dict(row)
            for row in portfolio.get("alerts") or []
            if isinstance(row, Mapping)
            and (
                visible is None
                or not row.get("venture_id")
                or str(row.get("venture_id")) in visible
            )
        ]
        open_rows = [row for row in rows if row.get("status") == "open"]
        by_severity: dict[str, int] = {}
        for row in open_rows:
            by_severity[str(row.get("severity"))] = by_severity.get(str(row.get("severity")), 0) + 1
        return {
            "portfolio_id": portfolio.get("id"),
            "open": len(open_rows),
            "resolved": len([row for row in rows if row.get("status") == "resolved"]),
            "by_severity": by_severity,
            "alerts": rows[-100:],
            "monitoring": dict(portfolio.get("monitoring") or DEFAULT_MONITORING),
        }

    def list_alerts(
        self,
        portfolio_id: str,
        *,
        status: str = "",
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        rows = [
            row
            for row in self.alert_summary(portfolio, visible=visible)["alerts"]
            if not status or row.get("status") == status
        ]
        return {"portfolio_id": portfolio_id, "alerts": rows}

    def resolve_alert(
        self,
        portfolio_id: str,
        alert_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        row = next(
            (
                item
                for item in portfolio.get("alerts") or []
                if isinstance(item, dict) and item.get("id") == alert_id
            ),
            None,
        )
        if row is None:
            raise PortfolioRecordNotFound("alert", alert_id)
        self._assert_no_secrets(payload)
        if not self._record_visible(row, self._visible_venture_ids(portfolio, who)):
            raise PortfolioAuthorizationError("The alert is restricted from this actor.")
        row["status"] = "resolved"
        row["resolution"] = text(payload.get("note"), "note", 1000, required=True)
        row["resolved_by"] = who
        row["resolved_at"] = self._clock().isoformat()
        self._event(portfolio, "alert_resolved", who, {"alert_id": alert_id})
        self._persist()
        return row

    # -- reports -------------------------------------------------------------
    def _report_period(self, kind: str, payload: Mapping[str, Any]) -> tuple[date, date]:
        today = self._clock().date()
        value = str(kind or "custom").lower()
        if value == "daily":
            return today, today
        if value == "weekly":
            return today - timedelta(days=today.weekday()), today
        if value == "monthly":
            return today.replace(day=1), today
        if value == "quarterly":
            month = ((today.month - 1) // 3) * 3 + 1
            return today.replace(month=month, day=1), today
        if value == "custom":
            start = optional_day(payload.get("period_start"), "period_start")
            end = optional_day(payload.get("period_end"), "period_end")
            if start is None or end is None:
                raise PortfolioValidationError("A custom report needs period_start and period_end.")
            if end < start:
                raise PortfolioValidationError("period_end cannot be before period_start.")
            return start, end
        raise PortfolioValidationError(f"period must be one of: {', '.join(REPORT_PERIODS)}")

    def generate_report(
        self,
        portfolio_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who, "analyst")
        self._assert_no_secrets(payload)
        kind = str(payload.get("period") or "monthly").lower()
        start, end = self._report_period(kind, payload)
        visible = self._visible_venture_ids(portfolio, who)
        analytics = self.portfolio_analytics(portfolio_id, actor=who, start=start, end=end)
        ranking = self.ranking(portfolio_id, actor=who)
        risks = self.assess_risks(portfolio_id, actor=who, start=start, end=end)
        allocations = self.propose_allocations(portfolio_id, actor=who, start=start, end=end)
        recommendations = self.generate_recommendations(
            portfolio_id, actor=who, start=start, end=end
        )
        alerts = self.alert_summary(portfolio, visible=visible)
        decisions = [
            dict(row)
            for row in portfolio.get("decisions") or []
            if isinstance(row, Mapping)
            and row.get("status") == "proposed"
            and self._record_visible(row, visible)
        ]
        lifecycle: dict[str, int] = {}
        for row in self._visible_refs(portfolio, who):
            status = str(row.get("lifecycle_status") or "UNKNOWN")
            lifecycle[status] = lifecycle.get(status, 0) + 1
        report = {
            "id": new_id("preport"),
            "portfolio_id": portfolio_id,
            "title": text(payload.get("title"), "title", 200)
            or f"{portfolio.get('name')} {kind} portfolio review",
            "period_kind": kind,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "base_currency": analytics["base_currency"],
            "generated_at": self._clock().isoformat(),
            "generated_by": who,
            "kind": "portfolio_report",
            "visible_venture_ids": sorted(visible),
            "source_timestamps": analytics["data_completeness"]["source_timestamps"],
            "snapshot_hash": "",
            "sections": [
                {
                    "number": 1,
                    "key": "overview",
                    "title": "Portfolio overview",
                    "kind": "actual",
                    "content": (
                        f"{analytics['venture_count']} visible venture(s) out of "
                        f"{len(portfolio.get('ventures') or [])}; {analytics['hidden_venture_count']} hidden."
                    ),
                },
                {
                    "number": 2,
                    "key": "lifecycle",
                    "title": "Venture lifecycle distribution",
                    "kind": "observation",
                    "content": ", ".join(
                        f"{key}: {value}" for key, value in sorted(lifecycle.items())
                    )
                    or "No ventures.",
                },
                {
                    "number": 3,
                    "key": "revenue_expense",
                    "title": "Revenue and expense summary",
                    "kind": "actual",
                    "content": (
                        f"Converted revenue {analytics['totals']['revenue'].get('value')}; "
                        f"approved actual expenses {analytics['totals']['expenses'].get('value')}. "
                        f"Complete: {analytics['totals']['complete']}."
                    ),
                },
                {
                    "number": 4,
                    "key": "cash_runway",
                    "title": "Cash flow and runway",
                    "kind": "actual",
                    "content": (
                        f"Converted closing cash {analytics['totals']['cash'].get('value')}; "
                        f"net cash change {analytics['totals']['net_cash_flow'].get('value')}."
                    ),
                },
                {
                    "number": 5,
                    "key": "profitability",
                    "title": "Profitability comparison",
                    "kind": "actual",
                    "content": (
                        f"Converted net profit {analytics['totals']['net_profit'].get('value')}; "
                        "individual venture margins are in the comparison section."
                    ),
                },
                {
                    "number": 6,
                    "key": "customers",
                    "title": "Customer acquisition and retention",
                    "kind": "actual",
                    "content": (
                        "Customer counts, conversion and churn come only from Phase 37 records; "
                        "unavailable metrics remain unavailable."
                    ),
                },
                {
                    "number": 7,
                    "key": "product",
                    "title": "Product development status",
                    "kind": "actual",
                    "content": "Product state and readiness come from Phase 36; no product is treated as launched without approval.",
                },
                {
                    "number": 8,
                    "key": "operations",
                    "title": "Operational performance",
                    "kind": "actual",
                    "content": "Task, blocked and overdue evidence comes from Phase 38 operations.",
                },
                {
                    "number": 9,
                    "key": "risk",
                    "title": "Risk assessment",
                    "kind": "assessment",
                    "content": (
                        f"{len([row for row in risks['risks'] if row.get('status') != 'resolved'])} open risk(s); "
                        f"{len(risks['correlated_risks'])} correlated exposure(s)."
                    ),
                },
                {
                    "number": 10,
                    "key": "allocation",
                    "title": "Resource allocation",
                    "kind": "recommendation",
                    "content": (
                        f"{len(allocations['items'])} allocation recommendation(s); none changes a budget."
                    ),
                },
                {
                    "number": 11,
                    "key": "strategy",
                    "title": "Strategic recommendations",
                    "kind": "recommendation",
                    "content": (
                        f"{len(recommendations['recommendations'])} advisory recommendation(s); "
                        "human approval is required."
                    ),
                },
                {
                    "number": 12,
                    "key": "decisions",
                    "title": "Outstanding decisions",
                    "kind": "observation",
                    "content": f"{len(decisions)} decision(s) awaiting an approver.",
                },
            ],
            "analytics": analytics,
            "ranking": ranking,
            "risks": risks,
            "allocations": allocations,
            "recommendations": recommendations,
            "alerts": alerts,
            "outstanding_decisions": decisions,
            "limitations": analytics["limitations"]
            + [
                "This report is a frozen snapshot. Later source changes do not rewrite it.",
                "No funds, budgets, lifecycle states or external systems were changed by this report.",
            ],
        }
        body = json.dumps(
            {
                "sections": report["sections"],
                "analytics": analytics,
                "risks": risks,
                "allocation": allocations,
                "ranking": ranking,
            },
            sort_keys=True,
            default=str,
        )
        report["snapshot_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        portfolio.setdefault("reports", []).append(report)
        portfolio["reports"] = portfolio["reports"][-100:]
        self._event(portfolio, "report_generated", who, {"report_id": report["id"], "period": kind})
        self._persist()
        self._ingest_knowledge(
            f"{report['title']} ({report['period']['start']} to {report['period']['end']})",
            " ".join(str(section.get("content")) for section in report["sections"]),
            "portfolio",
        )
        return report

    def _report_accessible(
        self,
        portfolio: Mapping[str, Any],
        report: Mapping[str, Any],
        visible: set[str],
    ) -> bool:
        stored = report.get("visible_venture_ids")
        if stored is None:
            # Older reports have no visibility snapshot; be conservative.
            stored = [
                ref.get("id") for ref in portfolio.get("ventures") or [] if isinstance(ref, Mapping)
            ]
        return {str(item) for item in stored if item not in (None, "")} <= visible

    def list_reports(self, portfolio_id: str, *, actor: str | None = None) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        return {
            "portfolio_id": portfolio_id,
            "reports": [
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "title",
                        "period_kind",
                        "period",
                        "base_currency",
                        "generated_at",
                        "generated_by",
                        "snapshot_hash",
                    )
                }
                for row in portfolio.get("reports") or []
                if isinstance(row, Mapping) and self._report_accessible(portfolio, row, visible)
            ],
            "note": "Report bodies are immutable snapshots; list rows are summaries.",
        }

    def get_report(
        self,
        portfolio_id: str,
        report_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        for row in portfolio.get("reports") or []:
            if isinstance(row, Mapping) and row.get("id") == report_id:
                if not self._report_accessible(portfolio, row, visible):
                    raise PortfolioAuthorizationError(
                        "The report contains a venture restricted from this actor."
                    )
                return dict(row)
        raise PortfolioRecordNotFound("report", report_id)

    # -- dashboard -----------------------------------------------------------
    def dashboard(
        self,
        portfolio_id: str,
        *,
        actor: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        portfolio = self._get(portfolio_id)
        who = self._actor(actor)
        role = self._authorize(portfolio, who)
        visible = self._visible_venture_ids(portfolio, who)
        analytics = self.portfolio_analytics(portfolio_id, actor=who, start=start, end=end)
        begin, finish, _prior_start, _prior_end = self._intervals(start, end)
        snapshots = self._snapshots_for(portfolio, who, start=begin, end=finish)
        risks, correlated = self._compute_risks(portfolio, snapshots, visible=visible)
        ranking = self.ranking(portfolio_id, actor=who)
        allocation_raw = (
            (portfolio.get("allocation_batches") or [None])[-1]
            if portfolio.get("allocation_batches")
            else None
        )
        allocation = (
            self._filter_batch(allocation_raw, visible, "items")
            if isinstance(allocation_raw, Mapping)
            else None
        )
        if (
            isinstance(allocation, Mapping)
            and allocation.get("filtered_for_visibility")
            and not allocation.get("items")
        ):
            allocation = None
        recommendation_raw = (
            (portfolio.get("recommendations") or [None])[-1]
            if portfolio.get("recommendations")
            else None
        )
        recommendation = (
            self._filter_batch(recommendation_raw, visible, "recommendations", keys=("venture_id",))
            if isinstance(recommendation_raw, Mapping)
            else None
        )
        if (
            isinstance(recommendation, Mapping)
            and recommendation.get("filtered_for_visibility")
            and not recommendation.get("recommendations")
        ):
            recommendation = None
        dependencies = [
            dict(row)
            for row in portfolio.get("dependencies") or []
            if isinstance(row, Mapping)
            and self._record_visible(row, visible, ("from_venture_id", "to_venture_id"))
        ]
        lifecycle: dict[str, int] = {}
        for ref in self._visible_refs(portfolio, who):
            status = str(ref.get("lifecycle_status") or "UNKNOWN")
            lifecycle[status] = lifecycle.get(status, 0) + 1
        active = sum(
            value
            for key, value in lifecycle.items()
            if key in {"CUSTOMER_ACQUISITION", "OPERATING", "UNDER_REVIEW"}
        )
        return {
            "portfolio": self._portfolio_summary(portfolio),
            "role": role,
            "period": {"start": begin.isoformat(), "end": finish.isoformat()},
            "base_currency": analytics["base_currency"],
            "overview": {
                "total_ventures": len(portfolio.get("ventures") or []),
                "visible_ventures": len(self._visible_refs(portfolio, who)),
                "active_ventures": active,
                "under_review": lifecycle.get("UNDER_REVIEW", 0),
                "paused_ventures": lifecycle.get("PAUSED", 0),
                "lifecycle_distribution": lifecycle,
                "revenue": analytics["totals"]["revenue"],
                "expenses": analytics["totals"]["expenses"],
                "profitability": analytics["totals"]["profitability"],
                "cash": analytics["totals"]["cash"],
                "consolidation_complete": analytics["totals"]["complete"],
                "excluded_from_consolidation": analytics["excluded_from_consolidation"],
            },
            "lifecycle_distribution": lifecycle,
            "venture_comparison": analytics["ventures"],
            "capital": {
                "approved_budgets": [
                    {
                        "venture_id": row.get("venture_id"),
                        "currency": row.get("currency"),
                        "approved_budget": row.get("approved_budget"),
                        "actual": row.get("budget_actual"),
                    }
                    for row in analytics["ventures"]
                ],
                "resources": self._resource_status(portfolio, visible=visible),
                "proposed_reallocations": [
                    row
                    for row in ((allocation or {}).get("items") or [])
                    if isinstance(row, Mapping) and row.get("action") == "reallocate"
                ],
                "latest_batch": allocation,
                "note": "No budget is changed by the dashboard.",
            },
            "risks": {
                "items": risks,
                "correlated": correlated,
                "high_or_critical": [
                    row
                    for row in risks
                    if row.get("status") != "resolved"
                    and row.get("severity") in {"high", "critical"}
                ],
                "by_severity": {
                    severity: len(
                        [
                            row
                            for row in risks
                            if row.get("status") != "resolved" and row.get("severity") == severity
                        ]
                    )
                    for severity in SEVERITIES
                },
            },
            "alerts": self.alert_summary(portfolio, visible=visible),
            "strategic": {
                "ranking": ranking,
                "scenarios": [
                    dict(row)
                    for row in portfolio.get("scenarios") or []
                    if isinstance(row, Mapping) and self._scenario_visible(row, visible)
                ][-10:],
                "recommendations": (recommendation or {}).get("recommendations", [])
                if isinstance(recommendation, Mapping)
                else [],
                "pending_decisions": [
                    dict(row)
                    for row in portfolio.get("decisions") or []
                    if isinstance(row, Mapping)
                    and row.get("status") == "proposed"
                    and self._record_visible(row, visible)
                ],
                "reports": [
                    {
                        "id": row.get("id"),
                        "title": row.get("title"),
                        "period": row.get("period"),
                        "generated_at": row.get("generated_at"),
                    }
                    for row in portfolio.get("reports") or []
                    if isinstance(row, Mapping) and self._report_accessible(portfolio, row, visible)
                ][-10:],
            },
            "coordination": {
                "dependency_cycles": self._dependency_cycles(dependencies),
                "dependencies": dependencies,
                "responsibilities": {
                    key: value.get("assigned_agent")
                    for key, value in self.coordination(portfolio_id, actor=who)
                    .get("responsibilities", {})
                    .items()
                },
            },
            "data_completeness": analytics["data_completeness"],
            "inter_venture": analytics["inter_venture"],
            "limitations": analytics["limitations"],
            "drill_down": {
                "venture_template": "/finance/ventures/{venture_id}",
                "note": "Venture drill-down opens the existing Phase 38 financial workspace.",
            },
        }


__all__ = ["PortfolioIntelligenceMixin"]
