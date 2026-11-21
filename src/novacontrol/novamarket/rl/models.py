"""Versioned contracts for the PI-13 isolated reinforcement-learning environment.

These contracts describe a simulated research environment only.  They carry no
portfolio id, order id, wallet id or execution-engine handle, so an RL action
cannot be mistaken for an NM-10 paper order or an NM-07 portfolio mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from novacontrol.novamarket.execution.config import ExecutionConfig

RL_PHASE = "PI-13"
RL_CONTRACT_VERSION = "pi13-rl-v1"
RL_ENVIRONMENT_VERSION = "pi13-rl-env-1.0"
RL_OBSERVATION_SCHEMA_VERSION = "pi13-observation-v1"
RL_ACTION_SCHEMA_VERSION = "pi13-action-v1"
RL_REWARD_INTERFACE_VERSION = "pi13-reward-interface-v1"
RL_EXPERIENCE_SCHEMA_VERSION = "pi13-experience-v1"
MAX_INSTRUMENTS = 1
MAX_BARS = 20_000
MAX_STEPS_CEILING = 5_000
MAX_EXPERIENCES_PER_EPISODE = 5_000
MAX_ACTIVE_ENVIRONMENTS = 4
MAX_ENVIRONMENTS = 100
MAX_STORED_EPISODES = 5_000


class RLValidationError(ValueError):
    """A caller supplied a configuration or action outside the RL contract."""


class RLEnvironmentError(RuntimeError):
    """The environment cannot advance safely."""


class RLEpisodeError(RLEnvironmentError):
    """The episode lifecycle does not permit this operation."""


class RLDataError(RLEnvironmentError):
    """Historical or observation data failed a point-in-time check."""


class RLStoreError(RuntimeError):
    """The isolated experience store refused or failed a write."""


class RLInvalidActionError(RLValidationError):
    """A syntactically valid action is outside the configured simulation."""


class ActionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    WAIT = "wait"
    NO_ACTION = "no_action"

    @property
    def is_trade(self) -> bool:
        return self in (ActionType.BUY, ActionType.SELL)


class EpisodeStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    TERMINATED = "terminated"
    TRUNCATED = "truncated"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TerminationReason(StrEnum):
    HISTORICAL_DATA_EXHAUSTED = "historical_data_exhausted"
    CONFIGURED_PERIOD_END = "configured_period_end"
    STEP_LIMIT = "step_limit"
    ENVIRONMENT_FAILURE = "environment_failure"
    CANCELLED = "cancelled"


class ExecutionStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    NOT_APPLICABLE = "not_applicable"
    NO_NEXT_BAR = "no_next_bar"


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RLValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_time(value: datetime | str, field_name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        return _aware(value, field_name)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RLValidationError(f"{field_name} is not an ISO timestamp") from exc
    return _aware(parsed, field_name)


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RLValidationError(f"{field_name} must be numeric") from exc
    if not number.is_finite():
        raise RLValidationError(f"{field_name} must be finite")
    return number


def money(value: Any) -> Decimal:
    return _decimal(value, "money").quantize(Decimal("0.01"))


def quantity(value: Any) -> Decimal:
    return _decimal(value, "quantity").quantize(Decimal("0.000001"))


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RLEnvironmentConfig:
    """One immutable, persisted environment definition.

    PI-13 deliberately starts with exactly one instrument per environment.  A
    reliable single-instrument environment is preferable to a multi-instrument
    portfolio whose cross-sectional timing has not been validated.
    """

    name: str
    dataset_id: str
    dataset_version: str
    instrument_id: str
    start: datetime
    end: datetime
    environment_id: str = ""
    environment_version: str = RL_ENVIRONMENT_VERSION
    initial_capital: Decimal = Decimal("100000.00")
    resolution: str = "1d"
    feature_profile: str = "swing"
    include_features: bool = True
    include_forecasts: bool = True
    max_steps: int = 252
    max_position_weight: Decimal = Decimal("0.25")
    max_order_notional: Decimal = Decimal("25000.00")
    max_gross_exposure: Decimal = Decimal("1.00")
    execution_config: Mapping[str, Any] = field(default_factory=dict)
    random_seed: int = 42
    reward_interface_version: str = RL_REWARD_INTERFACE_VERSION
    notes: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        dataset_id = str(self.dataset_id or "").strip()
        dataset_version = str(self.dataset_version or "").strip()
        instrument = str(self.instrument_id or "").strip().upper()
        environment_version = str(self.environment_version or "").strip()
        if not name:
            raise RLValidationError("environment name is required")
        if not dataset_id or not dataset_version:
            raise RLValidationError("dataset_id and dataset_version are required")
        if not instrument or ":" not in instrument:
            raise RLValidationError("instrument_id must be canonical MIC:SYMBOL")
        if not environment_version:
            raise RLValidationError("environment_version is required")
        if str(self.resolution).strip() != "1d":
            raise RLValidationError("PI-13 supports completed daily bars only")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "dataset_id", dataset_id)
        object.__setattr__(self, "dataset_version", dataset_version)
        object.__setattr__(self, "instrument_id", instrument)
        object.__setattr__(self, "environment_version", environment_version)
        object.__setattr__(self, "start", parse_time(self.start, "start"))
        object.__setattr__(self, "end", parse_time(self.end, "end"))
        if self.start >= self.end:
            raise RLValidationError("start must precede end")
        object.__setattr__(self, "initial_capital", money(self.initial_capital))
        object.__setattr__(
            self, "max_position_weight", _decimal(self.max_position_weight, "max_position_weight")
        )
        object.__setattr__(self, "max_order_notional", money(self.max_order_notional))
        object.__setattr__(
            self, "max_gross_exposure", _decimal(self.max_gross_exposure, "max_gross_exposure")
        )
        object.__setattr__(self, "max_steps", int(self.max_steps))
        object.__setattr__(self, "random_seed", int(self.random_seed))
        if self.initial_capital <= 0:
            raise RLValidationError("initial_capital must be positive")
        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise RLValidationError("max_position_weight must be within (0, 1]")
        if not Decimal("0") < self.max_gross_exposure <= Decimal("1"):
            raise RLValidationError("PI-13 is long-only with max_gross_exposure within (0, 1]")
        if self.max_order_notional <= 0:
            raise RLValidationError("max_order_notional must be positive")
        if not 1 <= self.max_steps <= MAX_STEPS_CEILING:
            raise RLValidationError(f"max_steps must be within [1, {MAX_STEPS_CEILING}]")
        if not str(self.feature_profile).strip():
            raise RLValidationError("feature_profile is required")
        if not isinstance(self.execution_config, Mapping):
            raise RLValidationError("execution_config must be an object")
        # Building this validates the NM-10 pricing/fee configuration without
        # using the execution engine or its stores.
        ExecutionConfig.from_mapping(dict(self.execution_config))
        object.__setattr__(self, "execution_config", dict(self.execution_config))
        object.__setattr__(self, "notes", str(self.notes or ""))

    @property
    def instruments(self) -> tuple[str, ...]:
        return (self.instrument_id,)

    @property
    def execution(self) -> ExecutionConfig:
        return ExecutionConfig.from_mapping(dict(self.execution_config))

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "environment_id": self.environment_id,
            "environment_version": self.environment_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "instrument_id": self.instrument_id,
            "instruments": list(self.instruments),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "initial_capital": decimal_text(self.initial_capital),
            "resolution": self.resolution,
            "feature_profile": self.feature_profile,
            "include_features": self.include_features,
            "include_forecasts": self.include_forecasts,
            "max_steps": self.max_steps,
            "max_position_weight": decimal_text(self.max_position_weight),
            "max_order_notional": decimal_text(self.max_order_notional),
            "max_gross_exposure": decimal_text(self.max_gross_exposure),
            "execution_config": dict(self.execution_config),
            "random_seed": self.random_seed,
            "reward_interface_version": self.reward_interface_version,
            "notes": self.notes,
            "contract_version": RL_CONTRACT_VERSION,
        }

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.payload())

    def with_environment_id(self, environment_id: str) -> RLEnvironmentConfig:
        value = str(environment_id or "").strip()
        if not value:
            raise RLValidationError("environment_id is required")
        return replace(self, environment_id=value)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.payload(),
            "config_hash": self.config_hash,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RLEnvironmentConfig:
        if not isinstance(payload, Mapping):
            raise RLValidationError("environment configuration must be an object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(
            set(payload)
            - known
            - {
                "instruments",
                "simulated",
                "advisory_only",
                "production_authority",
                "config_hash",
                "contract_version",
            }
        )
        if unknown:
            raise RLValidationError("unknown environment field(s): " + ", ".join(unknown))
        data = {key: payload[key] for key in known if key in payload}
        try:
            return cls(**data)
        except (TypeError, ValueError) as exc:
            # A payload missing required fields is a caller error and must not
            # surface as an internal server failure.
            raise RLValidationError(f"incomplete environment configuration: {exc}") from exc


@dataclass(frozen=True, slots=True)
class RLPositionState:
    instrument_id: str
    quantity: Decimal
    average_entry_price: Decimal
    last_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "quantity": decimal_text(self.quantity),
            "average_entry_price": decimal_text(self.average_entry_price),
            "last_price": decimal_text(self.last_price),
            "market_value": decimal_text(self.market_value),
            "unrealized_pnl": decimal_text(self.unrealized_pnl),
        }


@dataclass(frozen=True, slots=True)
class RLPortfolioState:
    initial_capital: Decimal
    cash: Decimal
    positions: tuple[RLPositionState, ...]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    equity: Decimal
    exposure: Decimal
    drawdown: Decimal
    peak_equity: Decimal

    @property
    def position(self) -> RLPositionState | None:
        return self.positions[0] if self.positions else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_capital": decimal_text(self.initial_capital),
            "cash": decimal_text(self.cash),
            "positions": [item.to_dict() for item in self.positions],
            "realized_pnl": decimal_text(self.realized_pnl),
            "unrealized_pnl": decimal_text(self.unrealized_pnl),
            "fees_paid": decimal_text(self.fees_paid),
            "equity": decimal_text(self.equity),
            "exposure": decimal_text(self.exposure),
            "drawdown": decimal_text(self.drawdown),
            "peak_equity": decimal_text(self.peak_equity),
            "currency": "INR",
            "nova_coin_equivalent": decimal_text(self.equity),
            "simulated": True,
            "production_book": False,
        }


@dataclass(frozen=True, slots=True)
class RLAction:
    action_type: ActionType | str
    instrument_id: str = ""
    quantity: Decimal | None = None
    notional: Decimal | None = None
    size_fraction: Decimal | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        try:
            action = ActionType(self.action_type)
        except ValueError as exc:
            raise RLValidationError(f"unsupported RL action {self.action_type!r}") from exc
        object.__setattr__(self, "action_type", action)
        object.__setattr__(self, "instrument_id", str(self.instrument_id or "").strip().upper())
        chosen = [
            name
            for name in ("quantity", "notional", "size_fraction")
            if getattr(self, name) is not None
        ]
        if len(chosen) > 1:
            raise RLValidationError("supply only one of quantity, notional or size_fraction")
        for name in chosen:
            value = _decimal(getattr(self, name), name)
            if value <= 0:
                raise RLValidationError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if action.is_trade and not self.instrument_id:
            raise RLValidationError(f"{action.value} requires instrument_id")
        if not action.is_trade and chosen:
            raise RLValidationError(f"{action.value} does not accept a size")
        object.__setattr__(self, "reason", str(self.reason or "")[:500])

    @property
    def kind(self) -> ActionType:
        return ActionType(self.action_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.kind.value,
            "instrument_id": self.instrument_id,
            "quantity": decimal_text(self.quantity) if self.quantity is not None else None,
            "notional": decimal_text(self.notional) if self.notional is not None else None,
            "size_fraction": decimal_text(self.size_fraction)
            if self.size_fraction is not None
            else None,
            "reason": self.reason,
            "schema_version": RL_ACTION_SCHEMA_VERSION,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RLAction:
        if not isinstance(payload, Mapping):
            raise RLValidationError("action must be an object")
        known = {
            "action_type",
            "instrument_id",
            "quantity",
            "notional",
            "size_fraction",
            "reason",
            "schema_version",
        }
        unknown = sorted(set(payload) - known)
        if unknown:
            raise RLValidationError("unknown action field(s): " + ", ".join(unknown))
        return cls(**{key: payload[key] for key in known if key in payload})


@dataclass(frozen=True, slots=True)
class RLObservation:
    observation_id: str
    environment_id: str
    episode_id: str
    timestamp: datetime
    instrument_id: str
    phase: str
    market: Mapping[str, Any]
    features: Mapping[str, Any]
    forecasts: Mapping[str, Any]
    portfolio: Mapping[str, Any]
    provenance: Mapping[str, Any]
    quality: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "timestamp": self.timestamp.isoformat(),
            "instrument_id": self.instrument_id,
            "phase": self.phase,
            "market": dict(self.market),
            "features": dict(self.features),
            "forecasts": dict(self.forecasts),
            "portfolio": dict(self.portfolio),
            "provenance": dict(self.provenance),
            "quality": dict(self.quality),
            "schema_version": RL_OBSERVATION_SCHEMA_VERSION,
            "simulated": True,
            "advisory_only": True,
            "production_book": False,
        }


@dataclass(frozen=True, slots=True)
class RLExecution:
    status: ExecutionStatus
    side: str
    quantity: Decimal
    reference_price: Decimal
    execution_price: Decimal
    notional: Decimal
    slippage_amount: Decimal
    fees: Decimal
    fee_components: Mapping[str, Decimal]
    at: datetime | None
    reason: str = ""
    source: str = "nm02_historical_bar"
    price_assumption: str = "next_bar_open_plus_nm10_slippage_model"

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ExecutionStatus(self.status))
        object.__setattr__(self, "quantity", quantity(self.quantity))
        object.__setattr__(self, "reference_price", money(self.reference_price))
        object.__setattr__(self, "execution_price", money(self.execution_price))
        object.__setattr__(self, "notional", money(self.notional))
        object.__setattr__(self, "slippage_amount", money(self.slippage_amount))
        object.__setattr__(self, "fees", money(self.fees))
        object.__setattr__(
            self,
            "fee_components",
            {key: money(value) for key, value in self.fee_components.items()},
        )
        if self.at is not None:
            object.__setattr__(self, "at", parse_time(self.at, "execution at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": ExecutionStatus(self.status).value,
            "side": self.side,
            "quantity": decimal_text(self.quantity),
            "reference_price": decimal_text(self.reference_price),
            "execution_price": decimal_text(self.execution_price),
            "notional": decimal_text(self.notional),
            "slippage_amount": decimal_text(self.slippage_amount),
            "fees": decimal_text(self.fees),
            "fee_components": {
                key: decimal_text(value) for key, value in self.fee_components.items()
            },
            "at": self.at.isoformat() if self.at else None,
            "reason": self.reason,
            "source": self.source,
            "price_assumption": self.price_assumption,
            "actual_historical_observation": True,
            "simulated_execution": True,
            "production_fill": False,
        }


@dataclass(frozen=True, slots=True)
class RLReward:
    total: Decimal
    components: Mapping[str, Any]
    policy_version: str = RL_REWARD_INTERFACE_VERSION
    interface_only: bool = True
    authoritative_formula: bool = False
    note: str = "PI-13 supplies reward components only; PI-14 owns any reward policy."

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": decimal_text(self.total),
            "components": dict(self.components),
            "policy_version": self.policy_version,
            "interface_only": self.interface_only,
            "authoritative_formula": self.authoritative_formula,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class RLTransition:
    transition_id: str
    environment_id: str
    episode_id: str
    sequence: int
    timestamp: datetime
    observation: Mapping[str, Any]
    action: Mapping[str, Any]
    execution: Mapping[str, Any]
    reward: Mapping[str, Any]
    next_observation: Mapping[str, Any]
    terminated: bool
    truncated: bool
    termination_reason: str
    dataset_version: str
    feature_set_version: str
    environment_version: str
    simulation_config_hash: str
    portfolio_before: Mapping[str, Any]
    portfolio_after: Mapping[str, Any]
    invalid_action: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "observation": dict(self.observation),
            "action": dict(self.action),
            "execution": dict(self.execution),
            "reward": dict(self.reward),
            "next_observation": dict(self.next_observation),
            "terminated": self.terminated,
            "truncated": self.truncated,
            "termination_reason": self.termination_reason,
            "dataset_version": self.dataset_version,
            "feature_set_version": self.feature_set_version,
            "environment_version": self.environment_version,
            "simulation_config_hash": self.simulation_config_hash,
            "portfolio_before": dict(self.portfolio_before),
            "portfolio_after": dict(self.portfolio_after),
            "invalid_action": self.invalid_action,
            "error": self.error,
            "schema_version": RL_EXPERIENCE_SCHEMA_VERSION,
            "simulated": True,
        }


@dataclass(frozen=True, slots=True)
class RLEpisode:
    episode_id: str
    environment_id: str
    status: EpisodeStatus
    created_at: datetime
    updated_at: datetime
    config: Mapping[str, Any]
    state: Mapping[str, Any]
    summary: Mapping[str, Any] = field(default_factory=dict)
    transition_count: int = 0
    invalid_actions: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EpisodeStatus(self.status))
        object.__setattr__(self, "created_at", parse_time(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", parse_time(self.updated_at, "updated_at"))
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "state", dict(self.state))
        object.__setattr__(self, "summary", dict(self.summary))

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "environment_id": self.environment_id,
            "status": EpisodeStatus(self.status).value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "config": dict(self.config),
            "state": dict(self.state),
            "summary": dict(self.summary),
            "transition_count": self.transition_count,
            "invalid_actions": self.invalid_actions,
            "error": self.error,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }


def is_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def sanitize_observation_number(value: Any) -> float | None:
    """Return a finite float or ``None``; never collapse a value to zero."""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ensure_sequence(value: Sequence[Any] | None) -> tuple[Any, ...]:
    return tuple(value or ())
