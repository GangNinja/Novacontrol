"""Canonical NM-10 order, fill, reservation and reconciliation records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping

PAISE = Decimal("0.01")
SHARE = Decimal("0.0001")


def money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_EVEN)


def quantity(value: object) -> Decimal:
    return Decimal(str(value)).quantize(SHARE, rounding=ROUND_HALF_EVEN)


def text(value: Decimal) -> str:
    return format(value, "f")


def parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif value in (None, ""):
        raise ValueError("a timestamp is required")
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def execution_id(prefix: str, *parts: object) -> str:
    digest = sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    CREATED = "created"
    PENDING_VALIDATION = "pending_validation"
    ACCEPTED = "accepted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


#: Every legal edge.  A transition table is the executable lifecycle contract;
#: the store refuses an edge that is not here.
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.PENDING_VALIDATION}),
    OrderStatus.PENDING_VALIDATION: frozenset(
        {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.FAILED}
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset({OrderStatus.CANCELLED, OrderStatus.FAILED}),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}

TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.FAILED,
    }
)

STATUS_EVENTS: dict[OrderStatus, str] = {
    OrderStatus.CREATED: "order.created",
    OrderStatus.ACCEPTED: "order.validated",
    OrderStatus.OPEN: "order.opened",
    OrderStatus.PARTIALLY_FILLED: "order.partially_filled",
    OrderStatus.FILLED: "order.filled",
    OrderStatus.CANCEL_PENDING: "order.cancel_requested",
    OrderStatus.CANCELLED: "order.cancelled",
    OrderStatus.REJECTED: "order.rejected",
    OrderStatus.EXPIRED: "order.expired",
    OrderStatus.FAILED: "order.failed",
}


def ensure_transition(source: OrderStatus | str, target: OrderStatus | str) -> tuple[OrderStatus, OrderStatus]:
    current = OrderStatus(source)
    wanted = OrderStatus(target)
    if wanted not in ORDER_TRANSITIONS[current]:
        raise ValueError(f"illegal paper order transition {current.value} -> {wanted.value}")
    return current, wanted


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    instrument_id: str
    reference_price: Decimal
    observed_at: datetime
    source: str
    volume: Decimal
    available_quantity: Decimal
    volatility_pct: Decimal
    trading_status: str
    currency: str = "INR"
    is_fallback: bool = False
    fresh: bool = True
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_price", money(self.reference_price))
        object.__setattr__(self, "volume", quantity(self.volume))
        object.__setattr__(self, "available_quantity", quantity(self.available_quantity))
        object.__setattr__(self, "volatility_pct", Decimal(str(self.volatility_pct)))
        object.__setattr__(self, "observed_at", parse_time(self.observed_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "reference_price": text(self.reference_price),
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "volume": text(self.volume),
            "available_quantity": text(self.available_quantity),
            "volatility_pct": text(self.volatility_pct),
            "trading_status": self.trading_status,
            "currency": self.currency,
            "is_fallback": self.is_fallback,
            "fresh": self.fresh,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    proposal_id: str
    strategy_id: str
    portfolio_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    status: OrderStatus
    risk_decision_id: str
    execution_config_version: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    reference_price: Decimal | None = None
    market_reference: Mapping[str, Any] = field(default_factory=dict)
    triggered_at: datetime | None = None
    reason: str = ""
    rejection_reason: str = ""
    cancellation_reason: str = ""
    idempotency_key: str = ""
    actor: str = ""
    last_error: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if self.requested_quantity == 0 and self.remaining_quantity == 0:
            pass
        object.__setattr__(self, "side", OrderSide(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "status", OrderStatus(self.status))
        object.__setattr__(self, "requested_quantity", quantity(self.requested_quantity))
        object.__setattr__(self, "filled_quantity", quantity(self.filled_quantity))
        object.__setattr__(self, "remaining_quantity", quantity(self.remaining_quantity))
        for name in ("limit_price", "trigger_price", "reference_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, money(value))
        object.__setattr__(self, "created_at", parse_time(self.created_at))
        object.__setattr__(self, "updated_at", parse_time(self.updated_at))
        object.__setattr__(self, "expires_at", parse_time(self.expires_at))
        if self.triggered_at is not None:
            object.__setattr__(self, "triggered_at", parse_time(self.triggered_at))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Order":
        values = dict(payload)
        values["requested_quantity"] = values.get("requested_quantity", values.get("quantity", "0"))
        values["quantity"] = values.get("requested_quantity", values.get("quantity", "0"))
        values["order_type"] = values.get("order_type", "market")
        values["status"] = values.get("status", "created")
        values.setdefault("filled_quantity", "0")
        values.setdefault("remaining_quantity", values["requested_quantity"])
        values.setdefault("risk_decision_id", "")
        values.setdefault("execution_config_version", "")
        values.setdefault("limit_price", None)
        values.setdefault("trigger_price", None)
        values.setdefault("reference_price", None)
        values.setdefault("market_reference", {})
        values.setdefault("triggered_at", None)
        values.setdefault("reason", "")
        values.setdefault("rejection_reason", "")
        values.setdefault("cancellation_reason", "")
        values.setdefault("idempotency_key", "")
        values.setdefault("actor", "")
        values.setdefault("last_error", "")
        values.setdefault("version", 1)
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in values.items() if key in allowed and key != "quantity"})

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_live(self) -> bool:
        return self.status in {
            OrderStatus.ACCEPTED,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "portfolio_id": self.portfolio_id,
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": text(self.requested_quantity),
            "requested_quantity": text(self.requested_quantity),
            "filled_quantity": text(self.filled_quantity),
            "remaining_quantity": text(self.remaining_quantity),
            "limit_price": text(self.limit_price) if self.limit_price is not None else None,
            "trigger_price": text(self.trigger_price) if self.trigger_price is not None else None,
            "reference_price": text(self.reference_price) if self.reference_price is not None else None,
            "status": self.status.value,
            "risk_decision_id": self.risk_decision_id,
            "execution_config_version": self.execution_config_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "market_reference": dict(self.market_reference),
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "reason": self.reason,
            "rejection_reason": self.rejection_reason,
            "cancellation_reason": self.cancellation_reason,
            "idempotency_key": self.idempotency_key,
            "actor": self.actor,
            "last_error": self.last_error,
            "is_paper": True,
            "is_executed": self.status is OrderStatus.FILLED,
            "can_cancel": self.is_live,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    order_id: str
    instrument_id: str
    sequence: int
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    slippage_amount: Decimal
    slippage_pct: Decimal
    slippage_model_version: str
    fee_breakdown: Mapping[str, Decimal]
    total_fees: Decimal
    gross_value: Decimal
    net_cash: Decimal
    status: str
    market_reference: Mapping[str, Any]
    executed_at: datetime
    ledger_transaction_ids: tuple[str, ...]
    position_effect: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", quantity(self.quantity))
        object.__setattr__(self, "price", money(self.price))
        object.__setattr__(self, "reference_price", money(self.reference_price))
        object.__setattr__(self, "slippage_amount", money(self.slippage_amount))
        object.__setattr__(self, "slippage_pct", Decimal(str(self.slippage_pct)))
        object.__setattr__(self, "total_fees", money(self.total_fees))
        object.__setattr__(self, "gross_value", money(self.gross_value))
        object.__setattr__(self, "net_cash", money(self.net_cash))
        object.__setattr__(self, "fee_breakdown", {key: money(value) for key, value in self.fee_breakdown.items()})
        object.__setattr__(self, "executed_at", parse_time(self.executed_at))
        object.__setattr__(self, "ledger_transaction_ids", tuple(str(item) for item in self.ledger_transaction_ids))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Fill":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "instrument_id": self.instrument_id,
            "sequence": self.sequence,
            "quantity": text(self.quantity),
            "price": text(self.price),
            "reference_price": text(self.reference_price),
            "slippage_amount": text(self.slippage_amount),
            "slippage_pct": text(self.slippage_pct),
            "slippage_model_version": self.slippage_model_version,
            "fee_breakdown": {key: text(value) for key, value in self.fee_breakdown.items()},
            "total_fees": text(self.total_fees),
            "gross_value": text(self.gross_value),
            "net_cash": text(self.net_cash),
            "status": self.status,
            "market_reference": dict(self.market_reference),
            "executed_at": self.executed_at.isoformat(),
            "ledger_transaction_ids": list(self.ledger_transaction_ids),
            "position_effect": dict(self.position_effect),
        }


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    order_id: str
    portfolio_id: str
    kind: str
    amount: Decimal
    remaining: Decimal
    status: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", quantity(self.amount) if self.kind == "quantity" else money(self.amount))
        object.__setattr__(self, "remaining", quantity(self.remaining) if self.kind == "quantity" else money(self.remaining))
        object.__setattr__(self, "created_at", parse_time(self.created_at))
        object.__setattr__(self, "updated_at", parse_time(self.updated_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "order_id": self.order_id,
            "portfolio_id": self.portfolio_id,
            "kind": self.kind,
            "amount": text(self.amount),
            "remaining": text(self.remaining),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    reconciliation_id: str
    order_id: str
    ok: bool
    issues: tuple[Mapping[str, Any], ...]
    checks: Mapping[str, Any]
    created_at: datetime

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReconciliationResult":
        values = dict(payload)
        values["issues"] = tuple(values.get("issues", ()))
        values.setdefault("checks", {})
        # Stored payloads carry the timestamp as an ISO string.  Leaving it as
        # a string makes the next to_dict() call fail on .isoformat(), which
        # broke GET /market/execution/reconciliations after a restart.
        created = values.get("created_at")
        if isinstance(created, str):
            values["created_at"] = parse_time(created)
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in values.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "order_id": self.order_id,
            "ok": self.ok,
            "issues": [dict(item) for item in self.issues],
            "checks": dict(self.checks),
            "created_at": self.created_at.isoformat(),
        }
