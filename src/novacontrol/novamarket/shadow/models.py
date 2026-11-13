"""NM-12 shadow-mode records: sessions, decisions, projections and outcomes.

Shadow Mode observes live market data and records what NovaMarket *would* have
done.  Every type in this module is either a session (mutable lifecycle) or an
append-only observation.  A simulation is never an execution: a projection has
no execution order id, is labelled hypothetical, and cannot be filled by any
code path in this package.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from enum import StrEnum
from typing import Any, Mapping

PAISE = Decimal("0.01")
SHARE = Decimal("0.0001")


class ShadowSessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: Explicit lifecycle.  An unlisted edge raises; a terminal session is never
#: silently restarted by a later request.
SESSION_TRANSITIONS: dict[ShadowSessionStatus, frozenset[ShadowSessionStatus]] = {
    ShadowSessionStatus.CREATED: frozenset(
        {ShadowSessionStatus.RUNNING, ShadowSessionStatus.CANCELLED}
    ),
    ShadowSessionStatus.RUNNING: frozenset(
        {
            ShadowSessionStatus.PAUSED,
            ShadowSessionStatus.STOPPED,
            ShadowSessionStatus.COMPLETED,
            ShadowSessionStatus.CANCELLED,
            ShadowSessionStatus.FAILED,
        }
    ),
    ShadowSessionStatus.PAUSED: frozenset(
        {
            ShadowSessionStatus.RUNNING,
            ShadowSessionStatus.STOPPED,
            ShadowSessionStatus.COMPLETED,
            ShadowSessionStatus.CANCELLED,
            ShadowSessionStatus.FAILED,
        }
    ),
    ShadowSessionStatus.STOPPED: frozenset(
        {ShadowSessionStatus.COMPLETED, ShadowSessionStatus.CANCELLED}
    ),
    ShadowSessionStatus.FAILED: frozenset({ShadowSessionStatus.CANCELLED}),
    ShadowSessionStatus.COMPLETED: frozenset(),
    ShadowSessionStatus.CANCELLED: frozenset(),
}

TERMINAL_STATUSES = frozenset(
    {
        ShadowSessionStatus.COMPLETED,
        ShadowSessionStatus.CANCELLED,
        ShadowSessionStatus.STOPPED,
    }
)


class ShadowDecisionStatus(StrEnum):
    SIGNAL = "signal"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    PROJECTED = "projected"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class ShadowProjectionStatus(StrEnum):
    PROJECTED = "projected"
    UNAVAILABLE_LIQUIDITY = "unavailable_liquidity"
    EXPIRED = "expired"


class ShadowOutcomeStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    MISSING_DATA = "missing_data"


class ShadowErrorKind(StrEnum):
    DATA_STALE = "data_stale"
    DATA_UNAVAILABLE = "data_unavailable"
    DATA_MISSING = "data_missing"
    STRATEGY_FAILED = "strategy_failed"
    RISK_FAILED = "risk_failed"
    EXECUTION_BLOCKED = "execution_blocked"
    RECOVERY = "recovery"
    VALIDATION = "validation"
    CYCLE_FAILED = "cycle_failed"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


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


def optional_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_time(value)


def decimal(value: object, *, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def money(value: object) -> Decimal:
    return decimal(value).quantize(PAISE, rounding=ROUND_HALF_EVEN)


def quantity(value: object) -> Decimal:
    return decimal(value).quantize(SHARE, rounding=ROUND_HALF_EVEN)


def text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def shadow_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """The immutable baseline captured at session start.

    A later change to the live portfolio never rewrites this object; a new
    session is the only way to get a new baseline.
    """

    snapshot_id: str
    portfolio_id: str
    captured_at: datetime
    positions: tuple[dict[str, Any], ...]
    cash: str
    invested: str
    total_value: str
    currency: str = "INR"
    source: str = "nm07"
    note: str = ""

    @classmethod
    def from_detail(
        cls,
        portfolio_id: str,
        detail: Mapping[str, Any],
        *,
        at: datetime,
        source: str = "nm07",
        note: str = "",
    ) -> "PortfolioSnapshot":
        valuation = dict(detail.get("valuation") or {})
        total = money(
            detail.get("total_value")
            or valuation.get("total_value")
            or valuation.get("total")
            or "0"
        )
        if total <= 0:
            raise ValueError("the portfolio snapshot needs a positive total value")
        positions: list[dict[str, Any]] = []
        for row in detail.get("holdings") or ():
            item = dict(row)
            market_value = money(
                item.get("market_value")
                or item.get("value")
                or Decimal(str(item.get("quantity") or "0"))
                * Decimal(str(item.get("last_price") or item.get("price") or "0"))
            )
            item["market_value"] = text(market_value)
            item["weight"] = text(market_value / total)
            positions.append(item)
        return cls(
            snapshot_id="psnap_" + digest(
                [portfolio_id, total, at.isoformat(), positions]
            )[:20],
            portfolio_id=portfolio_id or "shadow",
            captured_at=at,
            positions=tuple(positions),
            cash=text(money(detail.get("cash") or "0")) or "0.00",
            invested=text(money(detail.get("invested") or total)) or "0.00",
            total_value=text(total) or "0.00",
            currency=str(detail.get("currency") or "INR"),
            source=source,
            note=note,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, at: datetime) -> "PortfolioSnapshot":
        positions = tuple(dict(item) for item in payload.get("positions") or ())
        total = money(payload.get("total_value") or "0")
        if total <= 0:
            raise ValueError("the baseline snapshot needs a positive total_value")
        normalized: list[dict[str, Any]] = []
        for item in positions:
            row = dict(item)
            value = money(
                row.get("market_value")
                or Decimal(str(row.get("quantity") or "0"))
                * Decimal(str(row.get("price") or row.get("last_price") or "0"))
            )
            row["market_value"] = text(value)
            row["weight"] = text(value / total)
            normalized.append(row)
        return cls(
            snapshot_id=str(
                payload.get("snapshot_id")
                or "psnap_" + digest([total, at.isoformat(), normalized])[:20]
            ),
            portfolio_id=str(payload.get("portfolio_id") or "shadow"),
            captured_at=at,
            positions=tuple(normalized),
            cash=text(money(payload.get("cash") or "0")) or "0.00",
            invested=text(money(payload.get("invested") or total)) or "0.00",
            total_value=text(total) or "0.00",
            currency=str(payload.get("currency") or "INR"),
            source=str(payload.get("source") or "provided"),
            note=str(payload.get("note") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "portfolio_id": self.portfolio_id,
            "captured_at": self.captured_at.isoformat(),
            "positions": [dict(item) for item in self.positions],
            "cash": self.cash,
            "invested": self.invested,
            "total_value": self.total_value,
            "currency": self.currency,
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PortfolioSnapshot":
        return cls(
            snapshot_id=str(payload["snapshot_id"]),
            portfolio_id=str(payload["portfolio_id"]),
            captured_at=parse_time(payload["captured_at"]),
            positions=tuple(dict(item) for item in payload.get("positions") or ()),
            cash=str(payload.get("cash") or "0.00"),
            invested=str(payload.get("invested") or "0.00"),
            total_value=str(payload.get("total_value") or "0.00"),
            currency=str(payload.get("currency") or "INR"),
            source=str(payload.get("source") or ""),
            note=str(payload.get("note") or ""),
        )


@dataclass(frozen=True, slots=True)
class ShadowSession:
    session_id: str
    name: str
    description: str
    status: ShadowSessionStatus
    portfolio_id: str
    instruments: tuple[str, ...]
    strategies: tuple[str, ...]
    horizons: tuple[str, ...]
    market_data_source: str
    start_at: datetime | None
    end_at: datetime | None
    monitoring_interval_seconds: int
    risk_policy_version: str
    observation_intervals: tuple[int, ...]
    metadata: dict[str, Any]
    baseline: PortfolioSnapshot | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    paused_at: datetime | None = None
    stopped_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    last_cycle_at: datetime | None = None
    cycles: int = 0
    decision_count: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "portfolio_id": self.portfolio_id,
            "instruments": list(self.instruments),
            "strategies": list(self.strategies),
            "horizons": list(self.horizons),
            "market_data_source": self.market_data_source,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "monitoring_interval_seconds": self.monitoring_interval_seconds,
            "risk_policy_version": self.risk_policy_version,
            "observation_intervals": list(self.observation_intervals),
            "metadata": dict(self.metadata),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "cycles": self.cycles,
            "decision_count": self.decision_count,
            "error": self.error,
            "simulated": True,
            "execution_connected": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowSession":
        baseline = payload.get("baseline")
        return cls(
            session_id=str(payload["session_id"]),
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            status=ShadowSessionStatus(str(payload.get("status") or "created")),
            portfolio_id=str(payload.get("portfolio_id") or ""),
            instruments=tuple(str(item) for item in payload.get("instruments") or ()),
            strategies=tuple(str(item) for item in payload.get("strategies") or ()),
            horizons=tuple(str(item) for item in payload.get("horizons") or ()),
            market_data_source=str(payload.get("market_data_source") or "nm02"),
            start_at=optional_time(payload.get("start_at")),
            end_at=optional_time(payload.get("end_at")),
            monitoring_interval_seconds=int(
                payload.get("monitoring_interval_seconds") or 60
            ),
            risk_policy_version=str(payload.get("risk_policy_version") or ""),
            observation_intervals=tuple(
                int(item) for item in payload.get("observation_intervals") or (1, 5, 20)
            ),
            metadata=dict(payload.get("metadata") or {}),
            baseline=PortfolioSnapshot.from_dict(baseline) if baseline else None,
            created_at=parse_time(payload["created_at"]),
            updated_at=parse_time(payload["updated_at"]),
            started_at=optional_time(payload.get("started_at")),
            paused_at=optional_time(payload.get("paused_at")),
            stopped_at=optional_time(payload.get("stopped_at")),
            completed_at=optional_time(payload.get("completed_at")),
            cancelled_at=optional_time(payload.get("cancelled_at")),
            last_cycle_at=optional_time(payload.get("last_cycle_at")),
            cycles=int(payload.get("cycles") or 0),
            decision_count=int(payload.get("decision_count") or 0),
            error=str(payload.get("error") or ""),
        )


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    """One strategy observation, with its risk result and projection link."""

    decision_id: str
    session_id: str
    strategy_id: str
    strategy_version: str
    instrument_id: str
    signal_type: str
    confidence: str
    decision_at: datetime
    data_timestamp: datetime
    data_reference: dict[str, Any]
    portfolio_snapshot_id: str
    risk_policy_version: str
    status: ShadowDecisionStatus
    proposal: dict[str, Any]
    risk: dict[str, Any]
    projection_id: str
    explanation: str
    conditions: tuple[str, ...] = ()
    latency_ms: float = 0.0
    idempotency_key: str = ""
    simulated: bool = True
    execution_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "instrument_id": self.instrument_id,
            "signal_type": self.signal_type,
            "confidence": self.confidence,
            "decision_at": self.decision_at.isoformat(),
            "market_data_timestamp": self.data_timestamp.isoformat(),
            "market_data_reference": dict(self.data_reference),
            "portfolio_snapshot_id": self.portfolio_snapshot_id,
            "risk_policy_version": self.risk_policy_version,
            "status": self.status.value,
            "proposal": dict(self.proposal),
            "risk": dict(self.risk),
            "projection_id": self.projection_id,
            "explanation": self.explanation,
            "conditions": list(self.conditions),
            "latency_ms": round(self.latency_ms, 3),
            "idempotency_key": self.idempotency_key,
            "simulated": self.simulated,
            "execution_permitted": False,
            "label": "shadow decision",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowDecision":
        return cls(
            decision_id=str(payload["decision_id"]),
            session_id=str(payload["session_id"]),
            strategy_id=str(payload.get("strategy_id") or ""),
            strategy_version=str(payload.get("strategy_version") or ""),
            instrument_id=str(payload.get("instrument_id") or ""),
            signal_type=str(payload.get("signal_type") or "no_signal"),
            confidence=str(payload.get("confidence") or "0"),
            decision_at=parse_time(payload["decision_at"]),
            data_timestamp=parse_time(payload["market_data_timestamp"]),
            data_reference=dict(payload.get("market_data_reference") or {}),
            portfolio_snapshot_id=str(payload.get("portfolio_snapshot_id") or ""),
            risk_policy_version=str(payload.get("risk_policy_version") or ""),
            status=ShadowDecisionStatus(str(payload.get("status") or "signal")),
            proposal=dict(payload.get("proposal") or {}),
            risk=dict(payload.get("risk") or {}),
            projection_id=str(payload.get("projection_id") or ""),
            explanation=str(payload.get("explanation") or ""),
            conditions=tuple(str(item) for item in payload.get("conditions") or ()),
            latency_ms=float(payload.get("latency_ms") or 0.0),
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )


@dataclass(frozen=True, slots=True)
class ShadowProjection:
    """A hypothetical order projection.  It is never an execution record."""

    projection_id: str
    decision_id: str
    session_id: str
    instrument_id: str
    side: str
    action: str
    quantity: str
    order_type: str
    reference_price: str
    estimated_price: str | None
    estimated_slippage_pct: str
    estimated_fees: dict[str, str]
    estimated_total_fees: str
    estimated_notional: str
    assumptions: tuple[str, ...]
    status: ShadowProjectionStatus
    created_at: datetime
    notes: str = ""
    simulated: bool = True
    execution_order_id: str | None = None
    label: str = "hypothetical order projection"

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "action": self.action,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "reference_price": self.reference_price,
            "estimated_price": self.estimated_price,
            "estimated_slippage_pct": self.estimated_slippage_pct,
            "estimated_fees": dict(self.estimated_fees),
            "estimated_total_fees": self.estimated_total_fees,
            "estimated_notional": self.estimated_notional,
            "assumptions": list(self.assumptions),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "notes": self.notes,
            "simulated": self.simulated,
            "execution_order_id": None,
            "execution_permitted": False,
            "is_execution": False,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowProjection":
        return cls(
            projection_id=str(payload["projection_id"]),
            decision_id=str(payload["decision_id"]),
            session_id=str(payload["session_id"]),
            instrument_id=str(payload.get("instrument_id") or ""),
            side=str(payload.get("side") or "buy"),
            action=str(payload.get("action") or ""),
            quantity=str(payload.get("quantity") or "0"),
            order_type=str(payload.get("order_type") or "market"),
            reference_price=str(payload.get("reference_price") or "0"),
            estimated_price=(
                None
                if payload.get("estimated_price") in (None, "")
                else str(payload.get("estimated_price"))
            ),
            estimated_slippage_pct=str(payload.get("estimated_slippage_pct") or "0"),
            estimated_fees={
                str(key): str(value)
                for key, value in dict(payload.get("estimated_fees") or {}).items()
            },
            estimated_total_fees=str(payload.get("estimated_total_fees") or "0"),
            estimated_notional=str(payload.get("estimated_notional") or "0"),
            assumptions=tuple(str(item) for item in payload.get("assumptions") or ()),
            status=ShadowProjectionStatus(
                str(payload.get("status") or "projected")
            ),
            created_at=parse_time(payload["created_at"]),
            notes=str(payload.get("notes") or ""),
        )


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """A hypothetical outcome measured only from later observations."""

    outcome_id: str
    decision_id: str
    session_id: str
    instrument_id: str
    interval: int
    status: ShadowOutcomeStatus
    observed_at: datetime | None
    price: str | None
    return_pct: str | None
    pnl: str | None
    mfe_pct: str | None
    mae_pct: str | None
    bars_observed: int
    note: str
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "instrument_id": self.instrument_id,
            "interval": self.interval,
            "status": self.status.value,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "price": self.price,
            "hypothetical_return_pct": self.return_pct,
            "hypothetical_pnl": self.pnl,
            "max_favorable_pct": self.mfe_pct,
            "max_adverse_pct": self.mae_pct,
            "bars_observed": self.bars_observed,
            "note": self.note,
            "updated_at": self.updated_at.isoformat(),
            "hypothetical": True,
            "simulated": True,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowOutcome":
        return cls(
            outcome_id=str(payload["outcome_id"]),
            decision_id=str(payload["decision_id"]),
            session_id=str(payload["session_id"]),
            instrument_id=str(payload.get("instrument_id") or ""),
            interval=int(payload.get("interval") or 1),
            status=ShadowOutcomeStatus(str(payload.get("status") or "unresolved")),
            observed_at=optional_time(payload.get("observed_at")),
            price=None if payload.get("price") is None else str(payload.get("price")),
            return_pct=(
                None
                if payload.get("hypothetical_return_pct") is None
                else str(payload.get("hypothetical_return_pct"))
            ),
            pnl=(
                None
                if payload.get("hypothetical_pnl") is None
                else str(payload.get("hypothetical_pnl"))
            ),
            mfe_pct=(
                None
                if payload.get("max_favorable_pct") is None
                else str(payload.get("max_favorable_pct"))
            ),
            mae_pct=(
                None
                if payload.get("max_adverse_pct") is None
                else str(payload.get("max_adverse_pct"))
            ),
            bars_observed=int(payload.get("bars_observed") or 0),
            note=str(payload.get("note") or ""),
            updated_at=parse_time(payload["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    comparison_id: str
    session_id: str
    categories: tuple[str, ...]
    evidence: dict[str, Any]
    compatible: bool
    note: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "session_id": self.session_id,
            "categories": list(self.categories),
            "evidence": dict(self.evidence),
            "compatible": self.compatible,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowComparison":
        return cls(
            comparison_id=str(payload["comparison_id"]),
            session_id=str(payload["session_id"]),
            categories=tuple(str(item) for item in payload.get("categories") or ()),
            evidence=dict(payload.get("evidence") or {}),
            compatible=bool(payload.get("compatible")),
            note=str(payload.get("note") or ""),
            created_at=parse_time(payload["created_at"]),
        )


def with_status(
    session: ShadowSession,
    status: ShadowSessionStatus,
    *,
    at: datetime,
    error: str = "",
) -> ShadowSession:
    moment = at
    return replace(
        session,
        status=status,
        updated_at=moment,
        started_at=moment if status is ShadowSessionStatus.RUNNING and not session.started_at else session.started_at,
        paused_at=moment if status is ShadowSessionStatus.PAUSED else session.paused_at,
        stopped_at=moment if status is ShadowSessionStatus.STOPPED else session.stopped_at,
        completed_at=moment if status is ShadowSessionStatus.COMPLETED else session.completed_at,
        cancelled_at=moment if status is ShadowSessionStatus.CANCELLED else session.cancelled_at,
        error=error or session.error,
    )
