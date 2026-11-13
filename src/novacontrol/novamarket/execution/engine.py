"""NM-10 realistic paper-execution engine.

The engine has no broker client and no live-trade path.  It consumes NM-09's
approved proposals, revalidates the exact NM-08 decision, reserves simulated
cash or shares, and records fills only after the NM-03 ledger and NM-07
position writes have succeeded.  Cross-store operations are deliberately
ordered and reconciled rather than described as one atomic SQLite transaction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping

from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.execution.models import (
    TERMINAL_STATUSES,
    Fill,
    MarketSnapshot,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Reservation,
    SHARE,
    execution_id,
    money,
    parse_time,
    quantity,
    text,
)
from novacontrol.novamarket.execution.pricing import (
    ExecutionPricingError,
    execution_price,
    fee_breakdown,
    snapshot_for,
)
from novacontrol.novamarket.execution.reconciliation import ExecutionReconciler
from novacontrol.novamarket.execution.store import ExecutionStore, ExecutionStoreError
from novacontrol.novamarket.portfolio.models import Position

_LOGGER = logging.getLogger("novacontrol.novamarket.execution")

Publisher = Callable[[str, Mapping[str, Any]], Awaitable[None]]

_RISK_ACTIONS = {
    "buy": "add_position",
    "add_position": "add_position",
    "sell": "reduce_position",
    "reduce_position": "reduce_position",
    "exit": "remove_position",
    "remove_position": "remove_position",
}


class ExecutionError(RuntimeError):
    """A paper operation could not be completed safely."""


class ExecutionValidationError(ExecutionError, ValueError):
    """The proposal or order cannot be accepted under NM-10 rules."""


class UnsupportedOrderType(ExecutionValidationError):
    """The requested order type is not implemented."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionEngine:
    """Deterministic paper order lifecycle, fills, reservations and reconciliation."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        wallets: Any,
        portfolios: Any,
        risk: Any,
        strategies: Any,
        market_data: Any | None = None,
        config: ExecutionConfig | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        proposal_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self._store = store
        self._wallets = wallets
        self._portfolios = portfolios
        self._risk = risk
        self._strategies = strategies
        self._proposal_loader = proposal_loader
        self._market_data = market_data
        self._config = config or ExecutionConfig()
        self._publisher = publisher
        self._clock = clock or _now_utc
        self._last_error = ""
        self._latencies: list[float] = []
        self._lock = asyncio.Lock()
        self._clearing_wallet_id = "pfw_market_clearing"
        # A paper market counterparty needs bounded starting liquidity so a
        # profitable exit can settle.  Without it a sell can only pay out the
        # proceeds of earlier buys, which makes every profitable target exit
        # fail even though the portfolio and order records are valid.
        self._clearing_liquidity = Decimal("1000000000.00")
        self._reconciler = ExecutionReconciler(
            store,
            wallets=wallets,
            portfolios=portfolios,
            config=self._config,
            clock=self._clock,
        )

    # -- access --------------------------------------------------------------
    @property
    def store(self) -> ExecutionStore:
        return self._store

    @property
    def config(self) -> ExecutionConfig:
        return self._config

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_market_data(self, market_data: Any | None) -> None:
        self._market_data = market_data

    def set_proposal_loader(
        self, loader: Callable[[str], Mapping[str, Any] | None] | None
    ) -> None:
        """Add an additive proposal source without replacing strategy storage."""

        self._proposal_loader = loader

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    def set_config(self, config: ExecutionConfig) -> None:
        self._config = config
        self._reconciler = ExecutionReconciler(
            self._store,
            wallets=self._wallets,
            portfolios=self._portfolios,
            config=config,
            clock=self._clock,
        )

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    async def _emit(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(name, dict(payload))
        except Exception as exc:  # noqa: BLE001 - events never roll back a fill
            _LOGGER.warning("novamarket.execution.event_failed event=%s error=%s", name, exc)

    def _event_payload(self, order: Order, reason: str = "") -> dict[str, Any]:
        return {
            "order_id": order.order_id,
            "proposal_id": order.proposal_id,
            "portfolio_id": order.portfolio_id,
            "status": order.status.value,
            "reason": reason,
        }

    async def _status_event(self, order: Order, reason: str = "") -> None:
        name = {
            OrderStatus.CREATED: "novamarket.order.created",
            OrderStatus.ACCEPTED: "novamarket.order.validated",
            OrderStatus.OPEN: "novamarket.order.opened",
            OrderStatus.PARTIALLY_FILLED: "novamarket.order.partially_filled",
            OrderStatus.FILLED: "novamarket.order.filled",
            OrderStatus.CANCEL_PENDING: "novamarket.order.cancel_requested",
            OrderStatus.CANCELLED: "novamarket.order.cancelled",
            OrderStatus.REJECTED: "novamarket.order.rejected",
            OrderStatus.EXPIRED: "novamarket.order.expired",
            OrderStatus.FAILED: "novamarket.order.failed",
        }.get(order.status)
        if name:
            await self._emit(name, self._event_payload(order, reason))

    # -- submission ----------------------------------------------------------
    async def submit(
        self,
        proposal_id: str,
        *,
        quantity: object | None = None,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: object | None = None,
        trigger_price: object | None = None,
        expires_at: datetime | str | None = None,
        idempotency_key: str = "",
        actor: str = "",
        execute: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._recover_inflight_locked()
            return await self._submit_locked(
                proposal_id,
                requested=quantity,
                order_type=order_type,
                limit_price=limit_price,
                trigger_price=trigger_price,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
                actor=actor,
                execute=execute,
            )

    async def _submit_locked(
        self,
        proposal_id: str,
        *,
        requested: object | None,
        order_type: OrderType | str,
        limit_price: object | None,
        trigger_price: object | None,
        expires_at: datetime | str | None,
        idempotency_key: str,
        actor: str,
        execute: bool,
    ) -> dict[str, Any]:
        started = self._now()
        proposal_key = str(proposal_id or "").strip()
        if not proposal_key:
            raise ExecutionValidationError("proposal_id is required")
        raw_type = str(order_type).strip().lower().replace("-", "_")
        try:
            wanted_type = OrderType(raw_type)
        except ValueError as exc:
            raise UnsupportedOrderType(
                f"unsupported paper order type {order_type!r}; supported: "
                + ", ".join(item.value for item in OrderType)
            ) from exc
        existing = self._store.order_for_proposal(proposal_key)
        if existing is not None:
            return {
                "ok": existing.status is not OrderStatus.REJECTED,
                "replayed": True,
                "order": existing.to_dict(),
                "fills": [fill.to_dict() for fill in self._store.fills(existing.order_id)],
            }
        proposal = self._proposal(proposal_key)
        side = self._side(proposal)
        expiry = self._expiry(proposal, expires_at)
        order = Order(
            order_id=execution_id("ord", proposal_key),
            proposal_id=proposal_key,
            strategy_id=str(proposal.get("strategy_id", "")),
            portfolio_id=str(proposal.get("portfolio_id", "")),
            instrument_id=str(proposal.get("instrument_id", "")),
            side=side,
            order_type=wanted_type,
            requested_quantity=Decimal("0"),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("0"),
            status=OrderStatus.CREATED,
            risk_decision_id=str(proposal.get("risk_decision_id", "")),
            execution_config_version=self._config.version,
            created_at=started,
            updated_at=started,
            expires_at=expiry,
            limit_price=money(limit_price) if limit_price is not None else None,
            trigger_price=money(trigger_price) if trigger_price is not None else None,
            reference_price=(
                money(proposal["reference_price"])
                if proposal.get("reference_price") not in (None, "")
                else None
            ),
            idempotency_key=str(idempotency_key or ""),
            actor=str(actor or ""),
            reason=str(proposal.get("reason", "")),
        )
        self._store.save_order(order)
        await self._status_event(order, "proposal received")
        order = self._store.transition(order, OrderStatus.PENDING_VALIDATION, reason="proposal received")

        try:
            validated, snapshot = await self._validate(order, proposal, requested)
            self._store.save_order(validated)
            order = self._store.transition(validated, OrderStatus.ACCEPTED, reason="validated")
            await self._status_event(order, "risk and portfolio validation passed")
            await self._reserve(order, snapshot)
            order = self._store.transition(order, OrderStatus.OPEN, reason="simulated order accepted")
            await self._status_event(order, "awaiting paper execution")
        except ExecutionValidationError as exc:
            order = await self._reject(order, str(exc))
            return {
                "ok": False,
                "replayed": False,
                "order": order.to_dict(),
                "fills": [],
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - a bug must not leave a live order
            self._last_error = f"{type(exc).__name__}: {exc}"
            order = await self._fail(order, f"{type(exc).__name__}: {exc}")
            return {
                "ok": False,
                "replayed": False,
                "order": order.to_dict(),
                "fills": [],
                "error": self._last_error,
            }
        if execute:
            order, fills = await self._process_locked(order)
        else:
            fills = ()
        self._latencies.append((self._now() - started).total_seconds() * 1000.0)
        return {
            "ok": order.status not in {
                OrderStatus.REJECTED,
                OrderStatus.FAILED,
                OrderStatus.CANCELLED,
                OrderStatus.EXPIRED,
            },
            "replayed": False,
            "order": order.to_dict(),
            "fills": [fill.to_dict() for fill in fills],
        }

    async def preflight(
        self,
        proposal_id: str,
        *,
        quantity: object | None = None,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: object | None = None,
        trigger_price: object | None = None,
        expires_at: datetime | str | None = None,
        actor: str = "execution-preflight",
    ) -> dict[str, Any]:
        """Validate a proposal through NM-10 without creating an order.

        The check is deliberately the engine's own ``_validate`` path: it
        re-runs NM-08, verifies the NM-02 snapshot, and checks simulated cash or
        position through the authoritative stores.  It may record an NM-08
        assessment, but it never writes an order, fill, reservation or ledger
        entry.  A caller must still submit; the preflight is a gate, not the
        authoritative order.
        """

        started = self._now()
        proposal_key = str(proposal_id or "").strip()
        try:
            raw_type = str(order_type).strip().lower().replace("-", "_")
            wanted_type = OrderType(raw_type)
            proposal = self._proposal(proposal_key)
            side = self._side(proposal)
            expiry = self._expiry(proposal, expires_at)
            candidate = Order(
                order_id=execution_id(
                    "pre", proposal_key, quantity if quantity is not None else "notional"
                ),
                proposal_id=proposal_key,
                strategy_id=str(proposal.get("strategy_id", "")),
                portfolio_id=str(proposal.get("portfolio_id", "")),
                instrument_id=str(proposal.get("instrument_id", "")),
                side=side,
                order_type=wanted_type,
                requested_quantity=Decimal("0"),
                filled_quantity=Decimal("0"),
                remaining_quantity=Decimal("0"),
                status=OrderStatus.CREATED,
                risk_decision_id=str(proposal.get("risk_decision_id", "")),
                execution_config_version=self._config.version,
                created_at=started,
                updated_at=started,
                expires_at=expiry,
                limit_price=money(limit_price) if limit_price is not None else None,
                trigger_price=(
                    money(trigger_price) if trigger_price is not None else None
                ),
                reference_price=(
                    money(proposal["reference_price"])
                    if proposal.get("reference_price") not in (None, "")
                    else None
                ),
                actor=str(actor or "execution-preflight"),
                reason=str(proposal.get("reason", "")),
            )
            validated, snapshot = await self._validate(candidate, proposal, quantity)
        except (ExecutionValidationError, ExecutionPricingError) as exc:
            return {
                "ok": False,
                "error_type": "validation",
                "error": str(exc),
                "order_created": False,
            }
        except Exception as exc:  # noqa: BLE001 - unavailable checks fail closed
            return {
                "ok": False,
                "error_type": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "order_created": False,
            }
        return {
            "ok": True,
            "error_type": "",
            "error": "",
            "order_created": False,
            "proposal": dict(proposal),
            "order": validated.to_dict(),
            "snapshot": snapshot.to_dict(),
            "latency_ms": round((self._now() - started).total_seconds() * 1000.0, 3),
        }

    def _proposal(self, proposal_id: str) -> dict[str, Any]:
        loaded: Mapping[str, Any] | None = None
        loader = self._proposal_loader
        if loader is not None:
            try:
                candidate = loader(proposal_id)
            except Exception:  # noqa: BLE001 - fall back to strategy storage
                candidate = None
            if isinstance(candidate, Mapping):
                loaded = candidate
        if loaded is None:
            try:
                proposal = self._strategies.proposal(proposal_id)
            except Exception as exc:  # noqa: BLE001
                raise ExecutionValidationError(
                    f"unknown or unreadable strategy proposal {proposal_id!r}: {exc}"
                ) from exc
        else:
            proposal = loaded
        if not isinstance(proposal, Mapping):
            raise ExecutionValidationError("strategy proposal is not an object")
        result = dict(proposal)
        if not result.get("proposal_id"):
            result["proposal_id"] = proposal_id
        return result

    @staticmethod
    def _side(proposal: Mapping[str, Any]) -> OrderSide:
        side = str(proposal.get("side", "")).strip().lower()
        action = str(proposal.get("action", "")).strip().lower()
        if side in {"buy", "sell"}:
            return OrderSide(side)
        if action in {"buy", "add_position"}:
            return OrderSide.BUY
        if action in {"sell", "exit", "reduce_position", "remove_position"}:
            return OrderSide.SELL
        raise ExecutionValidationError("proposal has no executable BUY or SELL side")

    def _expiry(self, proposal: Mapping[str, Any], requested: datetime | str | None) -> datetime:
        now = self._now()
        raw = requested if requested is not None else proposal.get("expires_at")
        if raw in (None, ""):
            return now + timedelta(seconds=self._config.default_order_ttl_seconds)
        try:
            expiry = parse_time(raw)
        except ValueError as exc:
            raise ExecutionValidationError(f"invalid order expiry: {exc}") from exc
        if expiry <= now:
            raise ExecutionValidationError("proposal approval has expired")
        return expiry

    async def _validate(
        self, order: Order, proposal: Mapping[str, Any], requested: object | None
    ) -> tuple[Order, MarketSnapshot]:
        now = self._now()
        status = str(proposal.get("status", "")).strip().lower()
        if status != "approved":
            raise ExecutionValidationError(
                f"proposal is {status or 'unknown'}, not approved for execution"
            )
        if str(proposal.get("risk_outcome", "")).strip().lower() != "approved":
            raise ExecutionValidationError("proposal has no approved NM-08 risk outcome")
        if str(proposal.get("risk_decision_id", "")).strip() == "":
            raise ExecutionValidationError("proposal has no NM-08 risk decision reference")
        if order.expires_at <= now:
            raise ExecutionValidationError("proposal approval has expired")
        portfolio = self._portfolio(order.portfolio_id)
        if str(getattr(portfolio, "status", "")).split(".")[-1].lower() != "active":
            raise ExecutionValidationError(
                f"portfolio {order.portfolio_id} is not active "
                f"({getattr(portfolio, 'status', 'unknown')})"
            )
        if order.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT} and order.limit_price is None:
            raise ExecutionValidationError(f"{order.order_type.value} order requires a limit_price")
        if order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT} and order.trigger_price is None:
            raise ExecutionValidationError(f"{order.order_type.value} order requires a trigger_price")
        if order.order_type is OrderType.MARKET and order.limit_price is not None:
            raise ExecutionValidationError("a market order must not carry a limit_price")

        instrument = self._instrument(order.instrument_id)
        asset_class = getattr(instrument, "asset_class", "")
        asset_value = getattr(asset_class, "value", str(asset_class)).lower()
        if "crypto" in asset_value:
            raise ExecutionValidationError("crypto execution is outside NM-10 scope")
        snapshot = self._snapshot(order.instrument_id)
        if snapshot.reference_price <= 0:
            raise ExecutionValidationError("market reference price must be positive")
        decision = self._risk_decision(proposal, order)
        original_restrictions = tuple(decision.get("restrictions", ()) or ())
        if original_restrictions:
            raise ExecutionValidationError(
                "risk decision carries restrictions that this execution model cannot enforce: "
                + "; ".join(str(item) for item in original_restrictions)
            )
        qty = self._requested_quantity(order, proposal, requested, snapshot)
        if qty <= 0:
            raise ExecutionValidationError("requested quantity must be positive")
        actual_notional = money(qty * snapshot.reference_price)
        await self._revalidate_risk(order, proposal, decision, snapshot, actual_notional)
        if order.side is OrderSide.SELL:
            position = self._position(order.portfolio_id, order.instrument_id)
            available = (position.quantity if position is not None else Decimal("0")) - self._store.reserved_total(
                order.portfolio_id, "quantity"
            )
            if available < qty:
                raise ExecutionValidationError(
                    f"insufficient position quantity: requested {text(qty)}, available {text(max(available, Decimal('0')))}"
                )
        else:
            reservation_price = self._reservation_price(order, snapshot)
            notional = money(qty * reservation_price)
            fees = fee_breakdown(notional, order.side, self._config)
            buffer = money(notional * self._config.maximum_slippage_bps / Decimal("10000"))
            required = money(notional + fees["total"] + buffer)
            free = self._available_cash(order.portfolio_id)
            if required > free:
                raise ExecutionValidationError(
                    f"insufficient simulated cash: required {text(required)}, available {text(free)}"
                )
        updated = replace(
            order,
            requested_quantity=qty,
            remaining_quantity=qty,
            reference_price=snapshot.reference_price,
            market_reference=snapshot.to_dict(),
            risk_decision_id=str(proposal.get("risk_decision_id", "")),
            updated_at=now,
        )
        return updated, snapshot

    def _requested_quantity(
        self,
        order: Order,
        proposal: Mapping[str, Any],
        requested: object | None,
        snapshot: MarketSnapshot,
    ) -> Decimal:
        if requested is not None:
            try:
                qty = quantity(requested)
            except Exception as exc:  # noqa: BLE001
                raise ExecutionValidationError(f"invalid requested quantity: {exc}") from exc
            return qty
        reference = snapshot.reference_price
        raw_notional = proposal.get("notional")
        if raw_notional in (None, ""):
            raise ExecutionValidationError("proposal has no notional and no quantity was supplied")
        try:
            notional = Decimal(str(raw_notional))
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError("proposal notional is not decimal") from exc
        if notional <= 0 or reference <= 0:
            raise ExecutionValidationError("proposal notional and market price must be positive")
        qty = quantity(notional / reference)
        if qty <= 0:
            raise ExecutionValidationError("proposal notional is too small for one share")
        return qty

    async def _revalidate_risk(
        self,
        order: Order,
        proposal: Mapping[str, Any],
        decision: Mapping[str, Any],
        snapshot: MarketSnapshot,
        actual_notional: Decimal,
    ) -> None:
        proposal_decision = str(proposal.get("risk_decision_id", ""))
        if str(decision.get("decision_id", "")) != proposal_decision:
            raise ExecutionValidationError("risk decision reference does not match the stored decision")
        outcome = str(decision.get("outcome", "")).strip().lower()
        if outcome != "approved":
            raise ExecutionValidationError(f"risk decision is {outcome or 'not approved'}")
        if decision.get("proposal_id") not in (None, "", order.proposal_id):
            raise ExecutionValidationError("risk decision belongs to another proposal")
        expected_action = _RISK_ACTIONS.get(str(proposal.get("action", "")).lower())
        if expected_action and str(decision.get("action", "")).lower() != expected_action:
            raise ExecutionValidationError("risk decision action does not match the proposal")
        expires = decision.get("expires_at")
        if expires not in (None, ""):
            try:
                if parse_time(expires) <= self._now():
                    raise ExecutionValidationError("risk decision has expired")
            except ExecutionValidationError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ExecutionValidationError(f"risk decision expiry is invalid: {exc}") from exc
        proposal_state = str(proposal.get("risk_state_version", ""))
        decision_state = str(decision.get("state_version", ""))
        if proposal_state and decision_state and proposal_state != decision_state:
            raise ExecutionValidationError(
                "proposal was approved against a different portfolio risk state"
            )
        assess = getattr(self._risk, "assess", None)
        if assess is None:
            return
        action = expected_action or _RISK_ACTIONS.get(str(proposal.get("side", "")).lower(), "apply_proposal")
        try:
            revalidated = await assess(
                action,
                portfolio_id=order.portfolio_id,
                proposal_id=order.proposal_id,
                instrument_id=order.instrument_id,
                side=order.side.value,
                notional=actual_notional,
                actor=order.actor or "paper-execution",
            )
        except Exception as exc:  # noqa: BLE001 - unavailable risk fails closed
            raise ExecutionValidationError(
                f"independent risk revalidation is unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(revalidated, Mapping):
            raise ExecutionValidationError("risk revalidation returned no decision")
        if str(revalidated.get("outcome", "")).strip().lower() != "approved":
            raise ExecutionValidationError(
                f"risk revalidation refused the order: {revalidated.get('outcome', 'unknown')}"
            )
        restrictions = tuple(revalidated.get("restrictions", ()) or ())
        if restrictions:
            raise ExecutionValidationError(
                "risk revalidation returned restrictions that this execution model "
                "cannot enforce: " + "; ".join(str(item) for item in restrictions)
            )
        revalidated_state = str(revalidated.get("state_version", ""))
        if decision_state and revalidated_state and decision_state != revalidated_state:
            raise ExecutionValidationError("portfolio state changed after risk approval")

    def _risk_decision(self, proposal: Mapping[str, Any], order: Order) -> dict[str, Any]:
        decision_id = str(proposal.get("risk_decision_id", ""))
        store = getattr(self._risk, "store", None)
        if store is None or not hasattr(store, "decision"):
            raise ExecutionValidationError("NM-08 risk decision store is unavailable")
        try:
            decision = store.decision(decision_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError(f"risk decision could not be read: {exc}") from exc
        if not isinstance(decision, Mapping):
            raise ExecutionValidationError(f"unknown risk decision {decision_id!r}")
        if str(decision.get("portfolio_id", "")) not in ("", order.portfolio_id):
            raise ExecutionValidationError("risk decision belongs to another portfolio")
        return dict(decision)

    def _instrument(self, instrument_id: str) -> Any:
        if self._market_data is None:
            raise ExecutionValidationError("NM-02 market data is unavailable")
        try:
            return self._market_data.get_instrument(instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError(f"unknown instrument {instrument_id!r}: {exc}") from exc

    def _snapshot(self, instrument_id: str) -> MarketSnapshot:
        try:
            return snapshot_for(
                self._market_data, instrument_id, now=self._now(), config=self._config
            )
        except ExecutionPricingError as exc:
            raise ExecutionValidationError(str(exc)) from exc

    def _portfolio(self, portfolio_id: str) -> Any:
        try:
            detail = self._portfolios.detail(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError(f"unknown portfolio {portfolio_id!r}: {exc}") from exc
        record = detail.get("portfolio") if isinstance(detail, Mapping) else None
        if record is None:
            raise ExecutionValidationError(f"unknown portfolio {portfolio_id!r}")
        return SimpleNamespace(**record) if isinstance(record, Mapping) else record

    def _portfolio_record(self, portfolio_id: str) -> Any:
        try:
            record = self._portfolios.store.portfolio(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError(f"portfolio record is unavailable: {exc}") from exc
        if record is None:
            raise ExecutionValidationError(f"unknown portfolio {portfolio_id!r}")
        return record

    def _position(self, portfolio_id: str, instrument_id: str) -> Any | None:
        try:
            return self._portfolios.store.position(portfolio_id, instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError(f"positions are unavailable: {exc}") from exc

    def _wallet_balance(self, portfolio_id: str) -> Decimal:
        record = self._portfolio_record(portfolio_id)
        try:
            body = self._wallets.wallet(record.wallet_id)
            return money(body["wallet"]["balance"]["total"])
        except Exception as exc:  # noqa: BLE001
            raise ExecutionValidationError(f"wallet is unavailable: {exc}") from exc

    def _available_cash(self, portfolio_id: str) -> Decimal:
        return max(Decimal("0.00"), self._wallet_balance(portfolio_id) - self._store.reserved_total(portfolio_id, "cash"))

    @staticmethod
    def _reservation_price(order: Order, snapshot: MarketSnapshot) -> Decimal:
        """Reserve a BUY at the highest price the order may pay.

        A buy limit can execute anywhere up to its limit, so reserving only the
        current reference price would make a valid fill fail when the market
        rose toward the limit between acceptance and processing.
        """
        if (
            order.side is OrderSide.BUY
            and order.limit_price is not None
            and order.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}
        ):
            return max(snapshot.reference_price, order.limit_price)
        return snapshot.reference_price

    async def _reserve(self, order: Order, snapshot: MarketSnapshot) -> None:
        now = self._now()
        if order.side is OrderSide.SELL:
            amount = order.requested_quantity
            kind = "quantity"
        else:
            notional = money(order.requested_quantity * self._reservation_price(order, snapshot))
            fees = fee_breakdown(notional, order.side, self._config)
            buffer = money(notional * self._config.maximum_slippage_bps / Decimal("10000"))
            amount = money(notional + fees["total"] + buffer)
            kind = "cash"
            if amount > self._available_cash(order.portfolio_id):
                raise ExecutionValidationError("simulated cash reservation would exceed available cash")
        existing = self._store.reservations(order_id=order.order_id, kind=kind, status="active")
        if existing:
            raise ExecutionError("duplicate active reservation for order")
        self._store.save_reservation(
            Reservation(
                reservation_id=execution_id("resv", order.order_id, kind),
                order_id=order.order_id,
                portfolio_id=order.portfolio_id,
                kind=kind,
                amount=amount,
                remaining=amount,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )

    # -- processing ----------------------------------------------------------
    async def process(self, order_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._recover_inflight_locked()
            order = self._store.order(order_id)
            if order is None:
                raise ExecutionValidationError(f"unknown paper order {order_id!r}")
            order, fills = await self._process_locked(order)
            return {
                "ok": order.status not in {OrderStatus.FAILED, OrderStatus.REJECTED},
                "order": order.to_dict(),
                "fills": [fill.to_dict() for fill in fills],
            }

    async def _process_locked(self, order: Order) -> tuple[Order, tuple[Fill, ...]]:
        if order.status in TERMINAL_STATUSES or order.status is OrderStatus.CANCEL_PENDING:
            return order, ()
        now = self._now()
        if now >= order.expires_at:
            return await self._expire(order, "order approval expired"), ()
        if order.status is OrderStatus.ACCEPTED:
            order = self._store.transition(order, OrderStatus.OPEN, reason="processing accepted order")
            await self._status_event(order, "processing accepted order")
        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            return order, ()
        try:
            snapshot = self._snapshot(order.instrument_id)
        except ExecutionValidationError as exc:
            self._last_error = str(exc)
            order = replace(order, last_error=str(exc), updated_at=now)
            self._store.save_order(order)
            return order, ()
        order = self._trigger_satisfied(order, snapshot)
        if (
            order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
            and order.triggered_at is None
        ):
            return order, ()
        if not self._limit_satisfied(order, snapshot):
            return order, ()
        available = snapshot.available_quantity
        if available <= 0:
            order = replace(order, last_error="no simulated liquidity available", updated_at=now)
            self._store.save_order(order)
            return order, ()
        fill_qty = min(order.remaining_quantity, available)
        if order.side is OrderSide.SELL:
            position = self._position(order.portfolio_id, order.instrument_id)
            position_qty = position.quantity if position is not None else Decimal("0")
            fill_qty = min(fill_qty, position_qty)
        else:
            fill_qty = min(fill_qty, self._affordable_quantity(order, snapshot))
        if fill_qty <= 0:
            order = replace(order, last_error="no executable quantity after portfolio checks", updated_at=now)
            self._store.save_order(order)
            return order, ()
        try:
            fill = await self._fill(order, snapshot, fill_qty)
            order = self._store.order(order.order_id) or order
            return order, (fill,)
        except Exception as exc:  # noqa: BLE001 - fail closed, keep previous fills
            self._last_error = f"{type(exc).__name__}: {exc}"
            # Compensation may already have failed closed.  Re-read the order
            # before transitioning; retrying _fail with a stale status raises a
            # spurious concurrent-update error and hides the real failure.
            current = self._store.order(order.order_id) or order
            order = (
                current
                if current.status in TERMINAL_STATUSES
                else await self._fail(current, self._last_error)
            )
            return order, ()

    def _trigger_satisfied(self, order: Order, snapshot: MarketSnapshot) -> Order:
        if order.order_type not in {OrderType.STOP, OrderType.STOP_LIMIT}:
            return order
        if order.triggered_at is not None:
            return order
        trigger = order.trigger_price
        if trigger is None:
            return order
        if order.side is OrderSide.BUY:
            hit = snapshot.reference_price >= trigger
        else:
            hit = snapshot.reference_price <= trigger
        if not hit:
            return order
        updated = replace(order, triggered_at=self._now(), updated_at=self._now())
        self._store.save_order(updated)
        return updated

    def _limit_satisfied(self, order: Order, snapshot: MarketSnapshot) -> bool:
        if order.order_type not in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
            return True
        if order.limit_price is None:
            return False
        if order.side is OrderSide.BUY:
            return snapshot.reference_price <= order.limit_price
        return snapshot.reference_price >= order.limit_price

    def _affordable_quantity(self, order: Order, snapshot: MarketSnapshot) -> Decimal:
        reservations = self._store.reservations(order_id=order.order_id, kind="cash", status="active")
        reserved = sum((item.remaining for item in reservations), Decimal("0"))
        if reserved <= 0:
            return Decimal("0")
        unit = snapshot.reference_price * (Decimal("1") + self._config.maximum_slippage_bps / Decimal("10000"))
        if unit <= 0:
            return Decimal("0")
        candidate = order.remaining_quantity
        # The reservation includes an estimate of fees.  A price that moved up
        # between acceptance and execution can make that estimate short; reduce
        # the fill until its modelled fee-inclusive cost fits rather than writing
        # a fill that would overdraw the reserved cash.
        while candidate > 0:
            try:
                modelled, _, _ = execution_price(
                    snapshot,
                    side=order.side,
                    order_type=order.order_type,
                    quantity=candidate,
                    config=self._config,
                    limit_price=order.limit_price,
                )
            except ExecutionPricingError:
                modelled = snapshot.reference_price
            gross = money(candidate * modelled)
            fees = fee_breakdown(gross, order.side, self._config)
            if money(gross + fees["total"]) <= reserved:
                break
            candidate = quantity(candidate - SHARE)
        return max(Decimal("0"), min(order.remaining_quantity, candidate))

    # -- fills ---------------------------------------------------------------
    async def _fill(self, order: Order, snapshot: MarketSnapshot, fill_qty: Decimal) -> Fill:
        sequence = self._store.fill_count(order.order_id) + 1
        fill_id = execution_id("fill", order.order_id, sequence)
        price, slip_per_unit, slip_pct = execution_price(
            snapshot,
            side=order.side,
            order_type=order.order_type,
            quantity=fill_qty,
            config=self._config,
            limit_price=order.limit_price,
        )
        gross = money(fill_qty * price)
        fees = fee_breakdown(gross, order.side, self._config)
        total_fees = fees["total"]
        net_cash = money(gross + total_fees) if order.side is OrderSide.BUY else money(gross - total_fees)
        prepared = Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            sequence=sequence,
            quantity=fill_qty,
            price=price,
            reference_price=snapshot.reference_price,
            slippage_amount=money(slip_per_unit * fill_qty),
            slippage_pct=slip_pct,
            slippage_model_version=self._config.version,
            fee_breakdown=fees,
            total_fees=total_fees,
            gross_value=gross,
            net_cash=net_cash,
            status="recorded",
            market_reference=snapshot.to_dict(),
            executed_at=self._now(),
            ledger_transaction_ids=(),
            position_effect={},
        )
        # The journal is durable before the first external write.  If the
        # process dies in any later window, recovery can identify the exact
        # wallet transfers and position effect that belong to this fill.
        self._store.save_inflight(prepared)
        try:
            return await self._fill_body(order, snapshot, fill_qty)
        except Exception as exc:  # noqa: BLE001 - compensate before failing closed
            reason = f"paper fill failed: {type(exc).__name__}: {exc}"
            recovered = await self._compensate_fill(order, fill_id, reason)
            if not recovered:
                raise ExecutionError(
                    f"{reason}; compensation also failed: {self._last_error}"
                ) from exc
            raise

    async def _fill_body(self, order: Order, snapshot: MarketSnapshot, fill_qty: Decimal) -> Fill:
        sequence = self._store.fill_count(order.order_id) + 1
        fill_id = execution_id("fill", order.order_id, sequence)
        price, slip_per_unit, slip_pct = execution_price(
            snapshot,
            side=order.side,
            order_type=order.order_type,
            quantity=fill_qty,
            config=self._config,
            limit_price=order.limit_price,
        )
        if order.side is OrderSide.BUY and order.limit_price is not None and price > order.limit_price:
            raise ExecutionError("limit price would be violated")
        if order.side is OrderSide.SELL and order.limit_price is not None and price < order.limit_price:
            raise ExecutionError("limit price would be violated")
        gross = money(fill_qty * price)
        fees = fee_breakdown(gross, order.side, self._config)
        total_fees = fees["total"]
        net_cash = money(gross + total_fees) if order.side is OrderSide.BUY else money(gross - total_fees)
        if order.side is OrderSide.BUY:
            reserved = self._store.reservations(order_id=order.order_id, kind="cash", status="active")
            if sum((item.remaining for item in reserved), Decimal("0")) < net_cash:
                raise ExecutionError("cash reservation is insufficient for the modelled fill")
        else:
            reserved = self._store.reservations(order_id=order.order_id, kind="quantity", status="active")
            if sum((item.remaining for item in reserved), Decimal("0")) < fill_qty:
                raise ExecutionError("quantity reservation is insufficient for the fill")
        record = self._portfolio_record(order.portfolio_id)
        await self._ensure_clearing_wallet()
        transaction_ids: list[str] = []
        if order.side is OrderSide.BUY:
            gross_result = await self._wallets.transfer(
                record.wallet_id,
                self._clearing_wallet_id,
                gross,
                idempotency_key=f"{fill_id}:gross",
                reference=fill_id,
                description=f"NM-10 paper buy {order.instrument_id}",
            )
            transaction_ids.append(str(gross_result["transaction"]["transaction_id"]))
            if total_fees > 0:
                fee_result = await self._wallets.transfer(
                    record.wallet_id,
                    self._clearing_wallet_id,
                    total_fees,
                    idempotency_key=f"{fill_id}:fees",
                    reference=fill_id,
                    description=f"NM-10 paper fees {order.instrument_id}",
                )
                transaction_ids.append(str(fee_result["transaction"]["transaction_id"]))
        else:
            gross_result = await self._wallets.transfer(
                self._clearing_wallet_id,
                record.wallet_id,
                gross,
                idempotency_key=f"{fill_id}:gross",
                reference=fill_id,
                description=f"NM-10 paper sell {order.instrument_id}",
            )
            transaction_ids.append(str(gross_result["transaction"]["transaction_id"]))
            if total_fees > 0:
                fee_result = await self._wallets.transfer(
                    record.wallet_id,
                    self._clearing_wallet_id,
                    total_fees,
                    idempotency_key=f"{fill_id}:fees",
                    reference=fill_id,
                    description=f"NM-10 paper fees {order.instrument_id}",
                )
                transaction_ids.append(str(fee_result["transaction"]["transaction_id"]))
        position_effect = self._apply_position(
            order, fill_qty, price, net_cash, total_fees, snapshot, fill_id
        )
        fill = Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            sequence=sequence,
            quantity=fill_qty,
            price=price,
            reference_price=snapshot.reference_price,
            slippage_amount=money(slip_per_unit * fill_qty),
            slippage_pct=slip_pct,
            slippage_model_version=self._config.version,
            fee_breakdown=fees,
            total_fees=total_fees,
            gross_value=gross,
            net_cash=net_cash,
            status="recorded",
            market_reference=snapshot.to_dict(),
            executed_at=self._now(),
            ledger_transaction_ids=tuple(transaction_ids),
            position_effect=position_effect,
        )
        filled = quantity(order.filled_quantity + fill_qty)
        remaining = quantity(order.requested_quantity - filled)
        target = OrderStatus.FILLED if remaining <= 0 else OrderStatus.PARTIALLY_FILLED
        updated = replace(
            order,
            filled_quantity=filled,
            remaining_quantity=remaining,
            updated_at=self._now(),
            last_error="",
        )
        reservations = self._consume_reservations(
            order,
            fill_qty,
            net_cash,
            release_remainder=target is OrderStatus.FILLED,
        )
        order = self._store.record_fill(
            fill,
            updated,
            target,
            reason="paper fill recorded",
            reservations=reservations,
        )
        await self._emit(
            "novamarket.execution.fill.recorded",
            {
                "fill_id": fill.fill_id,
                "order_id": fill.order_id,
                "instrument_id": fill.instrument_id,
                "quantity": text(fill.quantity),
                "price": text(fill.price),
                "slippage_pct": text(fill.slippage_pct),
                "total_fees": text(fill.total_fees),
                "ledger_transaction_ids": list(fill.ledger_transaction_ids),
                "execution_config_version": self._config.version,
            },
        )
        await self._status_event(order, "fill recorded")
        return fill

    async def _compensate_fill(self, order: Order, fill_id: str, reason: str) -> bool:
        """Undo external effects for a pending fill, or finalise a committed one.

        Compensation is idempotent: wallet reversals use deterministic keys and
        position reversal is recorded against the fill id.  A failure here keeps
        the journal row pending so startup recovery can retry it.
        """
        entry = self._store.inflight(fill_id)
        if entry is None or entry["status"] != "pending":
            return True
        existing = self._store.fill(fill_id)
        if existing is not None:
            self._store.settle_inflight(fill_id, "committed")
            return True
        fill = entry["fill"]
        try:
            if self._position_effect_applied(fill):
                self._reverse_position_effect(fill)
            for transaction_id in self._inflight_ledger_ids(fill):
                transaction = self._wallets.store.transaction(transaction_id)
                if transaction.reversed:
                    continue
                await self._wallets.reverse(
                    transaction_id,
                    reason=reason,
                    idempotency_key=f"{transaction_id}:compensation",
                )
            self._store.settle_inflight(fill_id, "rolled_back")
        except Exception as exc:  # noqa: BLE001 - retain the journal for retry
            self._last_error = f"compensation failed for {fill_id}: {type(exc).__name__}: {exc}"
            _LOGGER.exception("novamarket.execution.compensation_failed fill=%s", fill_id)
            return False
        current = self._store.order(order.order_id)
        if current is not None and current.status not in TERMINAL_STATUSES:
            await self._fail(current, reason)
        return True

    def _position_effect_applied(self, fill: Fill) -> bool:
        portfolio_id = self._order_portfolio(fill.order_id)
        if not portfolio_id:
            return bool(fill.position_effect)
        try:
            rows = self._portfolios.store.position_history(
                portfolio_id,
                instrument_id=fill.instrument_id,
                limit=500,
            )
        except Exception:
            return bool(fill.position_effect)
        return any(
            str(row.get("reference") or "") == fill.fill_id
            and str(row.get("kind") or "") in {"paper_buy", "paper_sell"}
            for row in rows
        )

    def _order_portfolio(self, order_id: str) -> str:
        order = self._store.order(order_id)
        return order.portfolio_id if order is not None else ""

    def _inflight_ledger_ids(self, fill: Fill) -> tuple[str, ...]:
        found = list(fill.ledger_transaction_ids)
        store = getattr(self._wallets, "store", None)
        for key in (f"{fill.fill_id}:gross", f"{fill.fill_id}:fees"):
            if store is None or not hasattr(store, "transaction_for_key"):
                continue
            transaction = store.transaction_for_key(key)
            if transaction is not None and transaction.transaction_id not in found:
                found.append(transaction.transaction_id)
        return tuple(found)

    def _reverse_position_effect(self, fill: Fill) -> None:
        store = self._portfolios.store
        order = self._store.order(fill.order_id)
        if order is None:
            raise ExecutionError(f"cannot reverse position for missing order {fill.order_id}")
        current = store.position(order.portfolio_id, order.instrument_id)
        effect = dict(fill.position_effect or {})
        before = effect.get("position_before")
        realized_delta = Decimal(str(effect.get("realized_delta", "0")))
        now = self._now()
        if before:
            before_qty = Decimal(str(before.get("quantity", "0")))
            before_avg = Decimal(str(before.get("average_cost", "0")))
            before_realized = Decimal(str(before.get("realized_pnl", "0")))
            opened_at = parse_time(before.get("opened_at")) if before.get("opened_at") else now
            last_price = Decimal(str(before["last_price"])) if before.get("last_price") else None
            priced_at = parse_time(before["priced_at"]) if before.get("priced_at") else None
            sector = str(before.get("sector") or "")
            asset_class = str(before.get("asset_class") or "")
        else:
            if current is None:
                return
            before_qty = (
                current.quantity - fill.quantity
                if order.side is OrderSide.BUY
                else current.quantity + fill.quantity
            )
            if before_qty < 0:
                raise ExecutionError("position is inconsistent with the fill being reversed")
            if order.side is OrderSide.BUY:
                before_cost = current.average_cost * current.quantity - fill.net_cash
                before_avg = money(before_cost / before_qty) if before_qty > 0 else Decimal("0.00")
                before_realized = current.realized_pnl
            else:
                before_avg = current.average_cost
                before_realized = money(current.realized_pnl - realized_delta)
            opened_at = current.opened_at
            last_price = current.last_price
            priced_at = current.priced_at
            sector = current.sector
            asset_class = current.asset_class
        history = store.position_history(
            order.portfolio_id, instrument_id=order.instrument_id, limit=500
        )
        reversal_kind = "paper_buy_reversal" if order.side is OrderSide.BUY else "paper_sell_reversal"
        already_reversed = any(
            str(row.get("reference") or "") == fill.fill_id
            and str(row.get("kind") or "") == reversal_kind
            for row in history
        )
        if already_reversed:
            return
        position = Position(
            portfolio_id=order.portfolio_id,
            instrument_id=order.instrument_id,
            quantity=quantity(before_qty),
            average_cost=money(before_avg),
            realized_pnl=money(before_realized),
            sector=sector,
            asset_class=asset_class,
            opened_at=opened_at,
            updated_at=now,
            last_price=last_price,
            priced_at=priced_at,
            closed_at=now if before_qty == 0 else None,
        )
        with store.transaction():
            store.upsert_position(position)
            store.record_position_history(
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                kind=reversal_kind,
                quantity=fill.quantity,
                price=fill.price,
                notional=fill.gross_value,
                realized=money(-realized_delta),
                at=now,
                reference=fill.fill_id,
            )
            store.touch(
                order.portfolio_id,
                at=now,
                rebalanced=False,
                realized_delta=money(-realized_delta),
            )

    async def recover_pending(self) -> dict[str, Any]:
        """Finish or compensate every pending fill journal row after a restart."""
        async with self._lock:
            return await self._recover_inflight_locked()

    async def _recover_inflight_locked(self) -> dict[str, Any]:
        recovered: list[dict[str, Any]] = []
        for entry in self._store.pending_inflight():
            fill = entry["fill"]
            order = self._store.order(fill.order_id)
            if order is None:
                recovered.append({"fill_id": fill.fill_id, "ok": False, "reason": "order missing"})
                continue
            ok = await self._compensate_fill(
                order, fill.fill_id, "recovered a pending paper fill after restart"
            )
            recovered.append({"fill_id": fill.fill_id, "ok": ok})
        return {"ok": all(item["ok"] for item in recovered), "recovered": recovered}

    async def _ensure_clearing_wallet(self) -> str:
        try:
            self._wallets.wallet(self._clearing_wallet_id)
            return self._clearing_wallet_id
        except Exception:
            pass
        try:
            await self._wallets.create_wallet(
                "novamarket", label="Paper execution clearing", wallet_id=self._clearing_wallet_id
            )
        except Exception as exc:  # noqa: BLE001 - a concurrent creator is fine, verify below
            try:
                self._wallets.wallet(self._clearing_wallet_id)
            except Exception as verify_exc:
                raise ExecutionError(
                    f"paper clearing wallet is unavailable: {type(exc).__name__}: {exc}"
                ) from verify_exc
        await self._ensure_clearing_liquidity()
        return self._clearing_wallet_id

    async def _ensure_clearing_liquidity(self) -> None:
        """Seed the paper market counterparty once, never a portfolio wallet.

        This is simulated issuance, not a real-money movement and not a direct
        write to portfolio cash, positions, orders or fills.  The ledger remains
        balanced because the deposit debits the issuance account.
        """

        try:
            wallet = self._wallets.wallet(self._clearing_wallet_id)["wallet"]
            balance_value = wallet.get("balance")
            if isinstance(balance_value, Mapping):
                balance_value = balance_value.get("available") or "0"
            balance = Decimal(str(balance_value or "0"))
        except Exception as exc:  # noqa: BLE001 - clearing provisioning reports the cause
            raise ExecutionError(
                f"paper clearing wallet cannot be read: {type(exc).__name__}: {exc}"
            ) from exc
        if balance >= self._clearing_liquidity:
            return
        amount = self._clearing_liquidity - balance
        try:
            await self._wallets.deposit(
                self._clearing_wallet_id,
                amount,
                idempotency_key=f"nm10:clearing:liquidity:v1:{balance}",
                reference="NM-10 paper market-maker liquidity",
                description="Paper clearing liquidity",
            )
        except Exception as exc:  # noqa: BLE001 - settlement must fail closed
            raise ExecutionError(
                f"paper clearing liquidity is unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    def _apply_position(
        self,
        order: Order,
        fill_qty: Decimal,
        price: Decimal,
        net_cash: Decimal,
        total_fees: Decimal,
        snapshot: MarketSnapshot,
        fill_id: str,
    ) -> dict[str, Any]:
        store = self._portfolios.store
        existing = store.position(order.portfolio_id, order.instrument_id)
        now = self._now()
        instrument = self._instrument(order.instrument_id)
        sector = str(getattr(instrument, "metadata", {}).get("sector", "") or "")
        asset_class = str(getattr(getattr(instrument, "asset_class", ""), "value", "") or "")
        if order.side is OrderSide.BUY:
            before = existing.quantity if existing is not None else Decimal("0")
            before_cost = existing.cost_basis if existing is not None else Decimal("0.00")
            after = quantity(before + fill_qty)
            # Acquisition cost includes simulated fees: the position's cost basis
            # is what the portfolio actually paid, not just the quoted gross.
            average = money((before_cost + net_cash) / after) if after else price
            realized_delta = Decimal("0.00")
            position = Position(
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                quantity=after,
                average_cost=average,
                realized_pnl=existing.realized_pnl if existing is not None else Decimal("0.00"),
                sector=existing.sector if existing is not None else sector,
                asset_class=existing.asset_class if existing is not None else asset_class,
                opened_at=existing.opened_at if existing is not None else now,
                updated_at=now,
                last_price=price,
                priced_at=now,
                closed_at=None,
            )
            kind = "paper_buy"
        else:
            if existing is None or existing.quantity < fill_qty:
                raise ExecutionError("position no longer covers the reserved sell quantity")
            before = existing.quantity
            after = quantity(before - fill_qty)
            realized_delta = money((price - existing.average_cost) * fill_qty - total_fees)
            position = Position(
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                quantity=after,
                average_cost=existing.average_cost if after else Decimal("0.00"),
                realized_pnl=money(existing.realized_pnl + realized_delta),
                sector=existing.sector,
                asset_class=existing.asset_class,
                opened_at=existing.opened_at,
                updated_at=now,
                last_price=price,
                priced_at=now,
                closed_at=now if after == 0 else None,
            )
            kind = "paper_sell"
        position_before = existing.to_dict() if existing is not None else None
        with store.transaction():
            store.upsert_position(position)
            store.record_position_history(
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                kind=kind,
                quantity=fill_qty,
                price=price,
                notional=money(fill_qty * price),
                realized=realized_delta,
                at=now,
                reference=fill_id,
            )
            store.touch(
                order.portfolio_id,
                at=now,
                rebalanced=False,
                realized_delta=realized_delta,
            )
        return {
            "kind": kind,
            "position_before": position_before,
            "quantity_before": text(before),
            "quantity_after": text(after),
            "average_cost_after": text(position.average_cost),
            "realized_delta": text(realized_delta),
            "net_cash": text(net_cash),
            "fees": text(total_fees),
            "reference_price": text(snapshot.reference_price),
        }

    def _consume_reservations(
        self,
        order: Order,
        fill_qty: Decimal,
        net_cash: Decimal,
        *,
        release_remainder: bool = False,
    ) -> tuple[Reservation, ...]:
        now = self._now()
        updated: list[Reservation] = []
        for reservation in self._store.reservations(order_id=order.order_id, status="active"):
            used = net_cash if reservation.kind == "cash" else fill_qty
            remaining = reservation.remaining - used
            if remaining < 0:
                raise ExecutionError(f"{reservation.kind} reservation was over-consumed")
            if release_remainder:
                remaining = Decimal("0")
            updated.append(
                replace(
                    reservation,
                    remaining=money(remaining) if reservation.kind == "cash" else quantity(remaining),
                    status="active" if remaining > 0 else ("released" if release_remainder else "consumed"),
                    updated_at=now,
                )
            )
        return tuple(updated)

    # -- cancellation / expiry / failure -------------------------------------
    async def cancel(self, order_id: str, *, reason: str = "", actor: str = "") -> dict[str, Any]:
        async with self._lock:
            await self._recover_inflight_locked()
            order = self._store.order(order_id)
            if order is None:
                raise ExecutionValidationError(f"unknown paper order {order_id!r}")
            if order.status is OrderStatus.FILLED:
                raise ExecutionError("a filled order cannot be cancelled")
            if order.status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.FAILED}:
                return {"ok": True, "order": order.to_dict(), "fills": [f.to_dict() for f in self._store.fills(order_id)]}
            if order.status is not OrderStatus.CANCEL_PENDING:
                order = self._store.transition(order, OrderStatus.CANCEL_PENDING, reason=reason or "cancel requested")
                await self._status_event(order, reason or "cancel requested")
            self._release_reservations(order, "released by cancellation")
            order = self._store.transition(order, OrderStatus.CANCELLED, reason=reason or "cancelled")
            await self._status_event(order, reason or "cancelled")
            return {
                "ok": True,
                "order": order.to_dict(),
                "fills": [f.to_dict() for f in self._store.fills(order_id)],
            }

    async def _expire(self, order: Order, reason: str) -> Order:
        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.ACCEPTED}:
            return order
        self._release_reservations(order, "released by expiry")
        order = self._store.transition(order, OrderStatus.EXPIRED, reason=reason)
        await self._status_event(order, reason)
        return order

    async def _reject(self, order: Order, reason: str) -> Order:
        if order.status in TERMINAL_STATUSES:
            return order
        self._release_reservations(order, "released by rejection")
        order = self._store.transition(order, OrderStatus.REJECTED, reason=reason)
        await self._status_event(order, reason)
        return order

    async def _fail(self, order: Order, reason: str) -> Order:
        if order.status in TERMINAL_STATUSES:
            return order
        self._release_reservations(order, "released by failure")
        order = self._store.transition(order, OrderStatus.FAILED, reason=reason)
        await self._status_event(order, reason)
        return order

    def _release_reservations(self, order: Order, reason: str) -> None:
        now = self._now()
        for reservation in self._store.reservations(order_id=order.order_id, status="active"):
            self._store.save_reservation(
                replace(reservation, remaining=Decimal("0.00"), status="released", updated_at=now)
            )

    # -- reads ---------------------------------------------------------------
    def order(self, order_id: str) -> dict[str, Any]:
        order = self._store.order(order_id)
        if order is None:
            raise ExecutionValidationError(f"unknown paper order {order_id!r}")
        return {
            "ok": True,
            "order": order.to_dict(),
            "transitions": [dict(row) for row in self._store.transitions(order_id)],
            "fills": [fill.to_dict() for fill in self._store.fills(order_id)],
            "reservations": [row.to_dict() for row in self._store.reservations(order_id=order_id)],
        }

    def orders(self, *, status: str = "", portfolio_id: str = "", limit: int = 100) -> dict[str, Any]:
        rows = self._store.orders(status=status, portfolio_id=portfolio_id, limit=limit)
        return {
            "ok": True,
            "orders": [row.to_dict() for row in rows],
            "total": len(rows),
            "limit": max(1, min(int(limit), 1000)),
            "paper_only": True,
        }

    def fills(self, order_id: str = "") -> dict[str, Any]:
        rows = self._store.fills(order_id or None)
        return {"ok": True, "fills": [row.to_dict() for row in rows], "total": len(rows)}

    def fees(self) -> dict[str, Any]:
        return {
            "ok": True,
            "config_version": self._config.version,
            "effective_date": self._config.fee_effective_date,
            "currency": "INR",
            "components_bps": {
                "brokerage": text(self._config.brokerage_bps),
                "exchange_transaction": text(self._config.exchange_bps),
                "sebi_turnover": text(self._config.sebi_bps),
                "stamp_duty_buy": text(self._config.stamp_duty_bps),
                "gst_percent": text(self._config.gst_percent),
            },
            "minimum_total_fee": text(self._config.minimum_total_fee),
            "disclaimer": self._config.fee_disclaimer,
            "simulated": True,
        }

    def slippage(self) -> dict[str, Any]:
        return {
            "ok": True,
            "model": "half-spread + square-root participation impact + volatility adjustment",
            "model_version": self._config.version,
            "spread_bps": text(self._config.spread_bps),
            "impact_coefficient_bps": text(self._config.impact_coefficient_bps),
            "volatility_weight": text(self._config.volatility_weight),
            "maximum_slippage_bps": text(self._config.maximum_slippage_bps),
            "limitations": [
                "NM-02 supplies last trades and bars, not a consolidated bid/ask or depth book.",
                "The spread is a disclosed approximation, never a fabricated live quote.",
                "Available quantity is the reference volume multiplied by the configured participation rate.",
            ],
        }

    def status(self) -> dict[str, Any]:
        counts = self._store.counts()
        return {
            "ok": True,
            "phase": "NM-10",
            "operational": True,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "config_version": self._config.version,
            "order_types": [item.value for item in OrderType],
            "statuses": [item.value for item in OrderStatus],
            "counts": counts,
            "fills": counts["fills"],
            "reconciliations": counts["reconciliations"],
            "last_error": self._last_error,
            "latency_ms": {
                "samples": len(self._latencies),
                "last": round(self._latencies[-1], 3) if self._latencies else None,
                "average": round(sum(self._latencies) / len(self._latencies), 3) if self._latencies else None,
            },
            "liquidity": {
                "participation_rate": text(self._config.participation_rate),
                "maximum_slippage_bps": text(self._config.maximum_slippage_bps),
            },
            "fee_config_version": self._config.version,
            "fee_effective_date": self._config.fee_effective_date,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "NM-10",
            "contract_version": "1.0.0",
            "paper_only": True,
            "live_broker": False,
            "order_types": [item.value for item in OrderType],
            "statuses": [item.value for item in OrderStatus],
            "transitions": {
                status.value: sorted(item.value for item in targets)
                for status, targets in __import__("novacontrol.novamarket.execution.models", fromlist=["ORDER_TRANSITIONS"]).ORDER_TRANSITIONS.items()
            },
            "requires": [
                "NM-09 proposal status=approved",
                "NM-08 approved, unexpired decision bound to the same proposal",
                "valid NM-02 reference price and stale-data policy",
                "sufficient NM-03 simulated cash or NM-07 position quantity",
            ],
            "never": [
                "place real orders",
                "contact a broker",
                "create a fill without ledger-backed settlement",
                "update a position before a fill",
            ],
            "config": self._config.to_dict(),
        }

    # -- reconciliation ------------------------------------------------------
    def reconciliations(self, limit: int = 50) -> dict[str, Any]:
        rows = self._store.reconciliations(limit=limit)
        return {"ok": True, "reconciliations": [row.to_dict() for row in rows], "total": len(rows)}

    async def reconcile(self, order_id: str = "") -> dict[str, Any]:
        async with self._lock:
            await self._recover_inflight_locked()
            result = await self._reconciler.run(order_id)
            self._store.save_reconciliation(result)
            await self._emit(
                (
                    "novamarket.execution.reconciled"
                    if result.ok
                    else "novamarket.execution.reconciliation.failed"
                ),
                {
                    "reconciliation_id": result.reconciliation_id,
                    "order_id": result.order_id,
                    "ok": result.ok,
                    "issues": len(result.issues),
                },
            )
            return result.to_dict()

    async def close(self) -> None:
        self._store.close()
