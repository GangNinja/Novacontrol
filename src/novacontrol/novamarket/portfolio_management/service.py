"""PI-21 advanced autonomous portfolio-management coordinator.

The coordinator reads the authoritative NM-07 book and composes existing
forecast, regime, research, multi-agent, optimization and RL evidence into
advisory position, entry, exit, capital, exposure and rebalancing decisions.  It
has no ledger, position, cash, risk-approval or order method.  The only action
that can leave this package is a request to the existing PI-12/AT-04 executor
for a persisted, NM-08-approved AT-03 proposal, identified by proposal id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from novacontrol.novamarket.portfolio_management.actions import (
    PortfolioManagementActionsMixin,
)
from novacontrol.novamarket.portfolio_management.analysis import (
    PortfolioManagementAnalysisMixin,
)
from novacontrol.novamarket.portfolio_management.evidence import (
    EvidenceBook,
    ManagementSources,
    require_sources,
)
from novacontrol.novamarket.portfolio_management.models import (
    AGENT_AUTHORITY,
    CONFIG_VERSION,
    EXECUTION_AUTHORITY,
    HORIZON_FAMILIES,
    HORIZONS,
    MAX_AGENT_REFERENCES,
    MAX_CYCLES_PER_QUERY,
    MAX_DECISIONS,
    MAX_OPPORTUNITIES,
    ONE_CYCLE_AT_A_TIME,
    PM_CONTRACT_VERSION,
    PM_PHASE,
    RISK_AUTHORITY,
    CashRecommendation,
    CycleStatus,
    DataStatus,
    DeploymentRecommendation,
    EntryRecommendation,
    ExitRecommendation,
    ManagementAction,
    ManagementConfig,
    PortfolioManagementConflict,
    PortfolioManagementStateError,
    PortfolioManagementValidationError,
    PositionAssessment,
    RebalanceAction,
    authority_contract,
    canonical_horizon,
    decimal,
    digest,
    number,
    review_horizon,
    stable_id,
    utcnow,
)
from novacontrol.novamarket.portfolio_management.store import (
    PortfolioManagementStore,
)

_LOGGER = logging.getLogger("novacontrol.novamarket.portfolio_management")

ACTIVE_ORDER_STATUSES = frozenset(
    {
        "created",
        "pending_validation",
        "accepted",
        "open",
        "partially_filled",
        "cancel_pending",
    }
)
TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "rejected", "expired", "failed"})
POSITIVE_STANCES = frozenset({"bullish", "positive", "buy"})
NEGATIVE_STANCES = frozenset({"bearish", "negative", "sell"})
VALID_FORECAST_STATES = frozenset({"valid", "confirmed", "active"})
WAIT_FORECAST_STATES = frozenset({"unconfirmed", "pending", "awaiting_confirmation", "initial"})

#: Default cadence for scheduled reviews.  This is not a timer: the single
#: NovaMarket autonomy runtime tick is the only caller of :meth:`maybe_run`, so
#: the value only decides how often a tick that is already due performs work.
SCHEDULED_REVIEW_INTERVAL_SECONDS = 3600
SCHEDULED_REVIEW_MIN_INTERVAL_SECONDS = 30
SCHEDULED_LAST_ATTEMPT_KEY = "scheduled_last_attempt_at"
SCHEDULED_LAST_CYCLE_KEY = "scheduled_last_cycle_id"
SCHEDULED_CURSOR_KEY = "scheduled_cursor_portfolio_id"


def _text(value: object, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: object) -> list[dict[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("items", "rows", "results", "orders", "reservations", "fills", "proposals"):
            body = value.get(key)
            if isinstance(body, Sequence) and not isinstance(body, (str, bytes, bytearray)):
                return [dict(row) for row in body if isinstance(row, Mapping)]
    return []


def _num(value: object) -> float | None:
    return number(value)


def _dec(value: object, default: str = "0") -> Decimal:
    try:
        return decimal(value)
    except (TypeError, ValueError):
        try:
            return Decimal(default)
        except Exception:
            return Decimal("0")


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _pct(value: float | None) -> float | None:
    return None if value is None else _round(value * 100.0, 4)


def _canonical_instrument(value: object) -> str:
    text = _text(value, 160).upper()
    if not text:
        return ""
    if ":" not in text:
        return text
    exchange, symbol = text.split(":", 1)
    return f"{exchange}:{symbol}"


def _exchange(value: object) -> str:
    text = _text(value, 32).upper()
    if ":" in text:
        return text.split(":", 1)[0]
    return text or "XNSE"


def _mtime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        result = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


class PortfolioManagementService(PortfolioManagementAnalysisMixin, PortfolioManagementActionsMixin):
    """The one PI-21 coordinator.  Every financial authority stays external."""

    def __init__(
        self,
        sources: ManagementSources,
        *,
        store_path: str | Any = ":memory:",
        clock: Any | None = None,
        stale_after_seconds: int = 900,
        scheduled_interval_seconds: int = SCHEDULED_REVIEW_INTERVAL_SECONDS,
    ) -> None:
        self._sources = require_sources(sources)
        self._clock = clock or utcnow
        self._store = PortfolioManagementStore(store_path)
        self._stale_after_seconds = max(60, int(stale_after_seconds))
        self._scheduled_interval_seconds = max(
            SCHEDULED_REVIEW_MIN_INTERVAL_SECONDS, int(scheduled_interval_seconds)
        )
        self._scheduled_error = ""
        self._last_error = ""
        self._closed = False

    # -- lifecycle/views -----------------------------------------------------
    @property
    def store(self) -> PortfolioManagementStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._store.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _runtime(self) -> dict[str, Any]:
        try:
            body = self._sources.runtime_status()
            return _dict(body)
        except Exception as exc:  # noqa: BLE001
            return {"mode": "disabled", "available": False, "error": f"{type(exc).__name__}: {exc}"}

    def _mode(self, runtime: Mapping[str, Any] | None = None) -> str:
        body = dict(runtime or self._runtime())
        return _text(body.get("mode") or "disabled", 32).lower() or "disabled"

    @staticmethod
    def _kill_switch(runtime: Mapping[str, Any]) -> bool:
        for key in ("kill_switch", "kill_switch_active", "halted"):
            if _bool(runtime.get(key)):
                return True
        safety = _dict(runtime.get("safety"))
        return any(_bool(safety.get(key)) for key in ("kill_switch", "kill_switch_active"))

    def status(
        self,
        *,
        operator_authorized: bool | None = None,
        operator_configured: bool | None = None,
    ) -> dict[str, Any]:
        runtime = self._runtime()
        mode = self._mode(runtime)
        latest = self._store.latest_cycle()
        active = self._store.active_cycle()
        return {
            "ok": True,
            "phase": PM_PHASE,
            "implemented": True,
            "contract_version": PM_CONTRACT_VERSION,
            "initialised": True,
            "mode": mode,
            "runtime_state": _text(runtime.get("runtime_state"), 40) or "unknown",
            "runtime_available": runtime.get("available") is not False,
            "lease_valid": bool(_dict(runtime.get("lease")).get("valid")),
            "kill_switch": self._kill_switch(runtime),
            "operator_authorized": operator_authorized,
            "operator_configured": operator_configured,
            "operator_authorization_required": True,
            "active_cycle_id": (active or {}).get("cycle_id", ""),
            "latest_cycle_id": (latest or {}).get("cycle_id", ""),
            "latest_cycle_status": (latest or {}).get("status", ""),
            "scheduler": self.scheduled_state(runtime=runtime),
            "config_version": CONFIG_VERSION,
            "store": self._store.status(),
            "authorities": authority_contract(),
            "safety": {
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "direct_financial_mutation": False,
                "direct_order_submission": False,
                "risk_override": False,
                "policy_activation": False,
                "one_cycle_at_a_time": ONE_CYCLE_AT_A_TIME,
                "recovery_resumes_autonomy": False,
            },
            "last_error": self._last_error,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PM_PHASE,
            "name": "Advanced Autonomous Portfolio Management",
            "contract_version": PM_CONTRACT_VERSION,
            "read_only_financial": True,
            "advisory_decisions": True,
            "horizons": list(HORIZONS),
            "horizon_families": dict(HORIZON_FAMILIES),
            "cycle_statuses": [item.value for item in CycleStatus],
            "position_assessments": [item.value for item in PositionAssessment],
            "actions": [item.value for item in ManagementAction],
            "entry_recommendations": [item.value for item in EntryRecommendation],
            "exit_recommendations": [item.value for item in ExitRecommendation],
            "deployment_recommendations": [item.value for item in DeploymentRecommendation],
            "cash_recommendations": [item.value for item in CashRecommendation],
            "rebalance_actions": [item.value for item in RebalanceAction],
            "authorities": authority_contract(),
            "workflow": [
                "authoritative NM-07 portfolio snapshot",
                "position and portfolio compatibility evidence",
                "bounded opportunity discovery from stored forecasts",
                "capital/exposure/cash analysis",
                "PI-18 optimization comparison and rebalancing proposal",
                "explainable advisory decision records",
                "optional handoff of matching risk-approved AT-03 proposals to PI-12/AT-04",
                "NM-08 risk assessment and NM-10 paper execution remain authoritative",
                "reconciliation and no automatic resume",
            ],
            "separation": {
                "portfolio_state": "NM-07 and existing accounting services",
                "risk": RISK_AUTHORITY,
                "paper_execution": EXECUTION_AUTHORITY,
                "agents": AGENT_AUTHORITY,
                "research": "PI-19 advisory evidence",
                "optimization": "PI-18 advisory allocation",
                "rl": "advisory only; never activated by PI-21",
                "recommendation_quality": "not inferred from P&L",
            },
            "scheduler": {
                "hosted_by": "novamarket_runtime",
                "second_scheduler": False,
                "default_interval_seconds": SCHEDULED_REVIEW_INTERVAL_SECONDS,
                "triggers": ["manual", "scheduled"],
                "gates": [
                    "autonomy mode monitoring or autonomous",
                    "valid runtime lease owned by this process",
                    "autonomy kill switch clear",
                ],
            },
            "limits": {
                "max_cycles_per_query": MAX_CYCLES_PER_QUERY,
                "max_opportunities": MAX_OPPORTUNITIES,
                "max_decisions": MAX_DECISIONS,
                "max_agent_references": MAX_AGENT_REFERENCES,
            },
        }

    def navigation(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PM_PHASE,
            "views": [
                {"id": "overview", "label": "Command overview"},
                {"id": "positions", "label": "Position intelligence"},
                {"id": "opportunities", "label": "Opportunity intelligence"},
                {"id": "capital", "label": "Capital and cash"},
                {"id": "rebalancing", "label": "Rebalancing"},
                {"id": "decisions", "label": "Decision history"},
                {"id": "autonomy", "label": "Autonomous management"},
            ],
            "portfolio_required_for_cycle": True,
            "supports_manual_cycle": True,
            "supports_scheduled_cycle": True,
            "scheduled_by": "the single NovaMarket autonomy runtime tick",
            "controls": ["review", "handoff", "reconcile", "recover"],
            "direct_financial_controls": [],
        }

    # -- configuration ------------------------------------------------------
    def config(self) -> dict[str, Any]:
        version, config = self._store.config()
        return {
            "ok": True,
            "phase": PM_PHASE,
            "version": version,
            "config": config.to_dict(),
            "history": self._store.config_history(limit=20),
        }

    def update_config(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        _, current = self._store.config()
        merged = current.to_dict()
        merged.update(dict(payload or {}))
        config = ManagementConfig.from_mapping(merged)
        staged = self._store.save_config(config)
        return {
            "ok": True,
            "phase": PM_PHASE,
            "actor": _text(actor, 160),
            "version": staged["version"],
            "config": config.to_dict(),
            "financial_mutation": False,
        }

    # -- snapshot -----------------------------------------------------------
    def _portfolio_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        body = self._sources.portfolio_list(max(1, min(int(limit), 100)))
        return _items(_dict(body).get("portfolios")) or _items(body)

    def _detail(self, portfolio_id: str) -> dict[str, Any]:
        body = _dict(self._sources.portfolio_detail(portfolio_id))
        if not body or not _dict(body.get("portfolio")):
            raise PortfolioManagementStateError(
                f"the authoritative portfolio {portfolio_id!r} could not be read"
            )
        return body

    def _snapshot(self, portfolio_id: str, at: datetime) -> dict[str, Any]:
        detail = self._detail(portfolio_id)
        record = _dict(detail.get("portfolio"))
        valuation = _dict(detail.get("valuation"))
        holdings = _items(detail.get("holdings"))
        cash = _num(detail.get("cash"))
        if cash is None:
            cash = _num(valuation.get("cash"))
        try:
            reserved = _num(self._sources.reserved_cash(portfolio_id))
        except Exception:
            reserved = None
        pending = [
            row
            for row in _items(self._sources.pending_orders(portfolio_id))
            if _text(row.get("status"), 40).lower() in ACTIVE_ORDER_STATUSES
        ]
        pending_notional = 0.0
        for order in pending:
            qty = _num(order.get("remaining_quantity"))
            price = _num(order.get("reference_price") or order.get("limit_price"))
            if _text(order.get("side"), 20).lower() == "buy" and qty and price:
                pending_notional += qty * price
        committed = max(reserved or 0.0, pending_notional)
        available = None
        if cash is not None and reserved is not None:
            available = max(0.0, cash - committed)
        try:
            reservations = _items(self._sources.reservations(portfolio_id))
        except Exception:
            reservations = []
        try:
            fills = _items(self._sources.fills(portfolio_id))
        except Exception:
            fills = []
        total_value = _num(valuation.get("total_value"))
        total = total_value or 0.0
        valuation_missing = total_value is None
        positions: list[dict[str, Any]] = []
        for row in holdings:
            quantity = _num(row.get("quantity")) or 0.0
            average = _num(row.get("average_cost")) or 0.0
            price = _num(row.get("last_price"))
            current = _num(row.get("market_value"))
            if current is None and price is not None:
                current = quantity * price
            if current is None:
                current = _num(row.get("cost_basis")) or quantity * average
            pnl = _num(row.get("unrealized_pnl"))
            if pnl is None and current is not None:
                pnl = current - quantity * average
            weight = _num(row.get("weight"))
            if weight is None and total > 0 and current is not None:
                weight = current / total
            opened = _mtime(row.get("opened_at"))
            priced_at = _mtime(row.get("priced_at"))
            stale = (
                bool(row.get("stale"))
                or priced_at is None
                or (
                    priced_at is not None
                    and (at - priced_at).total_seconds() > self._stale_after_seconds
                )
            )
            positions.append(
                {
                    **row,
                    "instrument_id": _canonical_instrument(row.get("instrument_id")),
                    "exchange": _exchange(row.get("exchange") or row.get("instrument_id")),
                    "quantity": quantity,
                    "average_cost": average,
                    "current_price": price,
                    "current_value": current,
                    "unrealized_pnl": pnl,
                    "return_pct": (
                        _pct(pnl / (quantity * average))
                        if pnl is not None and quantity * average > 0
                        else None
                    ),
                    "weight": weight,
                    "holding_hours": (
                        max(0.0, (at - opened).total_seconds() / 3600.0) if opened else None
                    ),
                    "stale": stale,
                    "missing_price": price is None,
                    "priced_at": row.get("priced_at"),
                    "contribution": (pnl / total) if pnl is not None and total > 0 else None,
                    "inr_value": current,
                    "currency": "INR",
                    "nova_coin": True,
                }
            )
        reservations_cash = sum(
            (_num(row.get("remaining")) or 0.0)
            for row in reservations
            if _text(row.get("kind"), 20).lower() == "cash"
            and _text(row.get("status"), 20).lower() == "active"
        )
        anomalies = self._snapshot_anomalies(
            positions=positions,
            pending=pending,
            reservations=reservations,
            fills=fills,
            at=at,
        )
        if valuation_missing:
            anomalies.append(
                {
                    "code": "valuation_unavailable",
                    "detail": (
                        "The authoritative portfolio valuation has no total value; "
                        "it is reported as unavailable rather than zero."
                    ),
                }
            )
        if cash is not None and reserved is not None and reserved > cash + 0.000001:
            anomalies.append(
                {
                    "code": "reserved_capital_exceeds_cash",
                    "detail": (
                        "NM-10 cash reservations exceed the authoritative available "
                        "cash balance; no new deployment is safe."
                    ),
                }
            )
        if cash is not None and pending_notional > cash + 0.000001:
            anomalies.append(
                {
                    "code": "pending_commitments_exceed_cash",
                    "detail": (
                        "Pending buy notional exceeds the authoritative cash balance; "
                        "capital planning must wait for reconciliation."
                    ),
                }
            )
        previous = self._store.latest_snapshot(portfolio_id)
        if previous:
            old: dict[str, float | None] = {
                _text(row.get("instrument_id")): _num(row.get("quantity"))
                for row in _items(previous.get("positions"))
            }
            for row in positions:
                instrument = _text(row.get("instrument_id"))
                current_quantity: float | None = _num(row.get("quantity"))
                previous_quantity = old.get(instrument)
                if (
                    previous_quantity is not None
                    and current_quantity is not None
                    and abs(current_quantity - previous_quantity) > 0.00005
                ):
                    recent_fill = any(
                        _text(fill.get("instrument_id")).upper() == instrument
                        or _text(fill.get("order_id"))
                        in {_text(order.get("order_id")) for order in pending}
                        for fill in fills
                    )
                    if not recent_fill:
                        anomalies.append(
                            {
                                "code": "position_changed_without_recent_fill",
                                "instrument_id": instrument,
                                "previous_quantity": previous_quantity,
                                "current_quantity": current_quantity,
                                "detail": (
                                    "The authoritative quantity changed since the prior "
                                    "snapshot and no matching fill was available in the "
                                    "bounded execution record."
                                ),
                            }
                        )
        body = {
            "snapshot_id": stable_id(
                "pms_",
                portfolio_id,
                at.isoformat(),
                record.get("updated_at"),
                total,
                len(positions),
            ),
            "portfolio_id": portfolio_id,
            "at": at.isoformat(),
            "authoritative": True,
            "portfolio": record,
            "valuation": valuation,
            "valuation_available": not valuation_missing,
            "missing_valuation": valuation_missing,
            "cash": {
                "total_nc": cash,
                "reserved_nc": reserved,
                "available_nc": available,
                "pending_commitments_nc": committed,
                "pending_buy_notional_nc": pending_notional,
                "reserved_source": "NM-10 active cash reservations",
                "cash_weight": _num(valuation.get("cash_weight")),
                "cash_ratio_pct": _num(valuation.get("cash_weight_pct")),
                "inr_equivalent": cash,
                "currency": "INR",
                "nova_coin": True,
                "missing_as_zero": False,
            },
            "pending_orders": pending,
            "pending_buy_notional": pending_notional,
            "committed_capital_nc": committed,
            "reservations": reservations,
            "reservations_cash_total": reservations_cash,
            "fills_sample": fills[:50],
            "positions": positions,
            "counts": {
                "positions": len(positions),
                "pending_orders": len(pending),
                "stale_positions": sum(1 for row in positions if row["stale"]),
                "missing_prices": sum(1 for row in positions if row["missing_price"]),
            },
            "anomalies": anomalies,
            "data_versions": {
                "portfolio_updated_at": record.get("updated_at"),
                "market_prices": [
                    {
                        "instrument_id": row["instrument_id"],
                        "priced_at": row.get("priced_at"),
                        "price": row.get("current_price"),
                    }
                    for row in positions
                ],
                "execution": {
                    "pending_order_ids": [_text(row.get("order_id")) for row in pending],
                    "reservation_ids": [_text(row.get("reservation_id")) for row in reservations],
                },
            },
            "currency": "INR",
            "nova_coin": True,
            "nc_inr_rate": "1.0",
        }
        body["snapshot_version"] = digest(
            {
                "portfolio_id": portfolio_id,
                "record_updated_at": record.get("updated_at"),
                "valuation": valuation,
                "positions": [
                    (
                        row["instrument_id"],
                        row["quantity"],
                        row.get("current_price"),
                        row.get("priced_at"),
                    )
                    for row in positions
                ],
                "pending": [
                    (row.get("order_id"), row.get("status"), row.get("updated_at"))
                    for row in pending
                ],
                "reservations": [
                    (row.get("reservation_id"), row.get("status"), row.get("remaining"))
                    for row in reservations
                ],
            }
        )
        return body

    def _snapshot_anomalies(
        self,
        *,
        positions: Iterable[Mapping[str, Any]],
        pending: Iterable[Mapping[str, Any]],
        reservations: Iterable[Mapping[str, Any]],
        fills: Iterable[Mapping[str, Any]],
        at: datetime,
    ) -> list[dict[str, Any]]:
        anomalies: list[dict[str, Any]] = []
        for row in positions:
            if row.get("missing_price"):
                anomalies.append(
                    {
                        "code": "missing_price",
                        "instrument_id": row.get("instrument_id"),
                        "detail": (
                            "No authoritative market price is available; "
                            "the position remains at cost basis."
                        ),
                    }
                )
            elif row.get("stale"):
                anomalies.append(
                    {
                        "code": "stale_valuation",
                        "instrument_id": row.get("instrument_id"),
                        "priced_at": row.get("priced_at"),
                        "detail": (
                            "The latest price is older than the configured "
                            "management freshness window."
                        ),
                    }
                )
        order_ids = {_text(row.get("order_id")) for row in pending}
        active_reservations = [
            row for row in reservations if _text(row.get("status"), 20).lower() == "active"
        ]
        for row in active_reservations:
            if _text(row.get("order_id")) not in order_ids:
                anomalies.append(
                    {
                        "code": "reservation_without_active_order",
                        "reservation_id": row.get("reservation_id"),
                        "order_id": row.get("order_id"),
                        "detail": (
                            "An active reservation has no matching active order "
                            "in the bounded read."
                        ),
                    }
                )
        for row in pending:
            if _text(row.get("status"), 40).lower() in ACTIVE_ORDER_STATUSES:
                expires = _mtime(row.get("expires_at"))
                if expires is not None and expires <= at:
                    anomalies.append(
                        {
                            "code": "expired_pending_order",
                            "order_id": row.get("order_id"),
                            "detail": (
                                "The order is recorded as active but its expiry is in the past; "
                                "NM-10 reconciliation is required."
                            ),
                        }
                    )
        if not fills and pending:
            anomalies.append(
                {
                    "code": "pending_without_fill_evidence",
                    "detail": (
                        "Pending commitments exist and the bounded fill read "
                        "returned no fill evidence."
                    ),
                }
            )
        return anomalies

    @staticmethod
    def _signal_tone(value: float | None) -> str:
        if value is None:
            return "unavailable"
        if value > 0.005:
            return "positive"
        if value < -0.005:
            return "negative"
        return "neutral"

    def _forecast_context(
        self, instrument: str, horizon: str, evidence: EvidenceBook
    ) -> dict[str, Any]:
        raw = evidence.forecasts(instrument, horizon)
        return self._forecast_consensus(raw, now=self._now(), config=self._config)

    @property
    def _config(self) -> ManagementConfig:
        if not hasattr(self, "_cycle_config"):
            _, self._cycle_config = self._store.config()
        return self._cycle_config

    # -- scheduled reviews hosted by the autonomy runtime --------------------
    def _scheduled_last_attempt(self) -> datetime | None:
        return _mtime(self._store.meta(SCHEDULED_LAST_ATTEMPT_KEY))

    def scheduled_state(
        self, *, runtime: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read-only scheduler state, computed from the runtime it rides on.

        There is no timer here, so ``running`` can only mean "the runtime that
        owns the lease will host a review when the cadence is due".
        """

        body = dict(runtime if runtime is not None else self._runtime())
        mode = self._mode(body)
        lease_valid = bool(_dict(body.get("lease")).get("valid"))
        kill_switch = self._kill_switch(body)
        last = self._scheduled_last_attempt()
        next_due = (
            last.timestamp() + self._scheduled_interval_seconds
            if last is not None
            else None
        )
        return {
            "hosted_by": "novamarket_runtime",
            "second_scheduler": False,
            "running": mode in {"monitoring", "autonomous"} and lease_valid and not kill_switch,
            "mode": mode,
            "lease_valid": lease_valid,
            "kill_switch": kill_switch,
            "interval_seconds": self._scheduled_interval_seconds,
            "last_attempt_at": last.isoformat() if last is not None else None,
            "next_due_at": (
                datetime.fromtimestamp(next_due, tz=UTC).isoformat()
                if next_due is not None
                else None
            ),
            "last_cycle_id": self._store.meta(SCHEDULED_LAST_CYCLE_KEY),
            "cursor_portfolio_id": self._store.meta(SCHEDULED_CURSOR_KEY),
            "last_error": self._scheduled_error,
            "financial_mutation": False,
            "orders_possible": False,
        }

    async def maybe_run(self) -> dict[str, Any]:
        """Run one scheduled review if the hosting runtime says it is due.

        The single NovaMarket autonomy runtime tick calls this; the method adds
        no thread, timer or second scheduler.  It refuses with a stable status
        instead of raising, so a refusal is reported rather than silencing the
        runtime tick.  The autonomy mode, the durable runtime lease and the
        kill switch are re-checked here as well as in :meth:`run_cycle`.
        """

        if self._closed:
            return self._scheduled_refusal(
                "SERVICE_CLOSED", "the portfolio-management service is closed"
            )
        runtime = self._runtime()
        mode = self._mode(runtime)
        if mode in {"disabled", "paused"}:
            return self._scheduled_refusal(
                "RUNTIME_DISABLED" if mode == "disabled" else "RUNTIME_PAUSED",
                f"scheduled portfolio reviews are not allowed while autonomy is {mode}",
            )
        if mode not in {"monitoring", "autonomous"}:
            return self._scheduled_refusal(
                "RUNTIME_UNKNOWN",
                f"scheduled portfolio reviews need a started runtime, not {mode}",
            )
        if not _bool(_dict(runtime.get("lease")).get("valid")):
            return self._scheduled_refusal(
                "LEASE_INVALID", "the runtime lease is not valid"
            )
        if self._kill_switch(runtime):
            return self._scheduled_refusal(
                "KILL_SWITCH", "the autonomy kill switch blocks scheduled reviews"
            )
        now = self._now()
        last = self._scheduled_last_attempt()
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < self._scheduled_interval_seconds:
                return {
                    "ok": True,
                    "status": "not_due",
                    "retry_after_seconds": round(
                        self._scheduled_interval_seconds - elapsed, 1
                    ),
                    "trigger": "scheduled",
                    "financial_mutation": False,
                    "orders_possible": False,
                }
        portfolio_id = self._scheduled_portfolio()
        if not portfolio_id:
            return self._scheduled_refusal(
                "NO_PORTFOLIO",
                "no active portfolio is available for a scheduled review",
            )
        # The review analyses the horizon the portfolio itself declares; the 5d
        # default applied only when nothing usable is declared.
        horizon = self._scheduled_horizon(portfolio_id)
        # Record the attempt before the work so a failing review is not retried
        # by every later tick inside its own cadence window.
        self._store.set_meta(SCHEDULED_LAST_ATTEMPT_KEY, now.isoformat(), at=now)
        try:
            cycle = await self.run_cycle(
                portfolio_id=portfolio_id,
                horizon=horizon,
                trigger="scheduled",
                actor="autonomy-runtime",
            )
        except PortfolioManagementConflict as exc:
            self._scheduled_error = str(exc)
            return self._scheduled_refusal("CYCLE_ACTIVE", str(exc))
        except (PortfolioManagementStateError, PortfolioManagementValidationError) as exc:
            self._scheduled_error = str(exc)
            return self._scheduled_refusal("REVIEW_REFUSED", str(exc))
        except Exception as exc:  # noqa: BLE001 - report into the runtime tick
            self._scheduled_error = f"{type(exc).__name__}: {exc}"
            return {
                "ok": False,
                "status": "failed",
                "trigger": "scheduled",
                "error": self._scheduled_error,
                "financial_mutation": False,
                "orders_possible": False,
            }
        self._store.set_meta(
            SCHEDULED_LAST_CYCLE_KEY, str(cycle.get("cycle_id") or ""), at=now
        )
        status = _text(cycle.get("status"), 32) or "unknown"
        failed = status == CycleStatus.FAILED.value
        self._scheduled_error = str(cycle.get("error") or "") if failed else ""
        return {
            "ok": not failed,
            "status": "failed" if failed else "completed",
            "cycle_status": status,
            "trigger": "scheduled",
            "portfolio_id": portfolio_id,
            "horizon": horizon,
            "cycle_id": cycle.get("cycle_id"),
            "decisions": len(cycle.get("decisions") or ()),
            "error": self._scheduled_error,
            "financial_mutation": False,
            "orders_possible": False,
        }

    def _scheduled_horizon(self, portfolio_id: str) -> str:
        """The horizon a scheduled review analyses for this portfolio.

        The authoritative detail is what :meth:`run_cycle` will snapshot, so its
        declared horizon is the source of truth.  An unreadable detail falls
        through to the documented default instead of refusing the whole tick;
        ``run_cycle`` reports the read failure itself.
        """

        declared: object = ""
        try:
            declared = _dict(self._detail(portfolio_id).get("portfolio")).get("horizon")
        except Exception:  # noqa: BLE001 - run_cycle reports an unreadable detail
            declared = ""
        return review_horizon(declared)

    def _scheduled_portfolio(self) -> str:
        """The next portfolio in the book, rotating from a durable cursor.

        Every scheduled review used to re-read the first row, so a book of six
        portfolios only ever reviewed one.  The cursor is the portfolio the last
        selection chose: the row after it is picked and the end of the book
        wraps to the first row.  A cursor whose portfolio has left the book
        starts the rotation again instead of refusing.  The cursor advances at
        selection, so a portfolio that keeps failing cannot stall coverage; the
        failure itself is reported on the review result.
        """

        try:
            rows = self._portfolio_rows(limit=100)
        except Exception as exc:  # noqa: BLE001 - reported as a refusal
            self._scheduled_error = f"{type(exc).__name__}: {exc}"
            return ""
        book: list[str] = []
        for row in rows:
            portfolio_id = _text(row.get("portfolio_id") or row.get("id"), 160)
            if portfolio_id and portfolio_id not in book:
                book.append(portfolio_id)
        if not book:
            return ""
        cursor = _text(self._store.meta(SCHEDULED_CURSOR_KEY), 160)
        index = book.index(cursor) + 1 if cursor in book else 0
        chosen = book[index % len(book)]
        self._store.set_meta(SCHEDULED_CURSOR_KEY, chosen, at=self._now())
        return chosen

    @staticmethod
    def _scheduled_refusal(code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "refused",
            "reason_code": code,
            "reason": message,
            "trigger": "scheduled",
            "financial_mutation": False,
            "orders_possible": False,
        }

    # -- main cycle ---------------------------------------------------------
    async def run_cycle(
        self,
        *,
        portfolio_id: str,
        horizon: str = "5d",
        trigger: str = "manual",
        request_id: str = "",
        actor: str = "operator",
    ) -> dict[str, Any]:
        if self._closed:
            raise PortfolioManagementStateError("the portfolio-management service is closed")
        canonical_portfolio = _text(portfolio_id, 160)
        if not canonical_portfolio:
            rows = self._portfolio_rows(limit=100)
            if not rows:
                raise PortfolioManagementValidationError(
                    "an active portfolio is required to run a management review"
                )
            canonical_portfolio = _text(rows[0].get("portfolio_id") or rows[0].get("id"), 160)
            if not canonical_portfolio:
                raise PortfolioManagementValidationError(
                    "the portfolio listing has no portfolio id"
                )
        chosen_horizon = canonical_horizon(horizon)
        selected_trigger = _text(trigger or "manual", 30).lower()
        if selected_trigger not in {"manual", "scheduled"}:
            raise PortfolioManagementValidationError("trigger must be manual or scheduled")
        request = _text(request_id, 128)
        idempotency_key = f"{canonical_portfolio}|{request or f'auto-{uuid4().hex}'}"
        existing = self._store.cycle_by_key(idempotency_key)
        if existing is not None:
            return {
                **existing,
                "ok": True,
                "replayed": True,
                "financial_mutation": False,
                "orders_possible": False,
            }
        runtime = self._runtime()
        mode = self._mode(runtime)
        if selected_trigger == "scheduled" and mode in {"disabled", "paused"}:
            raise PortfolioManagementStateError(
                f"scheduled portfolio reviews are not allowed while autonomy is {mode}"
            )
        if selected_trigger == "scheduled" and self._kill_switch(runtime):
            raise PortfolioManagementStateError("the autonomy kill switch blocks scheduled reviews")
        _, config = self._store.config()
        self._cycle_config = config
        started = self._now()
        snapshot = self._snapshot(canonical_portfolio, started)
        cycle_id = stable_id("pmc_", canonical_portfolio, idempotency_key, chosen_horizon)
        seed_payload = {
            "phase": PM_PHASE,
            "horizon": chosen_horizon,
            "horizon_family": HORIZON_FAMILIES[chosen_horizon],
            "mode": mode,
            "operator_actor": _text(actor, 160),
            "snapshot": snapshot,
            "trigger": selected_trigger,
            "runtime": {
                "mode": mode,
                "kill_switch": self._kill_switch(runtime),
                "lease_valid": bool(_dict(runtime.get("lease")).get("valid")),
            },
            "safety": {
                "advisory_only": True,
                "financial_mutation": False,
                "order_submission": False,
                "risk_authority": RISK_AUTHORITY,
                "execution_authority": EXECUTION_AUTHORITY,
            },
            "status_history": [{"status": CycleStatus.RUNNING.value, "at": started.isoformat()}],
        }
        try:
            claimed, replayed = self._store.claim_cycle(
                cycle_id=cycle_id,
                idempotency_key=idempotency_key,
                portfolio_id=canonical_portfolio,
                trigger=selected_trigger,
                config_version=config.version,
                snapshot_version=str(snapshot["snapshot_version"]),
                payload=seed_payload,
                started_at=started,
            )
        except PortfolioManagementConflict:
            raise
        if replayed:
            return {
                **claimed,
                "ok": True,
                "replayed": True,
                "financial_mutation": False,
                "orders_possible": False,
            }
        evidence = EvidenceBook(self._sources)
        try:
            result = self._compose_cycle(
                cycle=claimed,
                snapshot=snapshot,
                horizon=chosen_horizon,
                config=config,
                evidence=evidence,
                started=started,
            )
            status = str(result["status"])
            payload = {**seed_payload, **result}
            payload["status_history"] = [
                *seed_payload["status_history"],
                {"status": status, "at": self._now().isoformat()},
            ]
            payload["evidence_errors"] = dict(evidence.errors)
            payload["data_status"] = result.get("data_status", DataStatus.AVAILABLE.value)
            self._store.record_cycle_data(
                cycle_id=cycle_id,
                snapshot=snapshot,
                positions=result["positions"],
                opportunities=result["opportunities"],
                decisions=result["decisions"],
                capital=result["capital"],
                exposure=result["exposure"],
                cash=result["cash"],
                rebalance=result["rebalance"],
                created_at=self._now(),
            )
            completed = self._store.finish_cycle(
                cycle_id,
                status=status,
                payload=payload,
                completed_at=self._now(),
            )
            self._store.append_event(
                cycle_id,
                "cycle.completed",
                f"Management cycle completed with status {status}.",
                payload={"status": status, "decisions": len(result["decisions"])},
                at=self._now(),
            )
            self._last_error = ""
            return {
                **completed,
                "ok": True,
                "replayed": False,
                "financial_mutation": False,
                "orders_possible": False,
                "advisory_only": True,
                **result,
            }
        except Exception as exc:  # noqa: BLE001 - preserve a failed cycle explicitly
            self._last_error = f"{type(exc).__name__}: {exc}"
            failure = {
                **seed_payload,
                "status": CycleStatus.FAILED.value,
                "error": self._last_error,
                "completed_at": self._now().isoformat(),
                "financial_mutation": False,
                "orders_possible": False,
            }
            try:
                self._store.finish_cycle(
                    cycle_id,
                    status=CycleStatus.FAILED.value,
                    payload=failure,
                    completed_at=self._now(),
                )
                self._store.append_event(
                    cycle_id,
                    "cycle.failed",
                    self._last_error,
                    at=self._now(),
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("portfolio-management failed cycle could not be recorded")
            raise
