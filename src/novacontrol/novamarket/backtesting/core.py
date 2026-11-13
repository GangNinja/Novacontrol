"""NM-11 reproducible historical backtesting.

The engine is isolated from production wallets and the paper-execution store.
It reuses NM-09 catalog evaluators and NM-10 execution pricing, fees and
liquidity assumptions, while replaying only point-in-time observations.

Timing convention: a completed bar observed at T may inform a decision at T.
An order created from that decision is eligible from the next bar onward. It is
never filled at the close that produced the signal.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.execution.models import (
    MarketSnapshot,
    OrderSide,
    OrderType,
    execution_id,
    money,
    parse_time,
    quantity,
    text,
)
from novacontrol.novamarket.execution.pricing import (
    ExecutionPricingError,
    execution_price,
    exits_limit,
    fee_breakdown,
)
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.portfolio.models import Position
from novacontrol.novamarket.risk import (
    POLICY_DEFAULTS as RISK_POLICY_DEFAULTS,
    RiskPolicy,
    RiskSnapshot,
    evaluate_policy,
)
from novacontrol.novamarket.strategies.catalog import (
    EVALUATORS,
    StrategyContext,
    validate_config,
)
from novacontrol.novamarket.strategies.models import SignalType

SOFTWARE_VERSION = "nm11-backtesting-1.0"
SUPPORTED_FREQUENCIES = ("1d",)
_ACTIONABLE = {SignalType.BUY, SignalType.SELL, SignalType.EXIT}


class BacktestError(ValueError):
    """The backtest cannot be run from supplied records."""


def _dec(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise BacktestError(f"{name} must be decimal") from exc
    if not result.is_finite():
        raise BacktestError(f"{name} must be finite")
    return result


def _hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, default=str, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _optional_moment(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_time(value)


def _expiry_time(
    schedule: Sequence[datetime], moment: datetime, ttl_bars: int
) -> datetime:
    """The timestamp of the ``ttl_bars``-th later bar, or the last known one.

    Expiry is counted in BARS, not calendar days: a Friday order with a one-bar
    TTL must not expire over the weekend before Monday's bar arrives.
    """
    later = [item for item in schedule if item > moment]
    if not later:
        return moment
    return later[min(max(1, ttl_bars), len(later)) - 1]


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    """One completed, source-attributed OHLCV bar."""

    instrument_id: str
    observed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str = "nm02"
    available_at: datetime | None = None
    retrieved_at: datetime | None = None
    partial: bool = False
    tradable: bool = True
    adjusted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", parse_time(self.observed_at))
        available = self.available_at or self.observed_at
        object.__setattr__(self, "available_at", parse_time(available))
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", parse_time(self.retrieved_at))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, money(getattr(self, name)))
        object.__setattr__(self, "volume", quantity(self.volume))
        if not str(self.instrument_id).strip():
            raise BacktestError("a historical bar needs an instrument id")
        if self.partial:
            raise BacktestError("a partial historical bar is not usable")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise BacktestError("OHLC prices must be positive")
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close, self.high
        ):
            raise BacktestError("OHLC bar is inconsistent")
        if self.volume < 0:
            raise BacktestError("volume cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        assert self.available_at is not None
        return {
            "instrument_id": self.instrument_id,
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "open": text(self.open),
            "high": text(self.high),
            "low": text(self.low),
            "close": text(self.close),
            "volume": text(self.volume),
            "source": self.source,
            "partial": self.partial,
            "tradable": self.tradable,
            "adjusted": self.adjusted,
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "HistoricalBar":
        observed = parse_time(row.get("observed_at") or row.get("at"))
        return cls(
            instrument_id=str(row.get("instrument_id") or ""),
            observed_at=observed,
            available_at=_optional_moment(row.get("available_at")) or observed,
            retrieved_at=_optional_moment(row.get("retrieved_at")),
            open=_dec(row.get("open"), "open"),
            high=_dec(row.get("high"), "high"),
            low=_dec(row.get("low"), "low"),
            close=_dec(row.get("close"), "close"),
            volume=_dec(row.get("volume", 0), "volume"),
            source=str(row.get("source") or row.get("provider") or "nm02"),
            partial=bool(row.get("partial", False)),
            tradable=bool(row.get("tradable", True)),
            adjusted=bool(row.get("adjusted", False)),
        )


def _position_value(position: Position) -> Decimal:
    value = position.market_value()
    return value if value is not None else position.cost_basis


def _mic(instrument_id: str) -> str:
    return str(instrument_id).split(":", 1)[0].strip().upper()


def risk_policy_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    max_position_weight: Decimal | None = None,
    max_gross_exposure: Decimal | None = None,
) -> RiskPolicy:
    """Build the NM-08 policy object from its persisted snapshot.

    The replay keeps no second limits table: every threshold comes from the
    NM-08 policy and is evaluated by ``evaluate_policy``.
    """
    base = RISK_POLICY_DEFAULTS["short_term"]

    def decimal(key: str, fallback: Decimal) -> Decimal:
        return _dec(snapshot.get(key, fallback), f"risk_policy.{key}")

    drawdown_raw = snapshot.get(
        "max_drawdown_pct", snapshot.get("max_drawdown", base.max_drawdown_pct)
    )
    drawdown = _dec(drawdown_raw, "risk_policy.max_drawdown")
    if "max_drawdown_pct" not in snapshot and drawdown <= Decimal("1"):
        drawdown *= Decimal("100")
    invested = _dec(
        snapshot.get(
            "max_invested_fraction",
            snapshot.get("max_gross_exposure", base.max_invested_fraction),
        ),
        "risk_policy.max_invested_fraction",
    )
    position = decimal("max_position_weight", base.max_position_weight)
    if max_position_weight is not None:
        position = min(position, max_position_weight)
    if max_gross_exposure is not None:
        invested = min(invested, max_gross_exposure)
    return RiskPolicy(
        policy_id=str(snapshot.get("policy_id") or base.policy_id),
        version=str(snapshot.get("version") or "nm08-backtest-v1"),
        label=str(snapshot.get("label") or "Historical backtest policy snapshot"),
        max_position_weight=position,
        max_sector_weight=decimal("max_sector_weight", base.max_sector_weight),
        max_invested_fraction=invested,
        min_cash_fraction=decimal("min_cash_fraction", base.min_cash_fraction),
        max_drawdown_pct=drawdown,
        max_volatility_pct=decimal("max_volatility_pct", base.max_volatility_pct),
        min_liquidity_value=decimal("min_liquidity_value", base.min_liquidity_value),
        max_turnover=decimal("max_turnover", base.max_turnover),
        max_forecast_age_hours=float(
            snapshot.get("max_forecast_age_hours", base.max_forecast_age_hours)
        ),
        allowed_model_states=tuple(
            snapshot.get("allowed_model_states") or base.allowed_model_states
        ),
        max_uncertainty=str(snapshot.get("max_uncertainty", base.max_uncertainty)),
        max_data_age_hours=float(
            snapshot.get("max_data_age_hours", base.max_data_age_hours)
        ),
        max_state_age_hours=float(
            snapshot.get("max_state_age_hours", base.max_state_age_hours)
        ),
        min_observations=max(
            2, int(snapshot.get("min_observations", base.min_observations))
        ),
        max_aggregate_sector_weight=decimal(
            "max_aggregate_sector_weight", base.max_aggregate_sector_weight
        ),
        restricted_instruments=tuple(
            str(item) for item in snapshot.get("restricted_instruments", ())
        ),
        aggregate=bool(snapshot.get("aggregate", False)),
    )


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    name: str
    strategy_catalog_id: str
    strategy_version: str
    strategy_config: Mapping[str, Any]
    instruments: tuple[str, ...]
    start: datetime
    end: datetime
    strategy_id: str = ""
    evaluation_frequency: str = "1d"
    horizon: str = "short_term"
    initial_capital: Decimal = Decimal("100000.00")
    benchmark: str = ""
    risk_policy_version: str = "nm08-backtest-v1"
    risk_policy: Mapping[str, Any] = field(default_factory=dict)
    forecast_model_version: str = ""
    execution_config: ExecutionConfig = field(default_factory=ExecutionConfig)
    dataset_id: str = "nm02-point-in-time"
    dataset_version: str = ""
    random_seed: int = 42
    order_notional: Decimal = Decimal("10000.00")
    max_position_weight: Decimal = Decimal("0.25")
    max_gross_exposure: Decimal = Decimal("1.00")
    order_type: str = "market"
    limit_offset_bps: Decimal = Decimal("25")
    stop_offset_bps: Decimal = Decimal("50")
    order_ttl_bars: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", parse_time(self.start))
        object.__setattr__(self, "end", parse_time(self.end))
        for name in (
            "initial_capital",
            "order_notional",
            "max_position_weight",
            "max_gross_exposure",
            "limit_offset_bps",
            "stop_offset_bps",
        ):
            object.__setattr__(self, name, _dec(getattr(self, name), name))
        object.__setattr__(self, "instruments", tuple(str(item) for item in self.instruments))
        object.__setattr__(self, "strategy_config", dict(self.strategy_config))
        object.__setattr__(self, "risk_policy", dict(self.risk_policy))
        if not self.name.strip():
            raise BacktestError("backtest name is required")
        if self.strategy_catalog_id not in EVALUATORS:
            raise BacktestError(f"unsupported strategy catalog {self.strategy_catalog_id!r}")
        if not self.strategy_version.strip():
            raise BacktestError("strategy_version is required")
        try:
            validate_config(self.strategy_catalog_id, dict(self.strategy_config))
        except ValueError as exc:
            raise BacktestError(f"invalid strategy configuration: {exc}") from exc
        if not self.instruments or any(not item.strip() for item in self.instruments):
            raise BacktestError("at least one instrument is required")
        if len(set(self.instruments)) != len(self.instruments):
            raise BacktestError("instruments must be unique")
        if self.start >= self.end:
            raise BacktestError("start must precede end")
        if self.evaluation_frequency not in SUPPORTED_FREQUENCIES:
            raise BacktestError(
                "evaluation_frequency must be one of " + ", ".join(SUPPORTED_FREQUENCIES)
            )
        if self.initial_capital <= 0 or self.order_notional <= 0:
            raise BacktestError("initial capital and order notional must be positive")
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise BacktestError("max_position_weight must be within (0, 1]")
        if not Decimal("0") < self.max_gross_exposure <= Decimal("10"):
            raise BacktestError("max_gross_exposure must be positive")
        if self.order_ttl_bars < 1:
            raise BacktestError("order_ttl_bars must be at least 1")
        try:
            object.__setattr__(self, "order_type", OrderType(self.order_type).value)
        except ValueError as exc:
            raise BacktestError(f"unsupported order type {self.order_type!r}") from exc
        if not isinstance(self.execution_config, ExecutionConfig):
            raise BacktestError("execution_config must be an ExecutionConfig")
        # Validates the persisted NM-08 snapshot by building the policy object.
        _ = self.effective_risk

    @property
    def effective_risk(self) -> RiskPolicy:
        return risk_policy_from_snapshot(
            self.risk_policy,
            max_position_weight=self.max_position_weight,
            max_gross_exposure=self.max_gross_exposure,
        )

    @property
    def config_hash(self) -> str:
        return _hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "strategy_id": self.strategy_id,
            "strategy_catalog_id": self.strategy_catalog_id,
            "strategy_version": self.strategy_version,
            "strategy_config": dict(self.strategy_config),
            "instruments": list(self.instruments),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "evaluation_frequency": self.evaluation_frequency,
            "horizon": self.horizon,
            "initial_capital": text(self.initial_capital),
            "benchmark": self.benchmark,
            "risk_policy_version": self.risk_policy_version,
            "risk_policy": dict(self.risk_policy),
            "forecast_model_version": self.forecast_model_version,
            "execution_config": self.execution_config.to_dict(),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "random_seed": self.random_seed,
            "order_notional": text(self.order_notional),
            "max_position_weight": text(self.max_position_weight),
            "max_gross_exposure": text(self.max_gross_exposure),
            "order_type": self.order_type,
            "limit_offset_bps": text(self.limit_offset_bps),
            "stop_offset_bps": text(self.stop_offset_bps),
            "order_ttl_bars": self.order_ttl_bars,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BacktestConfig":
        values = dict(payload)
        values["instruments"] = tuple(values.get("instruments") or ())
        values["strategy_config"] = dict(values.get("strategy_config") or {})
        values["risk_policy"] = dict(values.get("risk_policy") or {})
        execution = values.get("execution_config")
        if isinstance(execution, Mapping):
            values["execution_config"] = ExecutionConfig.from_mapping(execution)
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise BacktestError("unknown backtest configuration field(s): " + ", ".join(unknown))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    backtest_id: str
    order_id: str
    instrument_id: str
    side: str
    order_type: str
    decision_at: datetime
    executed_at: datetime
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    fees: Decimal
    slippage: Decimal
    slippage_pct: Decimal
    cash_after: Decimal
    position_after: Decimal
    realized_pnl: Decimal
    reason: str
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "backtest_id": self.backtest_id,
            "order_id": self.order_id,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "order_type": self.order_type,
            "decision_at": self.decision_at.isoformat(),
            "executed_at": self.executed_at.isoformat(),
            "quantity": text(self.quantity),
            "price": text(self.price),
            "reference_price": text(self.reference_price),
            "fees": text(self.fees),
            "slippage": text(self.slippage),
            "slippage_pct": text(self.slippage_pct),
            "cash_after": text(self.cash_after),
            "position_after": text(self.position_after),
            "realized_pnl": text(self.realized_pnl),
            "reason": self.reason,
            "partial": self.partial,
        }


def _trade_repro(trade: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: trade[key]
        for key in (
            "instrument_id",
            "side",
            "order_type",
            "decision_at",
            "executed_at",
            "quantity",
            "price",
            "reference_price",
            "fees",
            "slippage",
            "slippage_pct",
            "cash_after",
            "position_after",
            "realized_pnl",
            "reason",
            "partial",
        )
    }


def _order_repro(order: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: order[key]
        for key in (
            "instrument_id",
            "side",
            "order_type",
            "requested_quantity",
            "filled_quantity",
            "remaining_quantity",
            "decision_at",
            "expires_at",
            "status",
            "limit_price",
            "trigger_price",
            "reason",
            "rejection_reason",
        )
    }


def _signal_repro(signal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: signal[key]
        for key in (
            "instrument_id",
            "at",
            "signal_type",
            "confidence",
            "price",
            "reason",
        )
    }


def _decision_repro(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: decision[key]
        for key in (
            "instrument_id",
            "action",
            "outcome",
            "policy_outcome",
            "at",
            "policy_version",
            "requested_notional",
            "projected_weight",
            "projected_gross",
            "reason",
        )
    }


@dataclass(frozen=True, slots=True)
class SimulatedOrder:
    order_id: str
    backtest_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    decision_at: datetime
    expires_at: datetime
    status: str
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    reason: str = ""
    rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "backtest_id": self.backtest_id,
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "requested_quantity": text(self.requested_quantity),
            "filled_quantity": text(self.filled_quantity),
            "remaining_quantity": text(self.remaining_quantity),
            "decision_at": self.decision_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "limit_price": text(self.limit_price) if self.limit_price is not None else None,
            "trigger_price": text(self.trigger_price) if self.trigger_price is not None else None,
            "reason": self.reason,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    instrument_id: str
    action: str
    outcome: str
    at: datetime
    policy_version: str
    requested_notional: Decimal
    projected_weight: Decimal
    projected_gross: Decimal
    reason: str
    policy_outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "instrument_id": self.instrument_id,
            "action": self.action,
            "outcome": self.outcome,
            "policy_outcome": self.policy_outcome,
            "at": self.at.isoformat(),
            "policy_version": self.policy_version,
            "requested_notional": text(self.requested_notional),
            "projected_weight": text(self.projected_weight),
            "projected_gross": text(self.projected_gross),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SignalRecord:
    signal_id: str
    instrument_id: str
    at: datetime
    signal_type: str
    confidence: Decimal
    price: Decimal
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "instrument_id": self.instrument_id,
            "at": self.at.isoformat(),
            "signal_type": self.signal_type,
            "confidence": text(self.confidence),
            "price": text(self.price),
            "reason": self.reason,
        }


@dataclass(slots=True)
class BacktestResult:
    backtest_id: str
    config: BacktestConfig
    status: str
    trades: list[Trade]
    orders: list[SimulatedOrder]
    equity: list[dict[str, Any]]
    metrics: dict[str, Any]
    benchmark: dict[str, Any]
    robustness: dict[str, Any]
    warnings: list[str]
    data_quality: dict[str, Any]
    risk_decisions: list[RiskDecision]
    signals: list[SignalRecord]
    bars: list[HistoricalBar]
    repro_hash: str
    created_at: datetime

    def to_dict(self, *, include_bars: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "backtest_id": self.backtest_id,
            "status": self.status,
            "completed": self.status == "completed",
            "config": self.config.to_dict(),
            "config_hash": self.config.config_hash,
            "trades": [trade.to_dict() for trade in self.trades],
            "orders": [order.to_dict() for order in self.orders],
            "equity": list(self.equity),
            "metrics": dict(self.metrics),
            "benchmark": dict(self.benchmark),
            "robustness": dict(self.robustness),
            "warnings": list(self.warnings),
            "data_quality": dict(self.data_quality),
            "risk_decisions": [row.to_dict() for row in self.risk_decisions],
            "signals": [row.to_dict() for row in self.signals],
            "repro_hash": self.repro_hash,
            "created_at": self.created_at.isoformat(),
            "software": {
                "version": SOFTWARE_VERSION,
                "python": platform.python_version(),
                "platform": sys.platform,
            },
            "simulated": True,
            "claim": "historical simulation, not live performance",
        }
        if include_bars:
            body["bars"] = [bar.to_dict() for bar in self.bars]
        return body

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BacktestResult":
        config = BacktestConfig.from_mapping(payload["config"])
        trades = [
            Trade(
                trade_id=str(row["trade_id"]),
                backtest_id=str(row["backtest_id"]),
                order_id=str(row.get("order_id") or ""),
                instrument_id=str(row["instrument_id"]),
                side=str(row["side"]),
                order_type=str(row.get("order_type") or "market"),
                decision_at=parse_time(row["decision_at"]),
                executed_at=parse_time(row["executed_at"]),
                quantity=_dec(row["quantity"], "quantity"),
                price=_dec(row["price"], "price"),
                reference_price=_dec(row["reference_price"], "reference_price"),
                fees=_dec(row["fees"], "fees"),
                slippage=_dec(row["slippage"], "slippage"),
                slippage_pct=_dec(row.get("slippage_pct", 0), "slippage_pct"),
                cash_after=_dec(row["cash_after"], "cash_after"),
                position_after=_dec(row["position_after"], "position_after"),
                realized_pnl=_dec(row["realized_pnl"], "realized_pnl"),
                reason=str(row.get("reason") or ""),
                partial=bool(row.get("partial", False)),
            )
            for row in payload.get("trades", [])
        ]
        orders = [
            SimulatedOrder(
                order_id=str(row["order_id"]),
                backtest_id=str(row["backtest_id"]),
                instrument_id=str(row["instrument_id"]),
                side=OrderSide(row["side"]),
                order_type=OrderType(row["order_type"]),
                requested_quantity=_dec(row["requested_quantity"], "requested_quantity"),
                filled_quantity=_dec(row["filled_quantity"], "filled_quantity"),
                remaining_quantity=_dec(row["remaining_quantity"], "remaining_quantity"),
                decision_at=parse_time(row["decision_at"]),
                expires_at=parse_time(row["expires_at"]),
                status=str(row["status"]),
                limit_price=_dec(row["limit_price"], "limit_price") if row.get("limit_price") is not None else None,
                trigger_price=_dec(row["trigger_price"], "trigger_price") if row.get("trigger_price") is not None else None,
                reason=str(row.get("reason") or ""),
                rejection_reason=str(row.get("rejection_reason") or ""),
            )
            for row in payload.get("orders", [])
        ]
        decisions = [
            RiskDecision(
                decision_id=str(row["decision_id"]),
                instrument_id=str(row["instrument_id"]),
                action=str(row["action"]),
                outcome=str(row["outcome"]),
                at=parse_time(row["at"]),
                policy_version=str(row["policy_version"]),
                requested_notional=_dec(row["requested_notional"], "requested_notional"),
                projected_weight=_dec(row["projected_weight"], "projected_weight"),
                projected_gross=_dec(row["projected_gross"], "projected_gross"),
                reason=str(row.get("reason") or ""),
                policy_outcome=str(row.get("policy_outcome") or ""),
            )
            for row in payload.get("risk_decisions", [])
        ]
        signals = [
            SignalRecord(
                signal_id=str(row["signal_id"]),
                instrument_id=str(row["instrument_id"]),
                at=parse_time(row["at"]),
                signal_type=str(row["signal_type"]),
                confidence=_dec(row["confidence"], "confidence"),
                price=_dec(row["price"], "price"),
                reason=str(row.get("reason") or ""),
            )
            for row in payload.get("signals", [])
        ]
        bars = [HistoricalBar.from_mapping(row) for row in payload.get("bars", [])]
        return cls(
            backtest_id=str(payload["backtest_id"]),
            config=config,
            status=str(payload["status"]),
            trades=trades,
            orders=orders,
            equity=[dict(row) for row in payload.get("equity", [])],
            metrics=dict(payload.get("metrics", {})),
            benchmark=dict(payload.get("benchmark", {})),
            robustness=dict(payload.get("robustness", {})),
            warnings=[str(item) for item in payload.get("warnings", [])],
            data_quality=dict(payload.get("data_quality", {})),
            risk_decisions=decisions,
            signals=signals,
            bars=bars,
            repro_hash=str(payload["repro_hash"]),
            created_at=parse_time(payload["created_at"]),
        )


class BacktestStore:
    """Append-only SQLite records for configurations and completed runs."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS backtests(
                    backtest_id TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    bars_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS ix_backtests_hash
                    ON backtests(config_hash, bars_hash, status);
                CREATE TABLE IF NOT EXISTS backtest_runs(
                    run_id TEXT PRIMARY KEY,
                    backtest_id TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    bars_hash TEXT NOT NULL,
                    repro_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(backtest_id, repro_hash)
                );
                CREATE TRIGGER IF NOT EXISTS backtest_runs_immutable
                BEFORE UPDATE ON backtest_runs
                BEGIN SELECT RAISE(ABORT, 'completed backtests are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS backtest_runs_no_delete
                BEFORE DELETE ON backtest_runs
                BEGIN SELECT RAISE(ABORT, 'completed backtests are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS backtests_completed_immutable
                BEFORE UPDATE ON backtests
                WHEN OLD.status = 'completed'
                BEGIN SELECT RAISE(ABORT, 'completed backtests are immutable'); END;
                CREATE TABLE IF NOT EXISTS backtest_comparisons(
                    comparison_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS backtest_comparisons_immutable
                BEFORE UPDATE ON backtest_comparisons
                BEGIN SELECT RAISE(ABORT, 'backtest comparisons are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS backtest_comparisons_no_delete
                BEFORE DELETE ON backtest_comparisons
                BEGIN SELECT RAISE(ABORT, 'backtest comparisons are immutable'); END;
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(backtests)")
            }
            if "error" not in columns:
                self._connection.execute(
                    "ALTER TABLE backtests ADD COLUMN error TEXT NOT NULL DEFAULT ''"
                )

    def create(self, config: BacktestConfig, created_at: datetime) -> str:
        backtest_id = execution_id(
            "bt", config.config_hash, created_at.isoformat(), uuid.uuid4().hex
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO backtests(backtest_id, config_hash, bars_hash, status, "
                "created_at, payload) VALUES (?,?,?,?,?,?)",
                (
                    backtest_id,
                    config.config_hash,
                    "",
                    "created",
                    created_at.isoformat(),
                    json.dumps(config.to_dict(), sort_keys=True, default=str),
                ),
            )
        return backtest_id

    def config(self, backtest_id: str) -> BacktestConfig | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status, payload FROM backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if row["status"] == "completed" and isinstance(payload.get("config"), Mapping):
            # A completed row carries the whole result; its configuration is nested.
            payload = payload["config"]
        return BacktestConfig.from_mapping(payload)

    def find_completed(self, config_hash: str, bars_hash: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT backtest_id FROM backtests WHERE config_hash=? AND bars_hash=? "
                "AND status='completed' LIMIT 1",
                (config_hash, bars_hash),
            ).fetchone()
        return str(row["backtest_id"]) if row else None

    def find_by_config(self, config_hash: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT backtest_id FROM backtests WHERE config_hash=? "
                "AND status IN ('created','completed') ORDER BY created_at LIMIT 1",
                (config_hash,),
            ).fetchone()
        return str(row["backtest_id"]) if row else None

    def save(self, result: BacktestResult) -> None:
        payload = json.dumps(result.to_dict(include_bars=True), sort_keys=True, default=str)
        bars_hash = str(result.data_quality.get("bars_hash") or "")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE backtests SET bars_hash=?, status=?, payload=? "
                "WHERE backtest_id=? AND status='created'",
                (bars_hash, result.status, payload, result.backtest_id),
            )
            if cursor.rowcount != 1:
                raise BacktestError("only a created backtest can receive its first result")
            self._connection.execute(
                "INSERT INTO backtest_runs(run_id, backtest_id, config_hash, bars_hash, "
                "repro_hash, created_at, payload) VALUES (?,?,?,?,?,?,?)",
                (
                    # A repro hash is deterministic, so it cannot be the run's
                    # primary key: two forced replays of one configuration are
                    # distinct persisted runs that must agree on the hash.
                    execution_id("bt_run", result.backtest_id, result.repro_hash),
                    result.backtest_id,
                    result.config.config_hash,
                    bars_hash,
                    result.repro_hash,
                    result.created_at.isoformat(),
                    payload,
                ),
            )

    def result(self, backtest_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def result_model(self, backtest_id: str) -> BacktestResult | None:
        """The completed result, or ``None`` while the row is only a configuration."""
        with self._lock:
            row = self._connection.execute(
                "SELECT status, payload FROM backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
        if row is None or row["status"] != "completed":
            return None
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict) or "repro_hash" not in payload:
            return None
        return BacktestResult.from_dict(payload)

    def record(self, backtest_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT backtest_id, config_hash, bars_hash, status, created_at, error "
                "FROM backtests WHERE backtest_id=?",
                (backtest_id,),
            ).fetchone()
        return dict(row) if row else None

    def status(self, backtest_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
        return str(row["status"]) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Normalised rows that work for created, failed and completed records alike."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT backtest_id, config_hash, bars_hash, status, created_at, payload "
                "FROM backtests ORDER BY created_at DESC, backtest_id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            completed = row["status"] == "completed" and isinstance(
                payload.get("config"), Mapping
            )
            records.append(
                {
                    "backtest_id": row["backtest_id"],
                    "status": row["status"],
                    "config_hash": row["config_hash"],
                    "bars_hash": row["bars_hash"],
                    "created_at": row["created_at"],
                    "config": payload["config"] if completed else payload,
                    "metrics": dict(payload.get("metrics") or {}) if completed else {},
                    "repro_hash": str(payload.get("repro_hash") or "") if completed else "",
                    "error": str(payload.get("error") or "") if completed else "",
                }
            )
        return records

    def cancel(self, backtest_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status FROM backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "created":
                self._connection.execute(
                    "UPDATE backtests SET status='cancelled' WHERE backtest_id=? AND status='created'",
                    (backtest_id,),
                )
        return {"backtest_id": backtest_id, "status": self.status(backtest_id)}

    def fail(self, backtest_id: str, error: str) -> None:
        """Record a failed attempt without destroying the configuration."""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE backtests SET status='failed', error=? "
                "WHERE backtest_id=? AND status='created'",
                (str(error)[:2000], backtest_id),
            )

    def reopen(self, backtest_id: str) -> bool:
        """Re-arm a failed row so the same configuration can be retried."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE backtests SET status='created', error='' "
                "WHERE backtest_id=? AND status='failed'",
                (backtest_id,),
            )
        return cursor.rowcount == 1

    def save_comparison(
        self, kind: str, payload: Mapping[str, Any], created_at: datetime
    ) -> str:
        """Persist one comparison/sensitivity record; the row is immutable."""
        comparison_id = execution_id(
            "bt_cmp", str(kind), created_at.isoformat(), uuid.uuid4().hex
        )
        body = {
            "comparison_id": comparison_id,
            "kind": str(kind),
            "created_at": created_at.isoformat(),
            **dict(payload),
        }
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO backtest_comparisons(comparison_id, kind, created_at, "
                "payload) VALUES (?,?,?,?)",
                (
                    comparison_id,
                    str(kind),
                    created_at.isoformat(),
                    json.dumps(body, sort_keys=True, default=str),
                ),
            )
        return comparison_id

    def comparisons(self, limit: int = 50) -> builtins.list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM backtest_comparisons "
                "ORDER BY created_at DESC, comparison_id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def normalise_bars(
    rows: Iterable[HistoricalBar], *, start: datetime, end: datetime
) -> tuple[list[HistoricalBar], list[str]]:
    bars = [bar for bar in rows if start <= bar.observed_at <= end]
    warnings: list[str] = []
    bars.sort(key=lambda item: (item.observed_at, item.instrument_id))
    seen: set[tuple[str, datetime]] = set()
    clean: list[HistoricalBar] = []
    for bar in bars:
        key = (bar.instrument_id, bar.observed_at)
        if key in seen:
            warnings.append(
                f"duplicate bar removed: {bar.instrument_id} {bar.observed_at.isoformat()}"
            )
            continue
        seen.add(key)
        clean.append(bar)
    if not clean:
        raise BacktestError("no historical bars are available for the requested period")
    return clean, warnings


def _volatility(closes: Sequence[Decimal]) -> Decimal:
    returns = [
        float((current - previous) / previous)
        for previous, current in zip(closes, closes[1:])
        if previous > 0
    ]
    if len(returns) < 2:
        return Decimal("0")
    return Decimal(str(statistics.pstdev(returns) * 100)).quantize(Decimal("0.0001"))


def calculate_metrics(
    equity: Sequence[Mapping[str, Any]],
    trades: Sequence[Trade],
    *,
    initial_capital: Decimal,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    values = [_dec(row["equity"], "equity") for row in equity]
    if len(values) < 2:
        return {
            "observations": len(values),
            "available": False,
            "reason": "at least two equity observations are required",
        }
    returns = [
        float((current - previous) / previous)
        for previous, current in zip(values, values[1:])
        if previous > 0
    ]
    peak = values[0]
    drawdowns: list[float] = []
    for value in values:
        peak = max(peak, value)
        drawdowns.append(float((value - peak) / peak) if peak else 0.0)
    final = values[-1]
    total_return = float((final - initial_capital) / initial_capital)
    periods = max(1, len(values) - 1)
    annualized = (1 + total_return) ** (periods_per_year / periods) - 1 if total_return > -1 else -1.0
    volatility = statistics.pstdev(returns) * math.sqrt(periods_per_year) if len(returns) > 1 else None
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean / std * math.sqrt(periods_per_year) if std else None
    downside = [value for value in returns if value < 0]
    down_std = statistics.pstdev(downside) if len(downside) > 1 else 0.0
    sortino = mean / down_std * math.sqrt(periods_per_year) if down_std else None
    max_dd = min(drawdowns)
    calmar = annualized / abs(max_dd) if max_dd < 0 else None
    ordered = sorted(returns)
    var = ordered[max(0, math.floor(len(ordered) * 0.05))] if ordered else None
    tail = ordered[: max(1, math.floor(len(ordered) * 0.05))] if ordered else []
    cvar = statistics.fmean(tail) if tail else None
    sells = [trade for trade in trades if trade.side == "sell"]
    wins = [float(trade.realized_pnl) for trade in sells if trade.realized_pnl > 0]
    losses = [float(trade.realized_pnl) for trade in sells if trade.realized_pnl < 0]
    holding: list[float] = []
    open_trade: dict[str, datetime] = {}
    for trade in trades:
        if trade.side == "buy":
            open_trade.setdefault(trade.instrument_id, trade.executed_at)
        elif trade.instrument_id in open_trade:
            holding.append((trade.executed_at - open_trade.pop(trade.instrument_id)).total_seconds() / 86400)
    turnover = sum(float(trade.quantity * trade.price) for trade in trades) / float(initial_capital)
    return {
        "observations": len(values),
        "available": True,
        "initial_capital": text(initial_capital),
        "final_value": text(final),
        "total_return": round(total_return, 8),
        "annualized_return": round(annualized, 8),
        "cumulative_returns": [
            round(float((value / initial_capital) - 1), 8) for value in values
        ],
        "periodic_returns": [round(value, 8) for value in returns],
        "max_drawdown": round(max_dd, 8),
        "current_drawdown": round(drawdowns[-1], 8),
        "volatility": None if volatility is None else round(volatility, 8),
        "sharpe": None if sharpe is None else round(sharpe, 8),
        "sortino": None if sortino is None else round(sortino, 8),
        "calmar": None if calmar is None else round(calmar, 8),
        "var_95": None if var is None else round(var, 8),
        "cvar_95": None if cvar is None else round(cvar, 8),
        "trades": len(trades),
        "closed_trades": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(sells), 8) if sells else None,
        "average_win": round(statistics.fmean(wins), 8) if wins else None,
        "average_loss": round(statistics.fmean(losses), 8) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 8) if losses and sum(losses) else None,
        "average_holding_days": round(statistics.fmean(holding), 6) if holding else None,
        "turnover": round(turnover, 8),
        "trade_frequency": round(len(trades) / periods, 8),
        "total_fees": text(sum((trade.fees for trade in trades), Decimal("0"))),
        "total_slippage": text(sum((trade.slippage * trade.quantity for trade in trades), Decimal("0"))),
    }


def _returns_from_equity(equity: Sequence[Mapping[str, Any]]) -> list[tuple[datetime, float]]:
    rows: list[tuple[datetime, float]] = []
    previous: Decimal | None = None
    for row in equity:
        current = _dec(row["equity"], "equity")
        if previous is not None and previous > 0:
            rows.append((parse_time(row["at"]), float((current - previous) / previous)))
        previous = current
    return rows


def benchmark_metrics(
    bars: Sequence[HistoricalBar], *, initial_capital: Decimal
) -> dict[str, Any]:
    if len(bars) < 2:
        return {"available": False, "reason": "benchmark has fewer than two bars"}
    ordered = sorted(bars, key=lambda item: item.observed_at)
    closes = [bar.close for bar in ordered]
    values = [initial_capital * close / closes[0] for close in closes]
    metrics = calculate_metrics(
        [{"at": bar.observed_at.isoformat(), "equity": text(value)} for bar, value in zip(ordered, values)],
        [],
        initial_capital=initial_capital,
    )
    return {
        "available": True,
        "instrument_id": ordered[0].instrument_id,
        "observations": len(ordered),
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "volatility": metrics.get("volatility"),
        "sharpe": metrics.get("sharpe"),
        "basis": "buy-and-hold from the first available close; no fees or slippage",
    }


def regime_analysis(
    equity: Sequence[Mapping[str, Any]],
    benchmark: Sequence[HistoricalBar] | None = None,
) -> dict[str, Any]:
    returns = _returns_from_equity(equity)
    if len(returns) < 3:
        return {"available": False, "reason": "at least three return observations are required"}
    if benchmark and len(benchmark) >= 3:
        ordered = sorted(benchmark, key=lambda item: item.observed_at)
        closes = [float(item.close) for item in ordered]
        labels: list[tuple[datetime, str]] = []
        for index in range(1, len(ordered)):
            window = closes[max(0, index - 20): index + 1]
            trend = (window[-1] / window[0] - 1) if len(window) > 1 and window[0] else 0.0
            rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window)) if window[i - 1]]
            vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
            regime = "bull" if trend > 0.02 else "bear" if trend < -0.02 else "sideways"
            if vol > 0.02:
                regime += "_high_volatility"
            else:
                regime += "_low_volatility"
            labels.append((ordered[index].observed_at, regime))
        # Map each portfolio return to the latest benchmark label at or before it.
        mapped: list[tuple[str, float]] = []
        cursor = 0
        for moment, value in returns:
            while cursor + 1 < len(labels) and labels[cursor + 1][0] <= moment:
                cursor += 1
            mapped.append((labels[cursor][1] if labels else "unclassified", value))
    else:
        mapped = [
            ("unclassified", value) for _, value in returns
        ]
    groups: dict[str, list[float]] = {}
    for label, value in mapped:
        groups.setdefault(label, []).append(value)
    rows: dict[str, Any] = {}
    for label, values in sorted(groups.items()):
        equity_curve = [1.0]
        for value in values:
            equity_curve.append(equity_curve[-1] * (1 + value))
        peak = equity_curve[0]
        worst = 0.0
        for value in equity_curve:
            peak = max(peak, value)
            worst = min(worst, (value - peak) / peak if peak else 0.0)
        rows[label] = {
            "observations": len(values),
            "total_return": round(equity_curve[-1] - 1, 8),
            "win_rate": round(sum(1 for value in values if value > 0) / len(values), 8),
            "volatility": round(statistics.pstdev(values) * math.sqrt(252), 8) if len(values) > 1 else None,
            "max_drawdown": round(worst, 8),
        }
    return {
        "available": True,
        "basis": "benchmark 20-observation trend and volatility bands"
        if benchmark
        else "unclassified portfolio returns; no benchmark bars supplied",
        "regimes": rows,
        "warning": "regime labels are descriptive, not predictive; no period is selected as representative",
    }


class BacktestEngine:
    def __init__(
        self,
        store: BacktestStore,
        *,
        clock: Callable[[], datetime] | None = None,
        event_loader: Callable[[str, datetime], Sequence[Mapping[str, Any]]] | None = None,
        forecast_loader: Callable[[str, datetime], Sequence[Mapping[str, Any]]] | None = None,
        max_concurrency: int = 2,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._event_loader = event_loader
        self._forecast_loader = forecast_loader
        self._lock = threading.RLock()
        self._semaphore = threading.BoundedSemaphore(max(1, max_concurrency))
        self._cancelled: set[str] = set()
        self._last_error = ""

    @property
    def store(self) -> BacktestStore:
        return self._store

    def validate(
        self, config: BacktestConfig, bars: Iterable[HistoricalBar]
    ) -> tuple[list[HistoricalBar], list[str]]:
        clean, warnings = normalise_bars(bars, start=config.start, end=config.end)
        by_instrument: dict[str, list[HistoricalBar]] = {}
        for bar in clean:
            by_instrument.setdefault(bar.instrument_id, []).append(bar)
        warmup = [
            int(value)
            for key, value in config.strategy_config.items()
            if isinstance(value, int)
            and (key.endswith("_period") or key in {"lookback", "window"})
        ]
        required = max([2, *warmup])
        for instrument in config.instruments:
            series = by_instrument.get(instrument, [])
            if len(series) < required + 1:
                raise BacktestError(
                    f"insufficient historical data for {instrument}: {len(series)} bars; "
                    f"{required + 1} are required"
                )
        if config.benchmark:
            if config.benchmark not in by_instrument:
                warnings.append(
                    f"benchmark {config.benchmark} has no bars; benchmark metrics unavailable"
                )
        return clean, warnings

    def cancel(self, backtest_id: str) -> dict[str, Any]:
        status = self._store.status(backtest_id)
        if status is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        if status != "created":
            raise BacktestError(
                f"backtest {backtest_id} is {status} and cannot be cancelled"
            )
        # The flag is what a replay already in flight observes; the row is what
        # a replay that has not started yet observes.
        self._cancelled.add(backtest_id)
        result = self._store.cancel(backtest_id)
        if result is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        return result

    def run(
        self,
        config: BacktestConfig,
        bars: Iterable[HistoricalBar],
        *,
        created_at: datetime | None = None,
        force: bool = False,
    ) -> BacktestResult:
        created = created_at or self._clock()
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        clean, warnings = self.validate(config, bars)
        bars_hash = _hash([bar.to_dict() for bar in clean])
        if not force:
            existing = self._store.find_completed(config.config_hash, bars_hash)
            if existing is not None:
                result = self._store.result_model(existing)
                if result is not None:
                    return result
        backtest_id = self._store.create(config, created)
        acquired = self._semaphore.acquire(timeout=30)
        if not acquired:
            raise BacktestError("backtest concurrency limit is saturated")
        try:
            if backtest_id in self._cancelled:
                raise BacktestError(f"backtest {backtest_id} was cancelled")
            result = self._replay(
                backtest_id, config, clean, warnings, created, bars_hash
            )
            self._store.save(result)
            return result
        except Exception as exc:  # noqa: BLE001 - record the failure, then re-raise
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._store.fail(backtest_id, self._last_error)
            raise
        finally:
            self._semaphore.release()
            self._cancelled.discard(backtest_id)

    def execute(
        self, backtest_id: str, bars: Iterable[HistoricalBar], *, force: bool = False
    ) -> BacktestResult:
        """Run a configuration previously created by ``POST /market/backtests``."""
        status = self._store.status(backtest_id)
        if status is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        if status == "completed" and not force:
            existing = self._store.result_model(backtest_id)
            if existing is not None:
                return existing
        if status == "cancelled":
            raise BacktestError(f"backtest {backtest_id} was cancelled")
        if status == "failed":
            self._store.reopen(backtest_id)
        config = self._store.config(backtest_id)
        if config is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        acquired = self._semaphore.acquire(timeout=30)
        if not acquired:
            raise BacktestError("backtest concurrency limit is saturated")
        try:
            # Validation belongs inside the failure boundary: a dataset that is
            # too short is a failed attempt, not a row stuck at "created".
            clean, warnings = self.validate(config, bars)
            bars_hash = _hash([bar.to_dict() for bar in clean])
            result = self._replay(
                backtest_id, config, clean, warnings, self._clock(), bars_hash
            )
            self._store.save(result)
            return result
        except Exception as exc:  # noqa: BLE001 - record and re-raise
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._store.fail(backtest_id, self._last_error)
            raise
        finally:
            self._semaphore.release()
            self._cancelled.discard(backtest_id)

    def _replay(
        self,
        backtest_id: str,
        config: BacktestConfig,
        bars: Sequence[HistoricalBar],
        warnings: list[str],
        created: datetime,
        bars_hash: str,
    ) -> BacktestResult:
        replay_bars = [bar for bar in bars if bar.instrument_id in config.instruments]
        timeline = sorted(
            {(bar.observed_at, bar.instrument_id) for bar in replay_bars},
            key=lambda item: (item[0], item[1]),
        )
        by_key = {(bar.instrument_id, bar.observed_at): bar for bar in replay_bars}
        moment_instruments: dict[datetime, list[str]] = {}
        for moment, instrument in timeline:
            moment_instruments.setdefault(moment, []).append(instrument)
        schedule: dict[str, list[datetime]] = {item: [] for item in config.instruments}
        for moment in sorted(moment_instruments):
            for instrument in moment_instruments[moment]:
                schedule[instrument].append(moment)
        benchmark_rows = [
            bar for bar in bars if config.benchmark and bar.instrument_id == config.benchmark
        ]
        history: dict[str, list[HistoricalBar]] = {item: [] for item in config.instruments}
        last_close: dict[str, Decimal] = {}
        cash = config.initial_capital
        positions: dict[str, Position] = {
            item: Position(
                portfolio_id="backtest",
                instrument_id=item,
                quantity=Decimal("0"),
                average_cost=Decimal("0.00"),
                realized_pnl=Decimal("0.00"),
                sector="unknown",
                asset_class="equity",
                opened_at=created,
                updated_at=created,
                last_price=None,
                priced_at=None,
            )
            for item in config.instruments
        }
        trades: list[Trade] = []
        orders: list[SimulatedOrder] = []
        pending: dict[str, SimulatedOrder] = {}
        equity_by_moment: dict[datetime, dict[str, Any]] = {}
        rejections: list[dict[str, Any]] = []
        decisions: list[RiskDecision] = []
        signals: list[SignalRecord] = []
        peak_equity = config.initial_capital
        closed_bars = 0
        calendars: dict[str, Any] = {}
        for moment, instrument in timeline:
            if backtest_id in self._cancelled:
                raise BacktestError(f"backtest {backtest_id} was cancelled")
            bar = by_key[(instrument, moment)]
            assert bar.available_at is not None
            if bar.available_at > moment:
                warnings.append(
                    f"bar {instrument} {moment.isoformat()} was unavailable at decision time"
                )
                continue
            mic = _mic(instrument)
            if mic not in calendars:
                calendars[mic] = calendar_by_mic(mic)
            calendar = calendars[mic]
            if calendar is not None:
                local = calendar.local(bar.observed_at)
                if not calendar.coverage(bar.observed_at):
                    warnings.append(
                        f"calendar coverage for {mic} is not verified in {local.year}"
                    )
                if not calendar.is_trading_day(local.date()):
                    # A bar on a closed day is not a session: it cannot fill an
                    # order and it must not become a strategy observation.
                    closed_bars += 1
                    warnings.append(
                        f"exchange closed for {instrument} on {local.date().isoformat()}"
                    )
                    continue
            if not bar.tradable:
                warnings.append(f"trading halted for {instrument} at {moment.isoformat()}")
            order = pending.get(instrument)
            if order is not None:
                order, fresh_trades = self._execute_order(
                    backtest_id,
                    config,
                    order,
                    bar,
                    history[instrument],
                    positions,
                    cash,
                )
                cash = fresh_trades.pop("cash")
                trades.extend(fresh_trades.pop("trades"))
                for rejection in fresh_trades.pop("rejections"):
                    rejections.append(rejection)
                orders = [row for row in orders if row.order_id != order.order_id]
                if order.status in {"filled", "expired", "rejected", "cancelled"}:
                    pending.pop(instrument, None)
                orders.append(order)
            history[instrument].append(bar)
            last_close[instrument] = bar.close
            # NM-07's Position owns the mark; the replay only updates it.
            positions[instrument] = replace(
                positions[instrument],
                last_price=bar.close,
                priced_at=moment,
                updated_at=moment,
            )
            # Portfolio valuation uses every instrument's last known close; using
            # the current instrument's price for the others misstates equity in a
            # multi-instrument replay.
            positions_value = money(
                sum(
                    (_position_value(positions[i]) for i in config.instruments),
                    Decimal("0"),
                )
            )
            total_value = money(cash + positions_value)
            marked_peak = max(peak_equity, total_value)
            # One equity observation per timestamp: later instruments overwrite the
            # same moment, leaving the completed portfolio state for that moment.
            equity_by_moment[moment] = {
                "at": moment.isoformat(),
                "cash": text(money(cash)),
                "reserved": text(Decimal("0.00")),
                "positions_value": text(positions_value),
                "equity": text(total_value),
                "drawdown": text(
                    money((total_value - marked_peak) / marked_peak)
                    if marked_peak
                    else Decimal("0")
                ),
                "instrument_id": (
                    instrument if len(moment_instruments[moment]) == 1 else ""
                ),
            }
            if instrument == moment_instruments[moment][-1]:
                peak_equity = marked_peak
            if not bar.tradable:
                continue
            context = StrategyContext(
                instrument_id=instrument,
                portfolio_id="backtest",
                horizon=config.horizon,
                closes=tuple(float(item.close) for item in history[instrument]),
                highs=tuple(float(item.high) for item in history[instrument]),
                lows=tuple(float(item.low) for item in history[instrument]),
                volumes=tuple(float(item.volume) for item in history[instrument]),
                price=bar.close,
                observed_at=moment,
                now=moment,
                forecasts=tuple(
                    self._available_forecasts(instrument, moment, config)
                ),
                events=tuple(self._available_events(instrument, moment)),
                held=positions[instrument].quantity > 0,
                held_weight=money(
                    positions[instrument].quantity * bar.close / total_value
                    if total_value
                    else Decimal("0")
                ),
                cash=money(cash),
                total_value=total_value,
            )
            try:
                decision = EVALUATORS[config.strategy_catalog_id](
                    context, dict(config.strategy_config)
                )
            except Exception as exc:  # noqa: BLE001 - isolate one instrument
                warnings.append(
                    f"strategy failed for {instrument} at {moment.isoformat()}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if decision.signal_type not in _ACTIONABLE:
                continue
            signals.append(
                SignalRecord(
                    signal_id=execution_id(
                        "bt_sig", backtest_id, instrument, moment.isoformat(), decision.signal_type.value
                    ),
                    instrument_id=instrument,
                    at=moment,
                    signal_type=decision.signal_type.value,
                    confidence=decision.confidence,
                    price=bar.close,
                    reason=decision.explanation,
                )
            )
            side = OrderSide.BUY if decision.signal_type is SignalType.BUY else OrderSide.SELL
            if instrument in pending:
                rejections.append(
                    {
                        "instrument_id": instrument,
                        "at": moment.isoformat(),
                        "reason": "an order is already pending for this instrument",
                    }
                )
                continue
            if side is OrderSide.SELL and positions[instrument].quantity <= 0:
                rejections.append(
                    {
                        "instrument_id": instrument,
                        "at": moment.isoformat(),
                        "reason": "no position to sell",
                    }
                )
                continue
            qty = (
                quantity(min(config.order_notional, cash) / bar.close)
                if side is OrderSide.BUY
                else positions[instrument].quantity
            )
            notional = money(qty * bar.close)
            risk = self._risk_decision(
                config,
                instrument,
                "buy" if side is OrderSide.BUY else "sell",
                notional,
                moment,
                positions,
                cash,
                total_value,
                peak_equity,
                volatility_pct=_volatility(
                    [item.close for item in history[instrument]]
                ),
                liquidity=money(
                    (
                        history[instrument][-2].volume
                        if len(history[instrument]) > 1
                        else Decimal("0")
                    )
                    * bar.close
                ),
            )
            decisions.append(risk)
            if risk.outcome != "approved":
                rejections.append(risk.to_dict())
                continue
            order = self._new_order(
                backtest_id,
                config,
                instrument,
                side,
                qty,
                bar.close,
                moment,
                decision.explanation,
                expires_at=_expiry_time(
                    schedule[instrument], moment, config.order_ttl_bars
                ),
            )
            pending[instrument] = order
            orders.append(order)
        # Expire anything still pending at the end of the data.
        for order in list(pending.values()):
            expired = replace(
                order,
                status="expired",
                remaining_quantity=order.remaining_quantity,
                rejection_reason="order reached the end of the historical replay",
            )
            orders = [row for row in orders if row.order_id != order.order_id]
            orders.append(expired)
        pending.clear()
        if not equity_by_moment:
            raise BacktestError(
                "no bars fall on a trading session for the configured instruments"
            )
        equity = [equity_by_moment[key] for key in sorted(equity_by_moment)]
        final_value = money(
            cash
            + sum(
                (_position_value(positions[i]) for i in config.instruments),
                Decimal("0"),
            )
        )
        metrics = calculate_metrics(
            [{"at": config.start.isoformat(), "equity": text(config.initial_capital)}, *equity],
            trades,
            initial_capital=config.initial_capital,
        )
        metrics.update(
            {
                "rejections": len(rejections),
                "rejection_rate": round(
                    len(rejections) / max(1, len(trades) + len(rejections)), 8
                ),
                "orders": len(orders),
                "filled_orders": sum(1 for row in orders if row.status == "filled"),
                "partial_orders": sum(1 for row in orders if row.status == "partially_filled"),
                "unfilled_orders": sum(1 for row in orders if row.status in {"open", "expired"}),
                "fill_rate": round(
                    sum(1 for row in orders if row.filled_quantity > 0)
                    / max(1, len(orders)),
                    8,
                ),
                "partial_fill_rate": round(
                    sum(1 for row in orders if row.status == "partially_filled")
                    / max(1, len(orders)),
                    8,
                ),
                "benchmark": config.benchmark,
                "data_limited": len(equity) < 2,
            }
        )
        benchmark = benchmark_metrics(benchmark_rows, initial_capital=config.initial_capital)
        if benchmark.get("available"):
            benchmark["excess_return"] = round(
                float(metrics.get("total_return") or 0) - float(benchmark.get("total_return") or 0),
                8,
            )
        robustness = regime_analysis(equity, benchmark_rows) if benchmark_rows else regime_analysis(equity)
        data_quality = {
            "bars": len(replay_bars),
            "benchmark_bars": len(benchmark_rows),
            "instruments": len(config.instruments),
            "duplicates_removed": sum(1 for item in warnings if item.startswith("duplicate")),
            "unavailable_bars": sum(
                1 for item in warnings if "was unavailable at decision time" in item
            ),
            "halted_bars": sum(1 for item in warnings if item.startswith("trading halted")),
            "closed_bars": closed_bars,
            "calendar_enforced": True,
            "calendar_coverage": {
                item: calendars[item] is not None for item in sorted(calendars)
            },
            "partial_bars_rejected": 0,
            "sources": sorted({bar.source for bar in replay_bars}),
            "timezones": sorted({str(bar.observed_at.tzinfo) for bar in replay_bars}),
            "dataset_id": config.dataset_id,
            "dataset_version": config.dataset_version,
            "bars_hash": bars_hash,
            "survivorship_bias": (
                "the universe is the caller-selected instrument list; historical "
                "membership, delistings and suspensions are not reconstructed"
            ),
            "corporate_actions": (
                "adjusted bars are accepted only when the caller marks them adjusted; "
                "no corporate-action transformation is performed"
            ),
        }
        repro_payload = {
            "config_hash": config.config_hash,
            "bars_hash": bars_hash,
            "software": {
                "version": SOFTWARE_VERSION,
                "strategy_version": config.strategy_version,
                "execution_config_version": config.execution_config.version,
                "risk_policy_version": config.risk_policy_version,
                "python": platform.python_version(),
                "platform": sys.platform,
            },
            "trades": [_trade_repro(trade.to_dict()) for trade in trades],
            "orders": [_order_repro(order.to_dict()) for order in orders],
            "equity": equity,
            "metrics": metrics,
            "benchmark": benchmark,
            "robustness": robustness,
            "risk_decisions": [_decision_repro(row.to_dict()) for row in decisions],
            "signals": [_signal_repro(row.to_dict()) for row in signals],
        }
        result = BacktestResult(
            backtest_id=backtest_id,
            config=config,
            status="completed",
            trades=trades,
            orders=orders,
            equity=equity,
            metrics=metrics,
            benchmark=benchmark,
            robustness=robustness,
            warnings=warnings,
            data_quality=data_quality,
            risk_decisions=decisions,
            signals=signals,
            bars=list(bars),
            repro_hash=_hash(repro_payload),
            created_at=created,
        )
        return result

    def _new_order(
        self,
        backtest_id: str,
        config: BacktestConfig,
        instrument: str,
        side: OrderSide,
        qty: Decimal,
        close: Decimal,
        moment: datetime,
        reason: str,
        *,
        expires_at: datetime,
    ) -> SimulatedOrder:
        order_type = OrderType(config.order_type)
        limit = None
        trigger = None
        if order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
            offset = close * config.limit_offset_bps / Decimal("10000")
            limit = money(close - offset if side is OrderSide.BUY else close + offset)
        if order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            offset = close * config.stop_offset_bps / Decimal("10000")
            trigger = money(close + offset if side is OrderSide.BUY else close - offset)
        return SimulatedOrder(
            order_id=execution_id(
                "bt_order", backtest_id, instrument, moment.isoformat(), side.value
            ),
            backtest_id=backtest_id,
            instrument_id=instrument,
            side=side,
            order_type=order_type,
            requested_quantity=qty,
            filled_quantity=Decimal("0"),
            remaining_quantity=qty,
            decision_at=moment,
            expires_at=expires_at,
            status="open",
            limit_price=limit,
            trigger_price=trigger,
            reason=reason,
        )

    def _execute_order(
        self,
        backtest_id: str,
        config: BacktestConfig,
        order: SimulatedOrder,
        bar: HistoricalBar,
        history: Sequence[HistoricalBar],
        positions: dict[str, Position],
        cash: Decimal,
    ) -> tuple[SimulatedOrder, dict[str, Any]]:
        trades: list[Trade] = []
        rejections: list[dict[str, Any]] = []
        if not bar.tradable:
            return order, {"cash": cash, "trades": trades, "rejections": rejections}
        if bar.observed_at > order.expires_at:
            return (
                replace(order, status="expired", rejection_reason="order expired"),
                {"cash": cash, "trades": trades, "rejections": rejections},
            )
        base = bar.open
        triggered = True
        if order.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            assert order.trigger_price is not None
            triggered = (
                bar.high >= order.trigger_price
                if order.side is OrderSide.BUY
                else bar.low <= order.trigger_price
            )
            if triggered:
                base = (
                    max(bar.open, order.trigger_price)
                    if order.side is OrderSide.BUY
                    else min(bar.open, order.trigger_price)
                )
        if order.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT} and triggered:
            assert order.limit_price is not None
            if order.side is OrderSide.BUY:
                if bar.low > order.limit_price:
                    triggered = False
                else:
                    base = min(bar.open, order.limit_price)
            else:
                if bar.high < order.limit_price:
                    triggered = False
                else:
                    base = max(bar.open, order.limit_price)
        if not triggered:
            return order, {"cash": cash, "trades": trades, "rejections": rejections}
        snapshot = MarketSnapshot(
            instrument_id=bar.instrument_id,
            reference_price=money(base),
            observed_at=bar.observed_at,
            source=bar.source,
            # At the open, only the previously completed bar's volume is
            # observable. Using this bar's full-day volume would be look-ahead.
            volume=history[-1].volume if history else Decimal("0"),
            available_quantity=quantity(
                (history[-1].volume if history else Decimal("0"))
                * config.execution_config.participation_rate
            ),
            volatility_pct=_volatility([item.close for item in history]),
            trading_status="open",
        )
        available = snapshot.available_quantity
        if available <= 0:
            return order, {"cash": cash, "trades": trades, "rejections": rejections}
        fill_qty = min(order.remaining_quantity, available)
        if fill_qty <= 0:
            return order, {"cash": cash, "trades": trades, "rejections": rejections}
        if order.side is OrderSide.SELL:
            fill_qty = min(fill_qty, positions[bar.instrument_id].quantity)
        if fill_qty <= 0:
            rejections.append(
                {
                    "instrument_id": bar.instrument_id,
                    "at": bar.observed_at.isoformat(),
                    "reason": "no position available for sell",
                }
            )
            return replace(order, status="rejected", rejection_reason="no position available"), {
                "cash": cash,
                "trades": trades,
                "rejections": rejections,
            }
        try:
            price, slip_unit, slip_pct = execution_price(
                snapshot,
                side=order.side,
                order_type=order.order_type,
                quantity=fill_qty,
                config=config.execution_config,
                limit_price=order.limit_price,
            )
        except ExecutionPricingError as exc:
            rejections.append(
                {
                    "instrument_id": bar.instrument_id,
                    "at": bar.observed_at.isoformat(),
                    "reason": str(exc),
                }
            )
            return replace(order, status="rejected", rejection_reason=str(exc)), {
                "cash": cash,
                "trades": trades,
                "rejections": rejections,
            }
        if not exits_limit(side=order.side, price=price, limit_price=order.limit_price):
            return order, {"cash": cash, "trades": trades, "rejections": rejections}
        gross = money(fill_qty * price)
        fees = fee_breakdown(gross, order.side, config.execution_config)["total"]
        if order.side is OrderSide.BUY:
            cost = money(gross + fees)
            if cost > cash:
                # Fill only what available cash supports; fees are then rechecked.
                affordable = money(cash / price)
                fill_qty = min(fill_qty, quantity(affordable))
                gross = money(fill_qty * price)
                fees = fee_breakdown(gross, order.side, config.execution_config)["total"]
                cost = money(gross + fees)
            if fill_qty <= 0 or cost > cash:
                rejections.append(
                    {
                        "instrument_id": bar.instrument_id,
                        "at": bar.observed_at.isoformat(),
                        "reason": "insufficient simulated cash",
                    }
                )
                return replace(order, status="rejected", rejection_reason="insufficient simulated cash"), {
                    "cash": cash,
                    "trades": trades,
                    "rejections": rejections,
                }
            position = positions[bar.instrument_id]
            before = position.quantity
            basis = position.average_cost
            cash = money(cash - cost)
            after = quantity(before + fill_qty)
            positions[bar.instrument_id] = replace(
                position,
                quantity=after,
                average_cost=(
                    money((basis * before + cost) / after)
                    if after
                    else Decimal("0.00")
                ),
                last_price=price,
                priced_at=bar.observed_at,
                updated_at=bar.observed_at,
            )
            realized = Decimal("0.00")
        else:
            position = positions[bar.instrument_id]
            held = position.quantity
            fill_qty = min(fill_qty, held)
            proceeds = money(fill_qty * price - fees)
            cost_basis = money(position.average_cost * fill_qty)
            realized = money(proceeds - cost_basis)
            cash = money(cash + proceeds)
            after = quantity(held - fill_qty)
            positions[bar.instrument_id] = replace(
                position,
                quantity=after,
                average_cost=position.average_cost if after else Decimal("0.00"),
                realized_pnl=money(position.realized_pnl + realized),
                last_price=price,
                priced_at=bar.observed_at,
                updated_at=bar.observed_at,
            )
        partial = fill_qty < order.remaining_quantity
        trade = Trade(
            trade_id=execution_id(
                "bt_trade", backtest_id, order.order_id, bar.observed_at.isoformat(), text(fill_qty)
            ),
            backtest_id=backtest_id,
            order_id=order.order_id,
            instrument_id=bar.instrument_id,
            side=order.side.value,
            order_type=order.order_type.value,
            decision_at=order.decision_at,
            executed_at=bar.observed_at,
            quantity=fill_qty,
            price=price,
            reference_price=base,
            fees=fees,
            slippage=money(slip_unit),
            slippage_pct=slip_pct,
            cash_after=cash,
            position_after=positions[bar.instrument_id].quantity,
            realized_pnl=realized,
            reason=order.reason,
            partial=partial,
        )
        trades.append(trade)
        remaining = quantity(order.remaining_quantity - fill_qty)
        status = "partially_filled" if remaining > 0 else "filled"
        return replace(
            order,
            filled_quantity=quantity(order.filled_quantity + fill_qty),
            remaining_quantity=remaining,
            status=status,
        ), {"cash": cash, "trades": trades, "rejections": rejections}

    def _risk_decision(
        self,
        config: BacktestConfig,
        instrument: str,
        action: str,
        notional: Decimal,
        moment: datetime,
        positions: Mapping[str, Position],
        cash: Decimal,
        total_value: Decimal,
        peak_equity: Decimal,
        *,
        volatility_pct: Decimal | None = None,
        liquidity: Decimal | None = None,
    ) -> RiskDecision:
        policy = config.effective_risk
        buy = action == "buy"
        book = total_value or Decimal("1")
        weights = {item: _position_value(positions[item]) / book for item in positions}
        notional_weight = notional / book
        projected: list[dict[str, Any]] = []
        for item in positions:
            weight = weights.get(item, Decimal("0"))
            if item == instrument and buy:
                weight += notional_weight
            elif item == instrument:
                weight = max(Decimal("0"), weight - notional_weight)
            projected.append(
                {
                    "instrument_id": item,
                    "weight": text(money(weight)),
                    "sector": positions[item].sector or "unknown",
                }
            )
        cash_after = cash - notional if buy else cash + notional
        cash_weight = money(cash_after / book) if book else Decimal("0")
        drawdown = (
            (peak_equity - total_value) / peak_equity if peak_equity else Decimal("0")
        )
        # One snapshot shape, one evaluator: NM-08 owns every limit and its
        # outcome. The replay only maps the outcome to a simulated decision.
        snapshot = RiskSnapshot(
            positions=tuple(
                {
                    "instrument_id": item,
                    "weight": text(money(weights.get(item, Decimal("0")))),
                    "sector": positions[item].sector or "unknown",
                }
                for item in positions
            ),
            projected=tuple(projected),
            cash_weight=cash_weight,
            invested_fraction=money(max(Decimal("0"), Decimal("1") - cash_weight)),
            drawdown_pct=money(drawdown * Decimal("100")),
            volatility_pct=volatility_pct,
            turnover=Decimal("0"),
            data_age_hours=0.0,
            state_age_hours=0.0,
            liquidity=liquidity,
        )
        if not buy:
            # No entry gate may block a de-risking exit; the raw NM-08 evaluation
            # is still recorded for the audit trail.
            evaluation = evaluate_policy(
                policy, snapshot, action=action, adds_exposure=False
            )
            return self._decision(
                config,
                instrument,
                action,
                "approved",
                moment,
                notional,
                weights.get(instrument, Decimal("0")),
                Decimal("0"),
                "de-risking exit; NM-08 entry gates do not apply",
                policy_outcome=evaluation.outcome.value,
            )
        evaluation = evaluate_policy(
            policy, snapshot, action=action, adds_exposure=True
        )
        codes = {breach.code for breach in evaluation.breaches}
        raw = evaluation.outcome.value
        if raw == "approved":
            recorded = "approved"
        elif raw == "restricted" or codes == {"min_cash_fraction"}:
            recorded = "restricted"
        else:
            # A review condition or insufficient data cannot be approved by an
            # automated replay: there is no reviewer to escalate to.
            recorded = "rejected"
        reasons = [breach.message for breach in evaluation.breaches]
        reasons.extend(evaluation.insufficient)
        reasons.extend(evaluation.restrictions)
        reason = "; ".join(dict.fromkeys(reasons)) or (
            f"approved by NM-08 policy {policy.version}"
        )
        if raw == "requires_review":
            reason = "review required: " + reason
        projected_weight = next(
            (
                Decimal(str(row["weight"]))
                for row in projected
                if row["instrument_id"] == instrument
            ),
            Decimal("0"),
        )
        projected_gross = sum(
            (Decimal(str(row["weight"])) for row in projected), Decimal("0")
        )
        return self._decision(
            config,
            instrument,
            action,
            recorded,
            moment,
            notional,
            projected_weight,
            projected_gross,
            reason,
            policy_outcome=raw,
        )

    def _decision(
        self,
        config: BacktestConfig,
        instrument: str,
        action: str,
        outcome: str,
        moment: datetime,
        notional: Decimal,
        weight: Decimal,
        gross: Decimal,
        reason: str,
        *,
        policy_outcome: str = "",
    ) -> RiskDecision:
        return RiskDecision(
            decision_id=execution_id(
                "bt_risk", instrument, action, moment.isoformat(), outcome
            ),
            instrument_id=instrument,
            action=action,
            outcome=outcome,
            at=moment,
            policy_version=config.risk_policy_version,
            requested_notional=money(notional),
            projected_weight=money(weight),
            projected_gross=money(gross),
            reason=reason,
            policy_outcome=policy_outcome,
        )

    def _available_events(self, instrument: str, moment: datetime) -> list[dict[str, Any]]:
        if self._event_loader is None:
            return []
        try:
            rows = self._event_loader(instrument, moment)
        except Exception as exc:  # noqa: BLE001 - a source failure is not leakage
            self._last_error = f"event_loader: {type(exc).__name__}: {exc}"
            return []
        available: list[dict[str, Any]] = []
        for row in rows:
            stamp = row.get("published_at") or row.get("available_at") or row.get("created_at")
            try:
                if stamp is not None and parse_time(stamp) <= moment:
                    if row.get("instrument_id") and str(row.get("instrument_id")) != instrument:
                        continue
                    available.append(dict(row))
            except Exception:  # noqa: BLE001 - malformed source record is excluded
                continue
        return available

    def _available_forecasts(
        self, instrument: str, moment: datetime, config: BacktestConfig
    ) -> list[dict[str, Any]]:
        if self._forecast_loader is None:
            return []
        try:
            rows = self._forecast_loader(instrument, moment)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"forecast_loader: {type(exc).__name__}: {exc}"
            return []
        available: list[dict[str, Any]] = []
        for row in rows:
            created = row.get("created_at")
            cutoff = row.get("data_cutoff") or created
            model = str(row.get("model_version") or row.get("model_id") or "")
            try:
                if created is None or parse_time(created) > moment:
                    continue
                if cutoff is not None and parse_time(cutoff) > moment:
                    continue
                if config.forecast_model_version and model != config.forecast_model_version:
                    continue
                if str(row.get("status") or "").lower() in {"failed", "suspended", "degraded"}:
                    continue
                available.append(dict(row))
            except Exception:  # noqa: BLE001
                continue
        return available

    def verify(self, backtest_id: str) -> dict[str, Any]:
        payload = self._store.result(backtest_id)
        if payload is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        result = self._store.result_model(backtest_id)
        if result is None:
            raise BacktestError(f"backtest {backtest_id!r} has no result")
        try:
            replayed = self._replay(
                result.backtest_id,
                result.config,
                result.bars,
                list(result.warnings),
                result.created_at,
                result.data_quality.get("bars_hash", ""),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "backtest_id": backtest_id,
                "error": f"replay failed: {type(exc).__name__}: {exc}",
            }
        return {
            "ok": replayed.repro_hash == result.repro_hash,
            "backtest_id": backtest_id,
            "config_hash": result.config.config_hash,
            "bars_hash": result.data_quality.get("bars_hash"),
            "stored_repro_hash": result.repro_hash,
            "replayed_repro_hash": replayed.repro_hash,
            "verification": "full deterministic replay from persisted point-in-time bars",
        }

    def walk_forward(
        self,
        config: BacktestConfig,
        bars: Sequence[HistoricalBar],
        *,
        train_bars: int,
        test_bars: int,
        max_folds: int = 10,
    ) -> dict[str, Any]:
        """Sequential out-of-sample folds: train history, then measure the test slice.

        Each fold is a real replay whose configuration starts at the training
        window, so the strategy is warmed on history that precedes the test
        slice. Metrics are computed on the test slice only.
        """
        if train_bars < 2 or test_bars < 1:
            raise BacktestError(
                "walk-forward needs at least 2 training bars and 1 test bar"
            )
        clean, warnings = self.validate(config, bars)
        moments = sorted(
            {
                bar.observed_at
                for bar in clean
                if bar.instrument_id in config.instruments
            }
        )
        folds: list[dict[str, Any]] = []
        index = 0
        while (
            index + train_bars + test_bars <= len(moments)
            and len(folds) < max(1, max_folds)
        ):
            train_start = moments[index]
            test_start = moments[index + train_bars]
            test_end = moments[index + train_bars + test_bars - 1]
            window = [
                bar
                for bar in clean
                if train_start <= bar.observed_at <= test_end
            ]
            fold_config = replace(
                config,
                name=f"{config.name} fold {len(folds) + 1}",
                start=train_start,
                end=test_end,
            )
            try:
                result = self.run(fold_config, window, force=True)
            except BacktestError as exc:
                folds.append(
                    {"fold": len(folds) + 1, "skipped": True, "reason": str(exc)}
                )
                index += test_bars
                continue
            slice_rows = [
                row for row in result.equity if parse_time(row["at"]) >= test_start
            ]
            slice_trades = [
                trade for trade in result.trades if trade.executed_at >= test_start
            ]
            if len(slice_rows) < 2:
                folds.append(
                    {
                        "fold": len(folds) + 1,
                        "skipped": True,
                        "reason": "test slice has fewer than two observations",
                    }
                )
            else:
                start_value = Decimal(str(slice_rows[0]["equity"]))
                test_metrics = calculate_metrics(
                    slice_rows, slice_trades, initial_capital=start_value
                )
                folds.append(
                    {
                        "fold": len(folds) + 1,
                        "train_start": train_start.isoformat(),
                        "test_start": test_start.isoformat(),
                        "test_end": test_end.isoformat(),
                        "train_bars": train_bars,
                        "test_bars": test_bars,
                        "out_of_sample": True,
                        "total_return": test_metrics.get("total_return"),
                        "max_drawdown": test_metrics.get("max_drawdown"),
                        "sharpe": test_metrics.get("sharpe"),
                        "trades": test_metrics.get("trades"),
                        "backtest_id": result.backtest_id,
                    }
                )
            index += test_bars
        measured = [row for row in folds if not row.get("skipped")]
        returns = [
            float(row["total_return"])
            for row in measured
            if row.get("total_return") is not None
        ]
        return {
            "ok": True,
            "base_config_hash": config.config_hash,
            "train_bars": train_bars,
            "test_bars": test_bars,
            "folds": folds,
            "count": len(measured),
            "skipped": sum(1 for row in folds if row.get("skipped")),
            "mean_test_return": (
                round(statistics.fmean(returns), 8) if returns else None
            ),
            "positive_folds": sum(1 for value in returns if value > 0),
            "consistency": (
                round(sum(1 for value in returns if value > 0) / len(returns), 8)
                if returns
                else None
            ),
            "warning": (
                "sequential folds share one instrument history; each fold's test "
                "window is measured after its own training history and is not a "
                "promise of future performance"
            ),
            "warnings": warnings,
        }

    def sensitivity(
        self,
        base: BacktestConfig,
        variants: Sequence[BacktestConfig],
        bars: Sequence[HistoricalBar],
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for variant in variants:
            result = self.run(variant, bars, force=True)
            rows.append(
                {
                    "name": variant.name,
                    "config_hash": variant.config_hash,
                    "total_return": result.metrics.get("total_return"),
                    "max_drawdown": result.metrics.get("max_drawdown"),
                    "sharpe": result.metrics.get("sharpe"),
                    "trades": result.metrics.get("trades"),
                    "out_of_sample": False,
                }
            )
        return {
            "ok": True,
            "base_config_hash": base.config_hash,
            "variants": rows,
            "count": len(rows),
            "warning": "these runs share one historical window; no result is claimed as out-of-sample",
        }


def compare(results: Sequence[BacktestResult]) -> dict[str, Any]:
    if len(results) < 2:
        raise BacktestError("comparison needs at least two completed backtests")
    rows = []
    for result in results:
        rows.append(
            {
                "backtest_id": result.backtest_id,
                "name": result.config.name,
                "strategy_catalog_id": result.config.strategy_catalog_id,
                "strategy_version": result.config.strategy_version,
                "total_return": result.metrics.get("total_return"),
                "max_drawdown": result.metrics.get("max_drawdown"),
                "sharpe": result.metrics.get("sharpe"),
                "sortino": result.metrics.get("sortino"),
                "trades": result.metrics.get("trades"),
                "total_fees": result.metrics.get("total_fees"),
                "total_slippage": result.metrics.get("total_slippage"),
                "benchmark_excess": result.benchmark.get("excess_return"),
                "data_quality": result.data_quality,
                "warnings": list(result.warnings),
            }
        )
    return {
        "ok": True,
        "count": len(rows),
        "results": rows,
        "ranking": "not provided: compare multiple metrics and data-quality limitations, not final value alone",
    }
