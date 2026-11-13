"""AT-05 protective-exit supervision over the existing NovaMarket engines.

The supervisor is deliberately narrow.  The accepted baseline defines a
protective-exit configuration in the horizon profiles (``stop_loss_fraction``)
and nothing else: there is no persisted target price and no trailing-stop state
in the accepted strategy/portfolio architecture.  AT-05 therefore implements
stop-loss supervision and reports the other two conditions as unsupported
rather than inventing semantics.

NM-08 remains the only risk authority and NM-10 remains the only executor.  This
module persists exit decisions and state in the existing NM-10 store, composes
the AT-01 runtime lease, and reads the authoritative portfolio/book records.  It
has no cash, position, fill or ledger write.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.autonomy_exits.config import build_exit_configuration
from novacontrol.novamarket.autonomy_exits.models import (
    AT05_CONTRACT_VERSION,
    BROKER_CONNECTED,
    LIVE_TRADING,
    OPEN_DECISION_STATUSES,
    PAPER_ONLY,
    SUPPORTED_EXIT_CONDITIONS,
    SUPPORTED_EXIT_DIRECTIONS,
    TERMINAL_DECISION_STATUSES,
    UNSUPPORTED_EXIT_CONDITIONS,
    AutonomyExitError,
    ExitStatus,
)
from novacontrol.novamarket.execution.models import (
    OrderStatus,
    execution_id,
    money,
    quantity,
)
from novacontrol.novamarket.horizons import profile_for
from novacontrol.novamarket.portfolio.models import Side
from novacontrol.novamarket.risk.models import RiskAction

_LOGGER = logging.getLogger("novacontrol.novamarket.autonomy_exits")

_NON_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderStatus.CREATED.value,
        OrderStatus.PENDING_VALIDATION.value,
        OrderStatus.ACCEPTED.value,
        OrderStatus.OPEN.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.CANCEL_PENDING.value,
    }
)
_ACTIVE_EXIT_STATUSES = frozenset(OPEN_DECISION_STATUSES)
_HORIZON_MAP = {
    "short_term": "day_trading",
    "medium_term": "swing_trading",
    "long_term": "long_term",
    "day_trading": "day_trading",
    "swing_trading": "swing_trading",
}
_MAX_POSITIONS_PER_CYCLE = 200


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _time(value: object) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - malformed stored state fails the position closed
        return Decimal(default)


class AutonomyExitSupervisor:
    """One durable stop-loss supervisor hosted by the AT-01 runtime tick."""

    def __init__(
        self,
        *,
        runtime: Any,
        portfolios: Callable[..., Mapping[str, Any]],
        market: Any,
        risk: Any,
        execution: Any,
        decisions: Any,
        learning_recorder: Callable[[Mapping[str, Any]], Any] | None = None,
        publisher: Callable[[str, Mapping[str, Any]], Awaitable[None] | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        stale_after_seconds: int = 300,
        position_limit: int = _MAX_POSITIONS_PER_CYCLE,
        submission_timeout_seconds: float = 30.0,
    ) -> None:
        self.runtime = runtime
        self._portfolios = portfolios
        self.market = market
        self.risk = risk
        self.execution = execution
        self._decisions = decisions
        self._learning_recorder = learning_recorder
        self._publisher = publisher
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self.position_limit = max(1, min(int(position_limit), _MAX_POSITIONS_PER_CYCLE))
        self.submission_timeout_seconds = max(1.0, float(submission_timeout_seconds))
        self.last_cycle: dict[str, Any] = {}
        self._last_error = ""
        self._closed = False
        self._lock = asyncio.Lock()
        self._configured_loader = getattr(execution, "_proposal_loader", None)
        if hasattr(execution, "set_proposal_loader"):
            execution.set_proposal_loader(self._engine_proposal)

    # -- lifecycle -----------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_error(self) -> str:
        return self._last_error

    def close(self) -> None:
        self._closed = True

    def _now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _runtime_status(self) -> dict[str, Any]:
        if self._closed:
            return {"mode": "disabled", "lease": {"valid": False}}
        try:
            result = self.runtime.status()
        except Exception as exc:  # noqa: BLE001 - a missing runtime fails closed
            self._last_error = f"runtime unavailable: {type(exc).__name__}: {exc}"
            return {"mode": "disabled", "lease": {"valid": False}}
        return dict(result) if isinstance(result, Mapping) else {}

    def _mode(self) -> str:
        return str(self._runtime_status().get("mode") or "disabled").strip().lower()

    def _lease_valid(self, status: Mapping[str, Any] | None = None) -> bool:
        body = dict(status or self._runtime_status())
        lease = body.get("lease")
        if isinstance(lease, Mapping):
            return bool(lease.get("valid")) and not bool(lease.get("conflict"))
        return bool(body.get("lease_valid"))

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "AT-05",
            "milestone": "AT-05",
            "contract_version": AT05_CONTRACT_VERSION,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": BROKER_CONNECTED,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "portfolio_authority": "existing portfolio engine",
            "supported_exit_conditions": list(SUPPORTED_EXIT_CONDITIONS),
            "unsupported_exit_conditions": list(UNSUPPORTED_EXIT_CONDITIONS),
            "supported_directions": list(SUPPORTED_EXIT_DIRECTIONS),
            "exit_configuration_source": (
                "horizon stop_loss_fraction plus persisted target_price and "
                "trailing_stop_fraction"
            ),
            "configuration_validation": {
                "target_price": "must be above average cost for a long position",
                "trailing_stop_percent": "greater than 0 and less than 100",
                "short_positions": "not supported by this paper build",
                "high_water_mark": "highest valid observed price, never lowered",
            },
            "condition_precedence": ["stop_loss", "trailing_stop", "target_price"],
            "modes": {
                "disabled": "no supervision or order action",
                "monitoring": "evaluate and persist triggers; never submit",
                "autonomous": "evaluate and submit eligible risk-approved exits",
                "paused": "no new entries; protective exits and reconciliation continue",
            },
            "one_cycle_at_a_time": True,
            "max_positions_per_cycle": self.position_limit,
            "duplicate_prevention": [
                "stable trigger key",
                "durable exit decision",
                "active decision and pending-order suppression",
                "NM-10 idempotency key",
                "NM-10 order lookup before retry",
            ],
            "learning": {
                "advisory_only": True,
                "automatic_promotion": False,
                "automatic_strategy_modification": False,
                "incomplete_evidence_is_labelled": True,
            },
            "never": [
                "place a live order",
                "contact a broker",
                "bypass NM-08 or NM-10",
                "write cash, positions, fills or ledger entries directly",
                "force end-of-day liquidation",
                "submit a short-position target or trailing exit",
            ],
        }

    # -- public cycle --------------------------------------------------------
    async def run_cycle(self, *, trigger: str = "scheduled") -> dict[str, Any]:
        if self._closed:
            return {"status": "skipped", "skipped_reason": "supervisor_closed",
                    "trigger": trigger}
        if self._lock.locked():
            return {"status": "skipped", "skipped_reason": "cycle_already_running",
                    "trigger": trigger}
        async with self._lock:
            return await self._run_cycle(trigger)

    async def _run_cycle(self, trigger: str) -> dict[str, Any]:
        started = self._now()
        runtime = self._runtime_status()
        mode = str(runtime.get("mode") or "disabled").lower()
        summary: dict[str, Any] = {
            "ok": True,
            "status": "completed",
            "phase": "AT-05",
            "trigger": trigger,
            "mode": mode,
            "started_at": started.isoformat(),
            "completed_at": "",
            "scanned": 0,
            "supervised": 0,
            "triggered": 0,
            "submitted": 0,
            "pending_exits": 0,
            "skipped_reason": "",
            "positions": [],
            "decisions": [],
            "paper_only": True,
            "live_trading": False,
        }
        if mode not in {"monitoring", "autonomous", "paused"}:
            summary.update(status="skipped", skipped_reason="runtime_disabled")
            summary["completed_at"] = self._now_iso()
            self.last_cycle = summary
            return summary
        submit = mode in {"autonomous", "paused"}
        if submit and not self._lease_valid(runtime):
            summary.update(status="skipped", skipped_reason="lease_not_held_or_expired")
            summary["completed_at"] = self._now_iso()
            self.last_cycle = summary
            return summary
        try:
            listing = await self._call(self._portfolios, limit=self.position_limit)
            rows = listing.get("portfolios", ()) if isinstance(listing, Mapping) else ()
            for portfolio_row in list(rows or ())[: self.position_limit]:
                portfolio_id = _text(
                    portfolio_row.get("portfolio_id") or portfolio_row.get("id")
                )
                if not portfolio_id:
                    continue
                try:
                    detail = await self._call(self._portfolios, portfolio_id=portfolio_id)
                except Exception as exc:  # noqa: BLE001 - one book must not stop the scan
                    summary["positions"].append(
                        {"portfolio_id": portfolio_id, "skipped": "portfolio_unavailable",
                         "error": f"{type(exc).__name__}: {exc}"}
                    )
                    continue
                if not isinstance(detail, Mapping):
                    continue
                portfolio = _as_dict(detail.get("portfolio") or {})
                status = _text(portfolio.get("status")).split(".")[-1].lower()
                if status and status != "active":
                    continue
                holdings = detail.get("holdings") or detail.get("positions") or ()
                for position in list(holdings)[: self.position_limit]:
                    result = await self._supervise_position(
                        portfolio_id=portfolio_id,
                        portfolio=portfolio,
                        position=_as_dict(position),
                        mode=mode,
                        submit=submit,
                    )
                    summary["scanned"] += 1
                    summary["positions"].append(result)
                    if not result.get("skipped"):
                        summary["supervised"] += 1
                    if result.get("triggered"):
                        summary["triggered"] += 1
                    if result.get("submitted"):
                        summary["submitted"] += 1
                    if result.get("pending_exit"):
                        summary["pending_exits"] += 1
                    if result.get("decision_id"):
                        summary["decisions"].append(result["decision_id"])
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            self._last_error = f"{type(exc).__name__}: {exc}"
            summary.update(status="failed", skipped_reason="supervision_failed",
                           error=self._last_error)
            await self._emit("cycle_failed", {"error": self._last_error})
        summary["completed_at"] = self._now_iso()
        self.last_cycle = summary
        await self._emit(
            "cycle_completed",
            {
                "trigger": trigger,
                "mode": mode,
                "scanned": summary["scanned"],
                "triggered": summary["triggered"],
                "submitted": summary["submitted"],
                "status": summary["status"],
            },
        )
        return summary

    # -- one position --------------------------------------------------------
    async def _supervise_position(
        self,
        *,
        portfolio_id: str,
        portfolio: Mapping[str, Any],
        position: Mapping[str, Any],
        mode: str,
        submit: bool,
    ) -> dict[str, Any]:
        instrument_id = _text(position.get("instrument_id") or position.get("symbol"))
        qty = quantity(position.get("quantity"))
        average_cost = money(position.get("average_cost"))
        opened_at = _text(position.get("opened_at") or position.get("created_at"))
        if qty <= 0 or not instrument_id:
            return {"portfolio_id": portfolio_id, "instrument_id": instrument_id,
                    "skipped": "closed_or_empty_position"}
        if average_cost <= 0:
            return {"portfolio_id": portfolio_id, "instrument_id": instrument_id,
                    "skipped": "invalid_average_cost"}
        horizon = _text(portfolio.get("horizon") or position.get("horizon") or "short_term")
        try:
            profile = profile_for(_HORIZON_MAP.get(horizon, horizon))
        except ValueError:
            return {"portfolio_id": portfolio_id, "instrument_id": instrument_id,
                    "skipped": "unsupported_horizon", "horizon": horizon}
        fraction = _decimal(profile.stop_loss_fraction)
        if fraction <= 0:
            return {"portfolio_id": portfolio_id, "instrument_id": instrument_id,
                    "skipped": "no_supported_stop_loss_configuration"}

        config = self.execution.store.exit_configuration(portfolio_id, instrument_id) or {}
        target_price = _decimal(config.get("target_price")) if _text(
            config.get("target_price")
        ) else Decimal("0")
        trailing_fraction = _decimal(config.get("trailing_stop_fraction")) if _text(
            config.get("trailing_stop_fraction")
        ) else Decimal("0")
        configuration_error = ""
        if target_price and target_price <= average_cost:
            configuration_error = "target_price is not above the long position average cost"
        if trailing_fraction and not Decimal("0") < trailing_fraction < Decimal("1"):
            configuration_error = "trailing_stop_fraction must be between 0 and 1"
        if configuration_error:
            return {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "skipped": "invalid_exit_configuration",
                "configuration_error": configuration_error,
            }

        observation = await self._observation(instrument_id)
        if observation.get("error"):
            return {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "skipped": observation["error"],
                "market_data_timestamp": observation.get("observed_at", ""),
            }
        price = quantity(observation["price"])
        observed_at = _text(observation["observed_at"])
        stop_price = money(average_cost * (Decimal("1") - fraction))

        state = self.execution.store.exit_state(portfolio_id, instrument_id) or {}
        previous_conditions = _as_dict(state.get("conditions"))
        epochs = _as_dict(state.get("condition_epochs"))
        pending = self._pending_sell_quantity(portfolio_id, instrument_id)
        active = self._find_open_decision(portfolio_id, instrument_id)
        if active and _text(active.get("order_id")):
            active = await self.reconcile_decision(_text(active["decision_id"]))
            pending = self._pending_sell_quantity(portfolio_id, instrument_id)
        available = max(Decimal("0"), qty - pending)

        high_water = Decimal("0")
        high_water_at = ""
        if trailing_fraction:
            high_water = _decimal(
                config.get("high_water_mark")
                or state.get("trailing_high_water")
                or price
            )
            high_water_at = _text(
                config.get("high_water_at") or state.get("high_water_at")
            )
            if price > high_water:
                high_water = price
                high_water_at = observed_at
            trailing_price = money(
                high_water * (Decimal("1") - trailing_fraction)
            ) if high_water > 0 else Decimal("0")
        else:
            trailing_price = Decimal("0")

        condition_rows: dict[str, dict[str, Any]] = {}
        definitions = (
            ("stop_loss", True, price <= stop_price, stop_price),
            (
                "trailing_stop",
                bool(trailing_fraction),
                bool(trailing_fraction) and price <= trailing_price,
                trailing_price,
            ),
            (
                "target_price",
                bool(target_price),
                bool(target_price) and price >= target_price,
                target_price,
            ),
        )
        for name, enabled, condition_triggered, value in definitions:
            prior = _as_dict(previous_conditions.get(name))
            epoch = int(epochs.get(name) or prior.get("epoch") or 0)
            rearmed_at = ""
            if (
                enabled
                and not condition_triggered
                and bool(prior.get("triggered"))
                and pending == 0
                and active is None
            ):
                epoch += 1
                rearmed_at = self._now_iso()
            condition_rows[name] = {
                "enabled": enabled,
                "triggered": bool(condition_triggered),
                "trigger_value": str(money(value)) if enabled and value else "",
                "epoch": epoch,
                "rearmed_at": rearmed_at,
            }

        triggered_condition = next(
            (
                name
                for name in ("stop_loss", "trailing_stop", "target_price")
                if condition_rows[name]["triggered"]
            ),
            "",
        )
        triggered = bool(triggered_condition)
        trigger_value = (
            _text(condition_rows[triggered_condition]["trigger_value"])
            if triggered_condition
            else ""
        )
        state.update(
            {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "position_id": _text(position.get("position_id") or position.get("id")),
                "horizon": horizon,
                "quantity": str(qty),
                "available_quantity": str(available),
                "average_cost": str(average_cost),
                "current_price": str(price),
                "current_price_timestamp": observed_at,
                "last_evaluated_price": str(price),
                "last_evaluated_market_timestamp": observed_at,
                "last_supervision_at": self._now_iso(),
                "pending_exit_quantity": str(pending),
                "stop_loss_fraction": str(fraction),
                "stop_loss_price": str(stop_price),
                "target_price": str(target_price) if target_price else "",
                "trailing_stop_fraction": (
                    str(trailing_fraction) if trailing_fraction else ""
                ),
                "trailing_high_water": str(high_water) if trailing_fraction else "",
                "high_water_at": high_water_at,
                "trailing_stop_price": str(trailing_price) if trailing_price else "",
                "config_id": _text(config.get("config_id")),
                "config_version": int(config.get("version") or 0),
                "configured_conditions": list(config.get("configured_conditions") or ()),
                "conditions": condition_rows,
                "condition_epochs": {
                    name: row["epoch"] for name, row in condition_rows.items()
                },
                "triggered": triggered,
                "trigger_condition": triggered_condition,
                "trigger_value": trigger_value,
                "trigger_epoch": condition_rows[triggered_condition]["epoch"]
                if triggered_condition
                else int(state.get("trigger_epoch") or 0),
                "supported_conditions": list(SUPPORTED_EXIT_CONDITIONS),
                "unsupported_conditions": list(UNSUPPORTED_EXIT_CONDITIONS),
                "mode": mode,
                "paper_only": True,
                "live_trading": False,
            }
        )

        if not triggered:
            if state.get("trigger_state") == "triggered" and not pending:
                state["rearmed_at"] = self._now_iso()
            state.update(
                trigger_state="armed",
                active_decision_id="",
                supervision_status="monitoring",
            )
            self.execution.store.save_exit_state(state)
            self._sync_configuration_state(config, state)
            return {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "supervision_status": "monitoring",
                "triggered": False,
                "current_price": str(price),
                "stop_loss_price": str(stop_price),
                "target_price": str(target_price) if target_price else "",
                "trailing_stop_price": str(trailing_price) if trailing_price else "",
                "trailing_high_water": str(high_water) if trailing_fraction else "",
                "available_quantity": str(available),
                "market_data_timestamp": observed_at,
            }

        if pending > 0:
            state.update(supervision_status="pending_exit",
                         active_decision_id=_text((active or {}).get("decision_id")))
            self.execution.store.save_exit_state(state)
            self._sync_configuration_state(config, state)
            return {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "supervision_status": "pending_exit",
                "triggered": True,
                "trigger_condition": triggered_condition,
                "pending_exit": pending,
                "pending_exit_quantity": str(pending),
                "decision_id": _text((active or {}).get("decision_id")),
                "duplicate_suppressed": True,
            }

        epoch = int(condition_rows[triggered_condition]["epoch"])
        trigger_material = "|".join(
            (
                portfolio_id,
                instrument_id,
                opened_at,
                str(qty),
                triggered_condition,
                trigger_value,
                _text(config.get("config_id")),
                str(config.get("version") or 0),
                str(epoch),
            )
        )
        trigger_key = hashlib.sha256(trigger_material.encode("utf-8")).hexdigest()
        decision_id = execution_id("xdec", trigger_key)
        proposal_id = execution_id("xprop", trigger_key)
        existing = self.execution.store.exit_decision(proposal_id=proposal_id)
        if existing is not None:
            current = _text(existing.get("status"))
            if current == ExitStatus.MONITORING.value and submit:
                decision = await self._execute_decision(existing, state)
                return decision
            if current in _ACTIVE_EXIT_STATUSES:
                state.update(supervision_status="pending_exit",
                             active_decision_id=decision_id)
                self.execution.store.save_exit_state(state)
                self._sync_configuration_state(config, state)
                return {
                    "portfolio_id": portfolio_id,
                    "instrument_id": instrument_id,
                    "supervision_status": "pending_exit",
                    "triggered": True,
                    "trigger_condition": triggered_condition,
                    "decision_id": decision_id,
                    "pending_exit": pending,
                    "duplicate_suppressed": True,
                }
            state.update(supervision_status=current, active_decision_id="",
                         trigger_state="triggered", trigger_condition=triggered_condition)
            self.execution.store.save_exit_state(state)
            self._sync_configuration_state(config, state)
            return {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "supervision_status": current,
                "triggered": True,
                "trigger_condition": triggered_condition,
                "decision_id": decision_id,
                "duplicate_suppressed": True,
                "terminal": current in TERMINAL_DECISION_STATUSES,
            }

        entry = self._entry_link(portfolio_id, instrument_id, self._now_iso()) or {}
        exchange = await self._exchange(instrument_id)
        decision = {
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "portfolio_id": portfolio_id,
            "instrument_id": instrument_id,
            "exchange": exchange,
            "position_id": _text(position.get("position_id") or position.get("id")),
            "horizon": horizon,
            "condition": triggered_condition,
            "trigger_value": trigger_value,
            "observed_price": str(price),
            "market_data_timestamp": observed_at,
            "position_quantity": str(qty),
            "available_quantity": str(available),
            "proposed_exit_quantity": str(available),
            "remaining_quantity": str(qty),
            "decided_at": self._now_iso(),
            "trigger_key": trigger_key,
            "trigger_epoch": epoch,
            "mode": mode,
            "config_id": _text(config.get("config_id")),
            "config_version": int(config.get("version") or 0),
            "target_price": str(target_price) if target_price else "",
            "trailing_stop_fraction": (
                str(trailing_fraction) if trailing_fraction else ""
            ),
            "trailing_high_water": str(high_water) if trailing_fraction else "",
            "trailing_stop_price": str(trailing_price) if trailing_price else "",
            "risk_outcome": "",
            "risk_assessment_id": "",
            "order_id": "",
            "filled_quantity": "0",
            "status": ExitStatus.MONITORING.value if not submit else ExitStatus.TRIGGERED.value,
            "strategy_id": _text(entry.get("strategy_id")),
            "strategy_version": _text(entry.get("strategy_version")),
            "signal_id": _text(entry.get("signal_id")),
            "entry_order_id": _text(entry.get("order_id")),
            "entry_proposal_id": _text(entry.get("proposal_id")),
            "entry_fill_ids": list(entry.get("fill_ids") or ()),
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
        }
        if available <= 0:
            state.update(supervision_status="no_available_quantity")
            self.execution.store.save_exit_state(state)
            self._sync_configuration_state(config, state)
            return {"portfolio_id": portfolio_id, "instrument_id": instrument_id,
                    "triggered": True, "trigger_condition": triggered_condition,
                    "skipped": "no_available_quantity"}
        self.execution.store.save_exit_decision(decision)
        self._event(
            decision_id,
            f"exit.{triggered_condition}.triggered",
            f"{triggered_condition.replace('_', ' ')} threshold reached",
            decision,
        )
        state.update(
            trigger_state="triggered",
            trigger_identity=trigger_key,
            active_decision_id=decision_id,
            trigger_condition=triggered_condition,
            supervision_status="monitoring_triggered" if not submit else "exit_processing",
        )
        self.execution.store.save_exit_state(state)
        self._sync_configuration_state(config, state, decision_id=decision_id)
        if not submit:
            return {
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "triggered": True,
                "trigger_condition": triggered_condition,
                "decision_id": decision_id,
                "supervision_status": "monitoring_triggered",
            }
        return await self._execute_decision(decision, state)

    # -- NM-08 / NM-10 -------------------------------------------------------
    async def _execute_decision(
        self, decision: Mapping[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        body = dict(decision)
        decision_id = _text(body.get("decision_id"))
        proposal_id = _text(body.get("proposal_id"))
        qty = quantity(body.get("proposed_exit_quantity"))
        price = quantity(body.get("observed_price"))
        if qty <= 0 or price <= 0:
            body.update(status=ExitStatus.FAILED.value,
                        failure_reason="invalid exit quantity or price")
            self.execution.store.update_exit_decision(body)
            return {"decision_id": decision_id, "status": "failed",
                    "triggered": True}
        if body.get("risk_outcome") == "approved" and body.get("risk_assessment_id"):
            assessment = await self._assessment(body)
        else:
            try:
                assessment = await self.risk.assess(
                    RiskAction.REMOVE_POSITION.value,
                    portfolio_id=_text(body.get("portfolio_id")),
                    proposal_id=proposal_id,
                    instrument_id=_text(body.get("instrument_id")),
                    side=Side.SELL.value,
                    notional=money(qty * price),
                    actor="at05-exit-supervisor",
                )
            except Exception as exc:  # noqa: BLE001 - a failed assessment is never approval
                body.update(status=ExitStatus.FAILED.value,
                            failure_reason=f"{type(exc).__name__}: {exc}")
                self.execution.store.update_exit_decision(body)
                self._finish_state(state, ExitStatus.FAILED.value)
                self._event(decision_id, "exit.risk_failed", body["failure_reason"], body)
                return {"decision_id": decision_id, "status": "failed",
                        "triggered": True}
        assessment = _as_dict(assessment)
        body["risk_assessment_id"] = _text(assessment.get("decision_id"))
        body["risk_outcome"] = _text(assessment.get("outcome")).lower()
        body["risk_policy_id"] = _text(assessment.get("policy_id"))
        body["risk_policy_version"] = _text(assessment.get("policy_version"))
        body["risk_state_version"] = _text(assessment.get("state_version"))
        body["risk_expires_at"] = _text(assessment.get("expires_at"))
        body["risk_restrictions"] = list(assessment.get("restrictions") or ())
        body["risk_reasons"] = [
            _text(item.get("message") or item.get("code") or item)
            for item in (assessment.get("violations") or ())
        ]
        self._event(
            decision_id,
            "exit.risk_assessed",
            body["risk_outcome"],
            {"decision_id": decision_id,
             "assessment_id": body["risk_assessment_id"],
             "outcome": body["risk_outcome"]},
        )
        if body["risk_outcome"] != "approved":
            body["status"] = ExitStatus.RISK_REJECTED.value
            self.execution.store.update_exit_decision(body)
            self._finish_state(state, ExitStatus.RISK_REJECTED.value)
            return {"decision_id": decision_id, "status": "risk_rejected",
                    "triggered": True,
                    "risk_assessment_id": body["risk_assessment_id"]}
        body["status"] = ExitStatus.RISK_APPROVED.value
        self.execution.store.update_exit_decision(body)
        try:
            preflight = await self.execution.preflight(proposal_id, quantity=qty)
        except Exception as exc:  # noqa: BLE001 - no order on a failed preflight
            preflight = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(preflight, Mapping) or not bool(preflight.get("ok")):
            existing = self.execution.store.order_for_proposal(proposal_id)
            if existing is None:
                body["status"] = ExitStatus.PREFLIGHT_FAILED.value
                body["failure_reason"] = _text(
                    (preflight or {}).get("error") if isinstance(preflight, Mapping) else ""
                ) or "NM-10 preflight refused the exit"
                self.execution.store.update_exit_decision(body)
                self._finish_state(state, ExitStatus.PREFLIGHT_FAILED.value)
                self._event(decision_id, "exit.preflight_failed",
                            body["failure_reason"], body)
                return {"decision_id": decision_id, "status": "preflight_failed",
                        "triggered": True}
        self._event(decision_id, "exit.risk_approved", "NM-08 approved protective exit", body)

        try:
            submitted = await asyncio.wait_for(
                self.execution.submit(
                    proposal_id,
                    quantity=qty,
                    idempotency_key=f"at05:{decision_id}",
                    actor="at05-exit-supervisor",
                ),
                timeout=self.submission_timeout_seconds,
            )
            order = _as_dict((submitted or {}).get("order"))
        except Exception as exc:  # noqa: BLE001 - look up a possibly committed order
            existing = self.execution.store.order_for_proposal(proposal_id)
            if existing is None:
                unknown = isinstance(exc, asyncio.TimeoutError)
                body.update(
                    status=(ExitStatus.UNKNOWN.value if unknown
                            else ExitStatus.FAILED.value),
                    failure_reason=f"{type(exc).__name__}: {exc}",
                )
                self.execution.store.update_exit_decision(body)
                self._finish_state(state, body["status"])
                self._event(decision_id, "exit.submission_unknown"
                            if unknown else "exit.submission_failed",
                            body["failure_reason"], body)
                return {"decision_id": decision_id, "status": body["status"],
                        "triggered": True, "submitted": False}
            order = existing.to_dict()
        order_id = _text(order.get("order_id"))
        body.update(
            order_id=order_id,
            order_status=_text(order.get("status")),
            status=ExitStatus.SUBMITTED.value,
            submitted_at=self._now_iso(),
        )
        self.execution.store.update_exit_decision(body)
        state.update(
            active_decision_id=decision_id,
            nm10_order_id=order_id,
            exit_proposal_id=proposal_id,
            risk_assessment_id=body["risk_assessment_id"],
            supervision_status="submitted",
        )
        self.execution.store.save_exit_state(state)
        self._sync_configuration_state(None, state, decision_id=decision_id,
                                       order_id=order_id)
        self._event(decision_id, "exit.submitted", order_id, body)
        reconciled = await self.reconcile_decision(decision_id)
        return {
            "portfolio_id": body["portfolio_id"],
            "instrument_id": body["instrument_id"],
            "triggered": True,
            "decision_id": decision_id,
            "order_id": order_id,
            "submitted": True,
            "status": reconciled.get("status"),
        }

    async def _assessment(self, decision: Mapping[str, Any]) -> dict[str, Any]:
        try:
            row = self.risk.store.decision(_text(decision.get("risk_assessment_id")))
        except Exception:
            row = None
        if isinstance(row, Mapping):
            return dict(row)
        return {"outcome": "unknown"}

    # -- reconciliation ------------------------------------------------------
    async def reconcile_decision(self, decision_id: str) -> dict[str, Any]:
        return await self._reconcile_decision(decision_id, record_event=True)

    async def recover(self) -> dict[str, Any]:
        """Reconcile durable exit work without submitting or replaying an exit.

        This is the AT-06 restart door.  It uses the existing NM-10 recovery and
        the existing per-decision reconciler; it never opens a new execution
        path and it never changes the runtime mode.
        """

        if self._closed:
            return {"ok": False, "status": "closed", "recovered": 0,
                    "reconciled": 0, "unresolved": []}
        inflight: dict[str, Any] = {}
        try:
            result = await self.execution.recover_pending()
            inflight = dict(result) if isinstance(result, Mapping) else {}
        except Exception as exc:  # noqa: BLE001 - recovery reports, never lies
            inflight = {"error": f"{type(exc).__name__}: {exc}"}
        recovered = 0
        reconciled = 0
        unresolved: list[str] = []
        for row in self.execution.store.exit_decisions(limit=500):
            order_id = _text(row.get("order_id"))
            if not order_id:
                continue
            status = _text(row.get("status"))
            if status not in _ACTIVE_EXIT_STATUSES:
                continue
            try:
                updated = await self._reconcile_decision(
                    _text(row.get("decision_id")), record_event=True
                )
            except Exception as exc:  # noqa: BLE001 - one bad row never hides others
                unresolved.append(_text(row.get("decision_id")))
                self._last_error = f"recovery failed for {row.get('decision_id')}: {exc}"
                continue
            recovered += 1
            if _text(updated.get("reconciliation_status")) == "passed":
                reconciled += 1
            elif _text(updated.get("status")) in {
                ExitStatus.RECONCILIATION_REQUIRED.value,
                ExitStatus.FAILED.value,
            }:
                unresolved.append(_text(updated.get("decision_id")))
        return {
            "ok": True,
            "status": "completed",
            "recovered_at": self._now_iso(),
            "inflight": inflight,
            "recovered": recovered,
            "reconciled": reconciled,
            "unresolved": unresolved,
            "unresolved_count": len(unresolved),
            "paper_only": True,
            "live_trading": False,
        }

    async def _reconcile_decision(
        self, decision_id: str, *, record_event: bool
    ) -> dict[str, Any]:
        decision = self.execution.store.exit_decision(decision_id=decision_id)
        if decision is None:
            raise AutonomyExitError(
                "EXIT_NOT_FOUND", f"unknown exit decision {decision_id!r}", http_status=404
            )
        order_id = _text(decision.get("order_id"))
        if not order_id:
            return dict(decision)
        order = self.execution.store.order(order_id)
        if order is None:
            decision.update(status=ExitStatus.RECONCILIATION_REQUIRED.value,
                            reconciliation_status="order_missing")
            self.execution.store.update_exit_decision(decision)
            return dict(decision)
        reconciliation: Mapping[str, Any] = {}
        try:
            reconciliation = await self.execution.reconcile(order_id)
            reconciliation = _as_dict(reconciliation)
        except Exception as exc:  # noqa: BLE001 - unresolved is reported, never passed
            reconciliation = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "issues": ["reconciliation unavailable"],
            }
        fills = list(self.execution.store.fills(order_id))
        filled = sum((fill.quantity for fill in fills), Decimal("0"))
        requested = order.requested_quantity
        remaining_order = max(Decimal("0"), requested - filled)
        position_qty = await self._position_quantity(
            _text(decision.get("portfolio_id")), _text(decision.get("instrument_id"))
        )
        recon_ok = bool(reconciliation.get("ok")) and not reconciliation.get("issues")
        status = self._order_decision_status(
            order.status, filled=filled, requested=requested, recon_ok=recon_ok
        )
        decision.update(
            status=status,
            order_status=order.status.value,
            filled_quantity=str(filled),
            remaining_order_quantity=str(remaining_order),
            position_quantity_after=str(position_qty),
            remaining_quantity=str(position_qty),
            position_closed=bool(position_qty == 0 and order.status is OrderStatus.FILLED
                                 and recon_ok),
            reconciliation_status=(
                "passed" if recon_ok else "failed" if reconciliation else "pending"
            ),
            reconciliation_issues=list(reconciliation.get("issues") or ()),
            reconciliation_id=_text(reconciliation.get("reconciliation_id")),
            reconciled_at=self._now_iso(),
            average_exit_price=(
                str(
                    sum((fill.quantity * fill.price for fill in fills), Decimal("0"))
                    / filled
                )
                if filled > 0
                else ""
            ),
            total_fees=str(sum((fill.total_fees for fill in fills), Decimal("0"))),
        )
        self.execution.store.update_exit_decision(decision)
        state = self.execution.store.exit_state(
            _text(decision.get("portfolio_id")), _text(decision.get("instrument_id"))
        ) or {}
        state.update(
            nm10_order_id=order_id,
            filled_quantity=str(filled),
            remaining_order_quantity=str(remaining_order),
            remaining_quantity=str(position_qty),
            position_closed=decision["position_closed"],
            reconciliation_status=decision["reconciliation_status"],
            reconciliation_id=decision["reconciliation_id"],
            last_reconciled_at=decision["reconciled_at"],
            supervision_status=status,
        )
        if status not in _ACTIVE_EXIT_STATUSES:
            state["active_decision_id"] = ""
        self.execution.store.save_exit_state(state)
        self._sync_configuration_state(
            None, state, decision_id=decision_id, order_id=order_id,
            reconciliation_status=str(decision["reconciliation_status"]),
        )
        if filled > 0:
            try:
                await self._record_learning(decision, order, fills, position_qty, reconciliation)
            except Exception as exc:  # noqa: BLE001 - accounting is already authoritative
                self._event(decision_id, "learning.evidence_failed", str(exc), {
                    "decision_id": decision_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        self._event(decision_id, "exit.reconciled", status, {
            "decision_id": decision_id,
            "order_id": order_id,
            "filled_quantity": str(filled),
            "position_quantity_after": str(position_qty),
            "reconciliation_status": decision["reconciliation_status"],
        })
        return dict(decision)

    @staticmethod
    def _order_decision_status(
        order_status: OrderStatus, *, filled: Decimal, requested: Decimal, recon_ok: bool
    ) -> str:
        if order_status is OrderStatus.FILLED:
            if filled != requested:
                return ExitStatus.RECONCILIATION_REQUIRED.value
            return ExitStatus.FILLED.value if recon_ok else ExitStatus.RECONCILIATION_REQUIRED.value
        if order_status is OrderStatus.PARTIALLY_FILLED:
            return ExitStatus.PARTIALLY_FILLED.value
        if order_status is OrderStatus.REJECTED:
            return ExitStatus.REJECTED.value
        if order_status is OrderStatus.CANCELLED:
            return ExitStatus.CANCELLED.value
        if order_status is OrderStatus.EXPIRED:
            return ExitStatus.EXPIRED.value
        if order_status is OrderStatus.FAILED:
            return ExitStatus.FAILED.value
        if order_status in {
            OrderStatus.CREATED,
            OrderStatus.PENDING_VALIDATION,
            OrderStatus.ACCEPTED,
            OrderStatus.OPEN,
            OrderStatus.CANCEL_PENDING,
        }:
            return ExitStatus.SUBMITTED.value
        return ExitStatus.UNKNOWN.value

    def _finish_state(self, state: dict[str, Any], status: str) -> None:
        state.update(supervision_status=status, active_decision_id="",
                     trigger_state="triggered")
        self.execution.store.save_exit_state(state)

    def _pending_sell_quantity(self, portfolio_id: str, instrument_id: str) -> Decimal:
        total = Decimal("0")
        for order in self.execution.store.orders(portfolio_id=portfolio_id, limit=1000):
            if (
                order.instrument_id == instrument_id
                and order.side.value == Side.SELL.value
                and order.status.value in _NON_TERMINAL_ORDER_STATES
            ):
                total += max(Decimal("0"), order.remaining_quantity)
        return total

    def _find_open_decision(
        self, portfolio_id: str, instrument_id: str
    ) -> dict[str, Any] | None:
        for row in self.execution.store.exit_decisions(portfolio_id=portfolio_id, limit=500):
            if _text(row.get("instrument_id")) != instrument_id:
                continue
            if _text(row.get("status")) in _ACTIVE_EXIT_STATUSES:
                return dict(row)
        return None

    async def _position_quantity(self, portfolio_id: str, instrument_id: str) -> Decimal:
        try:
            detail = await self._call(self._portfolios, portfolio_id=portfolio_id)
        except Exception:
            return Decimal("0")
        for row in (detail.get("holdings") or ()) if isinstance(detail, Mapping) else ():
            body = _as_dict(row)
            if _text(body.get("instrument_id")) == instrument_id:
                return quantity(body.get("quantity"))
        return Decimal("0")

    # -- market data ---------------------------------------------------------
    async def _observation(self, instrument_id: str) -> dict[str, Any]:
        try:
            result = self.market.latest(instrument_id)
        except Exception as exc:  # noqa: BLE001 - missing data never triggers an exit
            return {"error": "missing_market_data", "detail": f"{type(exc).__name__}: {exc}"}
        usable = result.get("usable") if isinstance(result, Mapping) else getattr(result, "usable", None)
        selected = (
            result.get("selected") if isinstance(result, Mapping)
            else getattr(result, "selected", None)
        )
        if usable is False or selected is None:
            return {"error": "missing_market_data"}
        price = selected.get("price") if isinstance(selected, Mapping) else getattr(selected, "price", None)
        observed = (
            selected.get("observed_at") if isinstance(selected, Mapping)
            else getattr(selected, "observed_at", None)
        )
        if price in (None, "") or observed in (None, ""):
            return {"error": "missing_market_data"}
        try:
            moment = _time(observed)
        except Exception:
            return {"error": "invalid_market_data_timestamp"}
        age = (self._now() - moment).total_seconds()
        if age > self.stale_after_seconds:
            return {
                "error": "stale_market_data",
                "observed_at": moment.isoformat(),
                "age_seconds": age,
            }
        try:
            value = money(price)
        except Exception:
            return {"error": "invalid_market_price"}
        if value <= 0:
            return {"error": "invalid_market_price"}
        return {"price": value, "observed_at": moment.isoformat()}

    async def _exchange(self, instrument_id: str) -> str:
        try:
            instrument = self.market.get_instrument(instrument_id)
        except Exception:
            return instrument_id.split(":", 1)[0]
        if isinstance(instrument, Mapping):
            return _text(instrument.get("exchange") or instrument.get("mic"))
        return _text(getattr(instrument, "exchange", "") or getattr(instrument, "mic", ""))

    # -- learning evidence ---------------------------------------------------
    async def _record_learning(
        self,
        decision: Mapping[str, Any],
        order: Any,
        fills: list[Any],
        position_qty: Decimal,
        reconciliation: Mapping[str, Any],
    ) -> dict[str, Any]:
        entry = self._entry_link(
            _text(decision["portfolio_id"]),
            _text(decision["instrument_id"]),
            _text(decision.get("created_at") or decision.get("decided_at")),
        )
        valuation: dict[str, Any] = {}
        holding_realized = ""
        try:
            detail = await self._call(
                self._portfolios, portfolio_id=_text(decision["portfolio_id"])
            )
            valuation = _as_dict((detail or {}).get("valuation"))
            for row in (detail or {}).get("holdings") or ():
                body = _as_dict(row)
                if _text(body.get("instrument_id")) == _text(decision["instrument_id"]):
                    holding_realized = _text(body.get("realized_pnl"))
                    break
        except Exception as exc:  # noqa: BLE001 - evidence states the gap
            valuation = {"error": f"{type(exc).__name__}: {exc}"}
        fill_rows = [
            {
                "fill_id": fill.fill_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "gross_value": str(fill.gross_value),
                "total_fees": str(fill.total_fees),
                "net_cash": str(fill.net_cash),
                "executed_at": fill.executed_at.isoformat(),
                "ledger_transaction_ids": list(fill.ledger_transaction_ids),
                "position_effect": dict(fill.position_effect),
            }
            for fill in fills
        ]
        missing: list[str] = []
        if not entry:
            missing.append("entry order and fills")
        if not fill_rows:
            missing.append("exit fills")
        if not _text(decision.get("strategy_id")):
            missing.append("strategy identity")
        if not _text(decision.get("strategy_version")):
            missing.append("strategy version")
        if not valuation or valuation.get("error"):
            missing.append("authoritative portfolio valuation")
        if _text(decision.get("reconciliation_status")) != "passed":
            missing.append("passed ledger reconciliation")
        position_closed = bool(decision.get("position_closed"))
        if position_qty > 0:
            missing.append("remaining position quantity")
        if not missing and position_closed:
            status = "complete"
        elif fill_rows:
            status = "partial"
        else:
            status = "incomplete"
        holding_seconds = self._holding_seconds(entry)
        evidence = {
            "evidence_id": execution_id("xev", decision["decision_id"]),
            "decision_id": decision["decision_id"],
            "status": status,
            "attribution_status": status,
            "kind": "paper",
            "portfolio_id": decision["portfolio_id"],
            "instrument_id": decision["instrument_id"],
            "exchange": decision.get("exchange", ""),
            "horizon": decision.get("horizon", ""),
            "strategy_id": decision.get("strategy_id", ""),
            "strategy_version": decision.get("strategy_version", ""),
            "signal_id": decision.get("signal_id", ""),
            "entry": entry,
            "exit": {
                "condition": decision.get("condition", "stop_loss"),
                "trigger_value": decision.get("trigger_value", ""),
                "observed_price": decision.get("observed_price", ""),
                "market_data_timestamp": decision.get("market_data_timestamp", ""),
                "target_price": decision.get("target_price", ""),
                "trailing_stop_fraction": decision.get("trailing_stop_fraction", ""),
                "trailing_high_water": decision.get("trailing_high_water", ""),
                "trailing_stop_price": decision.get("trailing_stop_price", ""),
                "config_id": decision.get("config_id", ""),
                "config_version": decision.get("config_version", 0),
                "decision_id": decision["decision_id"],
                "proposal_id": decision.get("proposal_id", ""),
                "risk_assessment_id": decision.get("risk_assessment_id", ""),
                "risk_outcome": decision.get("risk_outcome", ""),
                "order_id": order.order_id,
                "order_status": order.status.value,
                "fills": fill_rows,
                "reconciliation_status": decision.get("reconciliation_status", ""),
                "reconciliation_id": decision.get("reconciliation_id", ""),
            },
            "realized": {
                "source": "authoritative portfolio valuation",
                "portfolio_realized_pnl": _text(valuation.get("realized_pnl")),
                "portfolio_return_pct": _text(valuation.get("return_pct")),
                "position_realized_pnl": holding_realized,
                "fees_total": decision.get("total_fees", "0"),
                "holding_duration_seconds": holding_seconds,
                "note": "realized figures are read from accounting, never recomputed here",
            },
            "position_quantity_after": str(position_qty),
            "position_closed": position_closed,
            "missing_evidence": missing,
            "complete": status == "complete",
            "advisory_only": True,
            "auto_promoted": False,
            "strategy_modified": False,
            "risk_limits_modified": False,
            "paper_only": True,
            "live_trading": False,
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "reconciliation": dict(reconciliation),
        }
        stored = self.execution.store.save_learning_evidence(evidence)
        if self._learning_recorder is not None:
            try:
                record = self._learning_recorder(stored)
                if inspect.isawaitable(record):
                    record = await record
                if isinstance(record, Mapping):
                    stored["learning_service"] = {
                        "recorded": bool(record.get("ok")),
                        "duplicate": bool(record.get("duplicate")),
                        "evidence": _as_dict(record.get("evidence")),
                    }
            except Exception as exc:  # noqa: BLE001 - the local evidence remains
                stored["learning_service"] = {
                    "recorded": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            stored = self.execution.store.save_learning_evidence(stored)
        self._event(decision["decision_id"], "learning.evidence_updated", status, {
            "decision_id": decision["decision_id"],
            "evidence_id": stored["evidence_id"],
            "status": status,
            "missing": missing,
        })
        return dict(stored)

    def _entry_link(
        self, portfolio_id: str, instrument_id: str, opened_at: str
    ) -> dict[str, Any] | None:
        try:
            reference = _time(opened_at) if opened_at else self._now()
        except ValueError:
            reference = self._now()
        candidates = [
            order
            for order in self.execution.store.orders(portfolio_id=portfolio_id, limit=1000)
            if order.instrument_id == instrument_id
            and order.side.value == Side.BUY.value
            and order.status is OrderStatus.FILLED
            and order.created_at <= reference
        ]
        if not candidates:
            return None
        entry = candidates[-1]
        proposal: dict[str, Any] = {}
        try:
            proposal = _as_dict(self._decisions.store.proposal(entry.proposal_id))
        except Exception:
            proposal = {}
        fills = list(self.execution.store.fills(entry.order_id))
        if not fills:
            return None
        return {
            "strategy_id": _text(proposal.get("strategy_id")),
            "strategy_version": _text(proposal.get("strategy_version")),
            "signal_id": _text(proposal.get("signal_id")),
            "run_id": _text(proposal.get("run_id")),
            "proposal_id": entry.proposal_id,
            "risk_decision_id": entry.risk_decision_id,
            "order_id": entry.order_id,
            "order_status": entry.status.value,
            "fill_ids": [fill.fill_id for fill in fills],
            "filled_quantity": str(sum((fill.quantity for fill in fills), Decimal("0"))),
            "average_price": str(
                sum((fill.quantity * fill.price for fill in fills), Decimal("0"))
                / sum((fill.quantity for fill in fills), Decimal("1"))
            ),
            "total_fees": str(sum((fill.total_fees for fill in fills), Decimal("0"))),
            "opened_at": entry.created_at.isoformat(),
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "total_fees": str(fill.total_fees),
                    "executed_at": fill.executed_at.isoformat(),
                    "ledger_transaction_ids": list(fill.ledger_transaction_ids),
                }
                for fill in fills
            ],
        }

    def _holding_seconds(self, entry: Mapping[str, Any] | None) -> int:
        opened = _text((entry or {}).get("opened_at"))
        if not opened:
            return 0
        try:
            return max(0, int((self._now() - _time(opened)).total_seconds()))
        except ValueError:
            return 0

    # -- API-facing views ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        decisions = self.execution.store.exit_decisions(limit=500)
        configurations = self.execution.store.exit_configurations(limit=500)
        counts: dict[str, int] = {}
        by_condition: dict[str, int] = {}
        for row in decisions:
            state = _text(row.get("status")) or "unknown"
            counts[state] = counts.get(state, 0) + 1
            condition = _text(row.get("condition")) or "unknown"
            by_condition[condition] = by_condition.get(condition, 0) + 1
        return {
            "ok": True,
            "phase": "AT-05",
            "initialised": True,
            "mode": self._mode(),
            "closed": self._closed,
            "last_cycle": self.last_cycle,
            "last_error": self._last_error,
            "counts": {
                "decisions": len(decisions),
                **counts,
            },
            "open_decisions": sum(
                1 for row in decisions if _text(row.get("status")) in _ACTIVE_EXIT_STATUSES
            ),
            "supervised_positions": len(self.execution.store.exit_states(limit=500)),
            "configured_positions": len(configurations),
            "by_condition": by_condition,
            "supported_exit_conditions": list(SUPPORTED_EXIT_CONDITIONS),
            "unsupported_exit_conditions": list(UNSUPPORTED_EXIT_CONDITIONS),
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
        }

    def supervision_view(self) -> dict[str, Any]:
        return {
            "mode": self._mode(),
            "last_cycle": self.last_cycle or {},
            "positions": list(self.execution.store.exit_states(limit=200)),
            "configurations": list(self.execution.store.exit_configurations(limit=200)),
            "last_error": self._last_error,
        }

    def exit_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        decision = self.execution.store.exit_decision(proposal_id=proposal_id)
        if not decision or decision.get("risk_outcome") != "approved":
            return None
        assessment = self._risk_decision_sync(_text(decision.get("risk_assessment_id")))
        if not assessment or _text(assessment.get("outcome")).lower() != "approved":
            return None
        expires = _text(decision.get("risk_expires_at"))
        if not expires:
            return None
        return {
            "proposal_id": proposal_id,
            "strategy_id": decision.get("strategy_id") or "at05_protective_exit",
            "strategy_version": decision.get("strategy_version") or "1",
            "signal_id": decision.get("decision_id", ""),
            "portfolio_id": decision.get("portfolio_id", ""),
            "instrument_id": decision.get("instrument_id", ""),
            "exchange": decision.get("exchange", ""),
            "horizon": decision.get("horizon", ""),
            "side": Side.SELL.value,
            "action": RiskAction.REMOVE_POSITION.value,
            "notional": money(
                quantity(decision["proposed_exit_quantity"])
                * quantity(decision["observed_price"])
            ),
            "reference_price": quantity(decision["observed_price"]),
            "risk_decision_id": decision["risk_assessment_id"],
            "risk_outcome": "approved",
            "risk_state_version": decision.get("risk_state_version", ""),
            "risk_restrictions": list(decision.get("risk_restrictions") or ()),
            "expires_at": expires,
            "created_at": decision.get("decided_at", ""),
            "status": "approved",
            "reason": f"AT-05 protective {decision.get('condition', 'stop_loss')} exit",
        }

    def _engine_proposal(self, proposal_id: str) -> Mapping[str, Any] | None:
        proposal = self.exit_proposal(proposal_id)
        if proposal is not None:
            return proposal
        previous = self._configured_loader
        if previous is None:
            return None
        try:
            loaded = previous(proposal_id)
            return dict(loaded) if isinstance(loaded, Mapping) else None
        except Exception:
            return None

    def _risk_decision_sync(self, decision_id: str) -> dict[str, Any]:
        try:
            return _as_dict(self.risk.store.decision(decision_id))
        except Exception:
            return {}

    def decisions(self, *, portfolio_id: str = "", limit: int = 100) -> dict[str, Any]:
        rows = self.execution.store.exit_decisions(
            portfolio_id=portfolio_id, limit=limit
        )
        return {"ok": True, "phase": "AT-05", "decisions": list(rows),
                "total": len(rows), "paper_only": True, "live_trading": False}

    def decision(self, decision_id: str) -> dict[str, Any]:
        row = self.execution.store.exit_decision(decision_id=decision_id)
        if row is None:
            raise AutonomyExitError(
                "EXIT_NOT_FOUND", f"unknown exit decision {decision_id!r}", http_status=404
            )
        return {
            "ok": True,
            "decision": row,
            "events": list(self.execution.store.exit_events(decision_id, limit=500)),
            "paper_only": True,
            "live_trading": False,
        }

    def events(self, decision_id: str, *, limit: int = 200) -> dict[str, Any]:
        row = self.execution.store.exit_decision(decision_id=decision_id)
        if row is None:
            raise AutonomyExitError(
                "EXIT_NOT_FOUND", f"unknown exit decision {decision_id!r}", http_status=404
            )
        events = self.execution.store.exit_events(decision_id, limit=limit)
        return {"ok": True, "decision_id": decision_id,
                "events": list(events), "total": len(events)}

    def learning(self, *, decision_id: str = "", limit: int = 100) -> dict[str, Any]:
        rows = self.execution.store.learning_evidence(
            decision_id=decision_id, limit=limit
        )
        return {
            "ok": True,
            "phase": "AT-05",
            "evidence": list(rows),
            "total": len(rows),
            "advisory_only": True,
            "auto_promoted": False,
            "complete": sum(1 for row in rows if row.get("status") == "complete"),
            "partial": sum(1 for row in rows if row.get("status") == "partial"),
            "incomplete": sum(1 for row in rows if row.get("status") == "incomplete"),
        }

    # -- configuration -------------------------------------------------------
    def configurations(self, *, limit: int = 200) -> dict[str, Any]:
        rows = list(self.execution.store.exit_configurations(limit=limit))
        return {
            "ok": True,
            "phase": "AT-05",
            "configurations": rows,
            "total": len(rows),
            "paper_only": True,
            "live_trading": False,
        }

    def configure_exit(
        self,
        portfolio_id: str,
        instrument_id: str,
        *,
        position_id: str = "",
        target_price: object = None,
        trailing_stop_fraction: object = None,
        trailing_stop_percent: object = None,
        average_cost: object = None,
    ) -> dict[str, Any]:
        existing = self.execution.store.exit_configuration(portfolio_id, instrument_id)
        body = build_exit_configuration(
            portfolio_id,
            instrument_id,
            position_id=position_id,
            target_price=target_price,
            trailing_stop_fraction=trailing_stop_fraction,
            trailing_stop_percent=trailing_stop_percent,
            average_cost=average_cost,
            existing=existing,
        )
        stored = self.execution.store.save_exit_configuration(body)
        return {"ok": True, "configuration": stored}

    def remove_exit_configuration(self, portfolio_id: str, instrument_id: str) -> dict[str, Any]:
        removed = self.execution.store.delete_exit_configuration(
            portfolio_id, instrument_id
        )
        return {"ok": True, "removed": removed,
                "portfolio_id": portfolio_id, "instrument_id": instrument_id}

    def _sync_configuration_state(
        self,
        config: Mapping[str, Any] | None,
        state: Mapping[str, Any],
        *,
        decision_id: str = "",
        order_id: str = "",
        reconciliation_status: str = "",
    ) -> None:
        """Mirror market/trigger state onto the durable configuration row.

        The configuration row remains the owner of target/trailing definitions
        and the high-water mark; the exit decision remains the owner of the
        risk/order/fill outcome.  This is a projection for restart-safe reads,
        not a second financial record.
        """

        body = dict(config or {})
        if not body:
            return
        body.update(
            {
                "position_id": _text(state.get("position_id") or body.get("position_id")),
                "high_water_mark": _text(
                    state.get("trailing_high_water") or body.get("high_water_mark")
                ),
                "high_water_at": _text(
                    state.get("high_water_at") or body.get("high_water_at")
                ),
                "last_evaluated_price": _text(state.get("last_evaluated_price")),
                "last_market_data_timestamp": _text(
                    state.get("last_evaluated_market_timestamp")
                ),
                "current_trailing_stop_price": _text(
                    state.get("trailing_stop_price")
                ),
                "trigger_status": "triggered" if state.get("triggered") else "armed",
                "active_decision_id": _text(
                    decision_id or state.get("active_decision_id") or ""
                ),
                "risk_assessment_id": _text(
                    state.get("risk_assessment_id") or body.get("risk_assessment_id")
                ),
                "order_id": _text(order_id or state.get("nm10_order_id") or ""),
                "submitted_quantity": _text(
                    state.get("submitted_quantity") or body.get("submitted_quantity")
                ),
                "filled_quantity": _text(
                    state.get("filled_quantity") or body.get("filled_quantity")
                ),
                "remaining_quantity": _text(
                    state.get("remaining_quantity") or body.get("remaining_quantity")
                ),
                "reconciliation_status": _text(
                    reconciliation_status
                    or state.get("reconciliation_status")
                    or body.get("reconciliation_status")
                ),
            }
        )
        try:
            self.execution.store.update_exit_configuration_state(body)
        except Exception as exc:  # noqa: BLE001 - decision journal remains primary
            _LOGGER.warning("novamarket.autonomy.exit.config_state_failed: %s", exc)

    # -- utilities -----------------------------------------------------------
    async def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        result = fn(**kwargs)
        return await result if inspect.isawaitable(result) else result

    async def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        publisher = self._publisher
        if publisher is None:
            return
        try:
            result = publisher(f"novamarket.autonomy.exit.{kind}", dict(payload))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - observability never blocks safety
            _LOGGER.warning("novamarket.autonomy.exit.event_failed kind=%s error=%s",
                            kind, exc)

    def _event(self, decision_id: str, kind: str, message: str,
               payload: Mapping[str, Any]) -> None:
        try:
            self.execution.store.record_exit_event(decision_id, kind, message, payload)
        except Exception as exc:  # noqa: BLE001 - decision persistence remains primary
            _LOGGER.warning("novamarket.autonomy.exit.event_failed: %s", exc)
