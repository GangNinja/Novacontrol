"""Versioned PI-14 reward and policy-evaluation contracts.

These objects describe experimental, historical-simulation measurements.  They
carry no portfolio id, order id, wallet id, autonomy handle or production
fill reference.  The reward engine reads a PI-13 transition and returns a
reproducible attribution; it never mutates the simulated book.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from novacontrol.novamarket.rl.models import RLEnvironmentConfig, RLValidationError, parse_time

REWARD_PHASE = "PI-14"
REWARD_CONTRACT_VERSION = "pi14-reward-v1"
REWARD_CONFIG_SCHEMA_VERSION = "pi14-reward-config-v1"
REWARD_RESULT_SCHEMA_VERSION = "pi14-reward-result-v1"
POLICY_EVALUATION_SCHEMA_VERSION = "pi14-policy-evaluation-v1"
MAX_REWARD_CONFIGS = 100
MAX_REWARD_RESULTS = 200_000
MAX_POLICY_EVALUATIONS = 1_000
MAX_POLICY_COMPARISONS = 1_000
MAX_EVALUATION_SPLITS = 20
MAX_EVALUATION_POLICIES = 8
MAX_WALK_FORWARD_WINDOWS = 200


class RewardValidationError(ValueError):
    """A reward configuration or policy-evaluation request is invalid."""


class RewardError(RuntimeError):
    """The reward or policy-evaluation layer cannot complete the request."""


class PolicyEvaluationError(RewardError):
    """A policy evaluation could not be completed safely."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise RewardValidationError(f"{name} must be numeric")
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise RewardValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise RewardValidationError(f"{name} must be finite")
    return number


def _decimal(value: object, name: str, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RewardValidationError(f"{name} must be numeric") from exc
    if not number.is_finite():
        raise RewardValidationError(f"{name} must be finite")
    return number


def canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RewardValidationError(f"{name} must be an object")
    return dict(value)


def _sequence(value: object, name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise RewardValidationError(f"{name} must be a sequence")
    return tuple(str(item) for item in value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise RewardValidationError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise RewardValidationError(f"{name} must be an integer")
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise RewardValidationError(f"{name} must be an integer") from exc


DEFAULT_COMPONENTS: tuple[str, ...] = (
    "profitability",
    "risk",
    "transaction_cost",
    "decision_quality",
    "forecast_utilization",
    "discipline",
    "portfolio_management",
    "execution_quality",
)

#: Explicitly experimental defaults, not financially validated weights.
DEFAULT_WEIGHTS: dict[str, float] = {
    "profitability": 0.20,
    "risk": 0.15,
    "transaction_cost": 0.10,
    "decision_quality": 0.15,
    "forecast_utilization": 0.10,
    "discipline": 0.10,
    "portfolio_management": 0.10,
    "execution_quality": 0.10,
}

DEFAULT_SCALES: dict[str, float] = {
    "profitability": 0.02,
    "risk": 0.20,
    "transaction_cost": 0.01,
    "decision_quality": 1.0,
    "forecast_utilization": 1.0,
    "discipline": 1.0,
    "portfolio_management": 1.0,
    "execution_quality": 1.0,
}

DEFAULT_PENALTIES: dict[str, float] = {
    "constraint_violation": 0.25,
    "unfilled_trade": 0.10,
    "repeated_trade": 0.05,
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "risk_exposure_high": 0.90,
    "risk_concentration_high": 0.25,
    "risk_drawdown_high": 0.10,
    "risk_volatility_high_pct": 25.0,
    "decision_full_exposure": 0.90,
    "decision_weak_forecast": 0.40,
    "discipline_lookback_trades": 3.0,
    "discipline_repeated_penalty": 0.50,
    "portfolio_drawdown_high": 0.10,
    "portfolio_concentration_high": 0.25,
    "portfolio_exposure_change_high": 0.50,
}


@dataclass(frozen=True, slots=True)
class RewardConfig:
    """One immutable, versioned component aggregation policy."""

    name: str
    version: str = "experimental-1"
    components: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    enabled: tuple[str, ...] = DEFAULT_COMPONENTS
    normalization: str = "bounded"
    bounds: tuple[float, float] = (-1.0, 1.0)
    scales: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_SCALES))
    thresholds: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    missing_policy: str = "renormalize"
    penalties: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_PENALTIES))
    experimental: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        version = str(self.version or "").strip()
        if not name or not version:
            raise RewardValidationError("reward configuration name and version are required")
        components = _mapping(self.components, "components")
        unknown = sorted(set(components) - set(DEFAULT_COMPONENTS))
        if unknown:
            raise RewardValidationError("unknown reward component(s): " + ", ".join(unknown))
        weights = {key: _finite(value, f"components.{key}") for key, value in components.items()}
        if any(abs(value) > 10 for value in weights.values()):
            raise RewardValidationError("component weights must be within [-10, 10]")
        enabled = _sequence(self.enabled, "enabled")
        unknown_enabled = sorted(set(enabled) - set(DEFAULT_COMPONENTS))
        if unknown_enabled:
            raise RewardValidationError(
                "unknown enabled component(s): " + ", ".join(unknown_enabled)
            )
        if not enabled:
            raise RewardValidationError("at least one reward component must be enabled")
        missing_weights = sorted(set(enabled) - set(weights))
        if missing_weights:
            raise RewardValidationError(
                "enabled component(s) have no weight: " + ", ".join(missing_weights)
            )
        normalization = str(self.normalization or "").strip().lower()
        if normalization not in {"bounded", "none"}:
            raise RewardValidationError("normalization must be 'bounded' or 'none'")
        if not isinstance(self.bounds, (tuple, list)) or len(self.bounds) != 2:
            raise RewardValidationError("bounds must be a (minimum, maximum) pair")
        low = _finite(self.bounds[0], "bounds.minimum")
        high = _finite(self.bounds[1], "bounds.maximum")
        if not low < high:
            raise RewardValidationError("bounds.minimum must be below bounds.maximum")
        scales = _mapping(self.scales, "scales")
        unknown_scales = sorted(set(scales) - set(DEFAULT_COMPONENTS))
        if unknown_scales:
            raise RewardValidationError("unknown reward scale(s): " + ", ".join(unknown_scales))
        scale_values = {key: _finite(value, f"scales.{key}") for key, value in scales.items()}
        if any(value <= 0 for value in scale_values.values()):
            raise RewardValidationError("component scales must be positive")
        thresholds = _mapping(self.thresholds, "thresholds")
        unknown_thresholds = sorted(set(thresholds) - set(DEFAULT_THRESHOLDS))
        if unknown_thresholds:
            raise RewardValidationError("unknown threshold(s): " + ", ".join(unknown_thresholds))
        threshold_values = {
            **DEFAULT_THRESHOLDS,
            **{key: _finite(value, f"thresholds.{key}") for key, value in thresholds.items()},
        }
        if any(value <= 0 for value in threshold_values.values()):
            raise RewardValidationError("thresholds must be positive")
        missing_policy = str(self.missing_policy or "").strip().lower()
        if missing_policy not in {"renormalize", "zero"}:
            raise RewardValidationError("missing_policy must be 'renormalize' or 'zero'")
        penalties = _mapping(self.penalties, "penalties")
        unknown_penalties = sorted(set(penalties) - set(DEFAULT_PENALTIES))
        if unknown_penalties:
            raise RewardValidationError("unknown penalty(ies): " + ", ".join(unknown_penalties))
        penalty_values = {
            key: _finite(value, f"penalties.{key}") for key, value in penalties.items()
        }
        if any(value < 0 or value > 10 for value in penalty_values.values()):
            raise RewardValidationError("penalty weights must be within [0, 10]")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "components", dict(sorted(weights.items())))
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "normalization", normalization)
        object.__setattr__(self, "bounds", (low, high))
        object.__setattr__(self, "scales", dict(sorted(scale_values.items())))
        object.__setattr__(self, "thresholds", dict(sorted(threshold_values.items())))
        object.__setattr__(self, "missing_policy", missing_policy)
        object.__setattr__(self, "penalties", dict(sorted(penalty_values.items())))
        object.__setattr__(self, "experimental", bool(self.experimental))
        object.__setattr__(self, "description", str(self.description or ""))

    @property
    def config_id(self) -> str:
        return "rwc_" + canonical_hash(self.payload())[:20]

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "components": dict(self.components),
            "enabled": list(self.enabled),
            "normalization": self.normalization,
            "bounds": list(self.bounds),
            "scales": dict(self.scales),
            "thresholds": dict(self.thresholds),
            "missing_policy": self.missing_policy,
            "penalties": dict(self.penalties),
            "experimental": self.experimental,
            "description": self.description,
            "schema_version": REWARD_CONFIG_SCHEMA_VERSION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            **self.payload(),
            "experimental_status": "experimental" if self.experimental else "declared",
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RewardConfig:
        if not isinstance(payload, Mapping):
            raise RewardValidationError("reward configuration must be an object")
        values = dict(payload)
        for key in (
            "config_id",
            "config_hash",
            "experimental_status",
            "simulated",
            "advisory_only",
            "production_authority",
            "schema_version",
        ):
            values.pop(key, None)
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise RewardValidationError(
                "unknown reward configuration field(s): " + ", ".join(unknown)
            )
        if "bounds" in values:
            raw_bounds = values["bounds"]
            if not isinstance(raw_bounds, (tuple, list)) or len(raw_bounds) != 2:
                raise RewardValidationError("bounds must be a (minimum, maximum) pair")
            values["bounds"] = tuple(raw_bounds)
        if "enabled" in values:
            if isinstance(values["enabled"], str):
                raise RewardValidationError("enabled must be a sequence of component names")
            values["enabled"] = tuple(values["enabled"])
        return cls(**values)


def default_reward_config() -> RewardConfig:
    return RewardConfig(name="PI-14 experimental default")


def _value(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _decimal_value(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return number if number.is_finite() else Decimal("0")


@dataclass(frozen=True, slots=True)
class TransitionView:
    """Read-only, typed projection of one stored PI-13 transition."""

    transition_id: str
    episode_id: str
    environment_id: str
    sequence: int
    timestamp: datetime
    observation: Mapping[str, Any]
    next_observation: Mapping[str, Any]
    action: Mapping[str, Any]
    execution: Mapping[str, Any]
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    environment: Mapping[str, Any]
    dataset_version: str
    feature_set_version: str
    environment_version: str
    simulation_config_hash: str
    terminated: bool
    truncated: bool
    termination_reason: str
    error: str

    @classmethod
    def from_transition(
        cls, transition: Mapping[str, Any], *, environment: Mapping[str, Any] | None = None
    ) -> TransitionView:
        if not isinstance(transition, Mapping):
            raise RewardValidationError("transition must be an object")
        transition_id = str(transition.get("transition_id") or "").strip()
        episode_id = str(transition.get("episode_id") or "").strip()
        if not transition_id or not episode_id:
            raise RewardValidationError("transition_id and episode_id are required")
        timestamp_raw = transition.get("timestamp")
        if not timestamp_raw:
            raise RewardValidationError("transition timestamp is required")
        observation = _dict(transition.get("observation"))
        next_observation = _dict(transition.get("next_observation"))
        if not observation or not next_observation:
            raise RewardValidationError("transition must contain two observations")
        before = _dict(transition.get("portfolio_before"))
        after = _dict(transition.get("portfolio_after"))
        if not before or not after:
            raise RewardValidationError("transition must contain both portfolio states")
        return cls(
            transition_id=transition_id,
            episode_id=episode_id,
            environment_id=str(transition.get("environment_id") or ""),
            sequence=int(transition.get("sequence") or 0),
            timestamp=parse_time(timestamp_raw),
            observation=observation,
            next_observation=next_observation,
            action=_dict(transition.get("action")),
            execution=_dict(transition.get("execution")),
            before=before,
            after=after,
            environment=_dict(environment),
            dataset_version=str(transition.get("dataset_version") or ""),
            feature_set_version=str(transition.get("feature_set_version") or ""),
            environment_version=str(transition.get("environment_version") or ""),
            simulation_config_hash=str(transition.get("simulation_config_hash") or ""),
            terminated=bool(transition.get("terminated", False)),
            truncated=bool(transition.get("truncated", False)),
            termination_reason=str(transition.get("termination_reason") or ""),
            error=str(transition.get("error") or ""),
        )

    @property
    def action_type(self) -> str:
        return str(self.action.get("action_type") or "").lower()

    @property
    def is_trade(self) -> bool:
        return self.action_type in {"buy", "sell"}

    @property
    def execution_status(self) -> str:
        return str(self.execution.get("status") or "").lower()

    @property
    def executed(self) -> bool:
        return (
            self.execution_status in {"filled", "partial"}
            and self._dec(self.execution.get("quantity")) > 0
        )

    @property
    def forecast(self) -> dict[str, Any]:
        return _dict(self.observation.get("forecasts"))

    @property
    def market(self) -> dict[str, Any]:
        return _dict(self.observation.get("market"))

    @property
    def next_market(self) -> dict[str, Any]:
        return _dict(self.next_observation.get("market"))

    @property
    def features(self) -> dict[str, Any]:
        values = _dict(self.observation.get("features")).get("values")
        return _dict(values)

    @property
    def before_equity(self) -> float:
        return _value(self.before.get("equity"))

    @property
    def after_equity(self) -> float:
        return _value(self.after.get("equity"))

    @property
    def before_cash(self) -> float:
        return _value(self.before.get("cash"))

    @property
    def after_cash(self) -> float:
        return _value(self.after.get("cash"))

    @property
    def before_exposure(self) -> float:
        return _value(self.before.get("exposure"))

    @property
    def after_exposure(self) -> float:
        return _value(self.after.get("exposure"))

    @property
    def before_position_value(self) -> float:
        return self._position_value(self.before)

    @property
    def after_position_value(self) -> float:
        return self._position_value(self.after)

    @property
    def before_position_quantity(self) -> Decimal:
        positions = self.before.get("positions")
        if not isinstance(positions, list) or not positions:
            return Decimal("0")
        return self._dec(_dict(positions[0]).get("quantity"))

    @property
    def after_position_quantity(self) -> Decimal:
        positions = self.after.get("positions")
        if not isinstance(positions, list) or not positions:
            return Decimal("0")
        return self._dec(_dict(positions[0]).get("quantity"))

    @property
    def realized_delta(self) -> float:
        return _value(self.after.get("realized_pnl")) - _value(self.before.get("realized_pnl"))

    @property
    def unrealized_delta(self) -> float:
        return _value(self.after.get("unrealized_pnl")) - _value(self.before.get("unrealized_pnl"))

    @property
    def fees_delta(self) -> float:
        return max(0.0, _value(self.after.get("fees_paid")) - _value(self.before.get("fees_paid")))

    @property
    def slippage_cost(self) -> float:
        return max(0.0, _value(self.execution.get("slippage_amount")))

    @property
    def execution_at(self) -> datetime:
        """The simulated fill time, not the earlier decision timestamp."""
        raw = self.execution.get("at")
        if not raw:
            return self.timestamp
        try:
            return parse_time(raw, "execution.at")
        except RLValidationError:
            return self.timestamp

    @property
    def explicit_cost(self) -> float:
        return self.fees_delta + self.slippage_cost

    @property
    def net_return(self) -> float | None:
        if self.before_equity <= 0:
            return None
        return (self.after_equity - self.before_equity) / self.before_equity

    @property
    def gross_return(self) -> float | None:
        if self.before_equity <= 0:
            return None
        return (
            self.after_equity - self.before_equity + self.fees_delta + self.slippage_cost
        ) / self.before_equity

    @property
    def cash_weight(self) -> float | None:
        return self.after_cash / self.after_equity if self.after_equity > 0 else None

    @property
    def exposure_ratio(self) -> float:
        # RLPortfolioState.exposure is already a market-value/equity ratio.
        return max(0.0, min(1.0, self.after_exposure))

    @property
    def concentration(self) -> float:
        if self.after_equity <= 0:
            return 0.0
        return max(0.0, self.after_position_value / self.after_equity)

    @property
    def drawdown(self) -> float:
        return _value(self.after.get("drawdown"))

    @property
    def forecast_direction(self) -> str:
        return str(self.forecast.get("direction") or "unavailable").lower()

    @property
    def forecast_validity(self) -> str:
        return str(self.forecast.get("validity") or "unavailable").lower()

    @property
    def forecast_available(self) -> bool:
        return bool(self.forecast.get("available")) and self.forecast_validity not in {
            "invalid",
            "expired",
            "stale",
            "unavailable",
        }

    @property
    def expected_return(self) -> float | None:
        value = self.forecast.get("expected_return")
        if value is None:
            return None
        return _value(value)

    @property
    def uncertainty(self) -> str:
        return str(self.forecast.get("uncertainty") or "unknown").lower()

    @property
    def disagreement(self) -> float | None:
        value = self.forecast.get("disagreement")
        return None if value is None else _value(value)

    @property
    def forecast_age_hours(self) -> float | None:
        generated = self.forecast.get("generated_at")
        if not generated:
            return None
        try:
            at = parse_time(generated)
        except RLValidationError:
            return None
        return max(0.0, (self.timestamp - at).total_seconds() / 3600.0)

    @property
    def system_position(self) -> str:
        """The portfolio side before the action; PI-13 is long-only."""
        return "long" if self.before_position_quantity > 0 else "flat"

    @staticmethod
    def _position_value(portfolio: Mapping[str, Any]) -> float:
        positions = portfolio.get("positions")
        if not isinstance(positions, list):
            return 0.0
        total = 0.0
        for row in positions:
            total += _value(_dict(row).get("market_value"))
        return total

    @staticmethod
    def _dec(value: object) -> Decimal:
        return _decimal_value(value)


@dataclass(frozen=True, slots=True)
class RewardContext:
    transition: TransitionView
    history: tuple[TransitionView, ...] = ()
    risk_evidence: Mapping[str, Any] = field(default_factory=dict)
    thresholds: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))


@dataclass(frozen=True, slots=True)
class ComponentResult:
    name: str
    category: str
    raw_value: float | None
    available: bool = True
    reason: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    penalties: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "raw_value": None if self.raw_value is None else round(self.raw_value, 10),
            "available": self.available,
            "reason": self.reason,
            "metrics": dict(self.metrics),
            "penalties": dict(self.penalties),
        }


@dataclass(frozen=True, slots=True)
class RewardResult:
    reward_id: str
    transition_id: str
    episode_id: str
    config_id: str
    config_hash: str
    total: float
    components: Mapping[str, Any]
    attribution: tuple[dict[str, Any], ...]
    penalties: Mapping[str, float]
    missing_components: tuple[str, ...]
    excluded_components: tuple[str, ...]
    normalization: Mapping[str, Any]
    source: Mapping[str, Any]
    calculated_at: datetime
    result_hash: str
    experimental: bool = True
    simulated: bool = True
    advisory_only: bool = True
    production_authority: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reward_id": self.reward_id,
            "transition_id": self.transition_id,
            "episode_id": self.episode_id,
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            "total": round(self.total, 10),
            "components": dict(self.components),
            "attribution": [dict(item) for item in self.attribution],
            "penalties": dict(self.penalties),
            "missing_components": list(self.missing_components),
            "excluded_components": list(self.excluded_components),
            "normalization": dict(self.normalization),
            "source": dict(self.source),
            "calculated_at": self.calculated_at.isoformat(),
            "result_hash": self.result_hash,
            "schema_version": REWARD_RESULT_SCHEMA_VERSION,
            "experimental": self.experimental,
            "simulated": self.simulated,
            "advisory_only": self.advisory_only,
            "production_authority": self.production_authority,
        }


@dataclass(frozen=True, slots=True)
class SplitDefinition:
    label: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", str(self.label or "").strip())
        object.__setattr__(self, "start", parse_time(self.start, "split.start"))
        object.__setattr__(self, "end", parse_time(self.end, "split.end"))
        if not self.label:
            raise RewardValidationError("split label is required")
        if self.start >= self.end:
            raise RewardValidationError("split start must precede split end")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> SplitDefinition:
        if not isinstance(payload, Mapping):
            raise RewardValidationError("split must be an object")
        start = payload.get("start")
        end = payload.get("end")
        if not isinstance(start, (datetime, str)) or not isinstance(end, (datetime, str)):
            raise RewardValidationError("split start and end are required ISO timestamps")
        return cls(
            label=str(payload.get("label") or ""),
            start=parse_time(start, "split.start"),
            end=parse_time(end, "split.end"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """A chronological, identical-condition policy comparison request."""

    name: str
    environment: Mapping[str, Any]
    policies: tuple[str, ...]
    reward_config: Mapping[str, Any] = field(
        default_factory=lambda: default_reward_config().to_dict()
    )
    split: str = "all"
    splits: Mapping[str, SplitDefinition] = field(default_factory=dict)
    max_transitions: int = 5_000
    walk_forward: bool = False
    walk_forward_train: int = 252
    walk_forward_test: int = 63
    walk_forward_step: int = 63
    description: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise RewardValidationError("evaluation name is required")
        environment = _mapping(self.environment, "environment")
        try:
            RLEnvironmentConfig.from_mapping(environment)
        except RLValidationError as exc:
            raise RewardValidationError(f"invalid environment configuration: {exc}") from exc
        policies = _sequence(self.policies, "policies")
        if not policies:
            raise RewardValidationError("at least one policy is required")
        if len(set(policies)) != len(policies):
            raise RewardValidationError("policies must be unique")
        if len(policies) > MAX_EVALUATION_POLICIES:
            raise RewardValidationError(
                f"at most {MAX_EVALUATION_POLICIES} policies may be evaluated together"
            )
        splits_raw = self.splits
        if not isinstance(splits_raw, Mapping):
            raise RewardValidationError("splits must be an object")
        splits: dict[str, SplitDefinition] = {}
        for key, split_value in splits_raw.items():
            label = str(key).strip()
            if not label:
                raise RewardValidationError("split names cannot be empty")
            splits[label] = (
                split_value
                if isinstance(split_value, SplitDefinition)
                else SplitDefinition.from_mapping(split_value)
            )
        if len(splits) > MAX_EVALUATION_SPLITS:
            raise RewardValidationError(
                f"at most {MAX_EVALUATION_SPLITS} chronological splits are supported"
            )
        if splits:
            ordered = sorted(splits.values(), key=lambda item: item.start)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                # Split windows are inclusive at both ends; touching end/start is
                # therefore a shared bar and would leak one observation across splits.
                if previous.end >= current.start:
                    raise RewardValidationError("chronological splits must not overlap")
            by_label = {item.label.lower(): item for item in splits.values()}
            if (
                "train" in by_label
                and "test" in by_label
                and by_label["train"].start >= by_label["test"].start
            ):
                raise RewardValidationError(
                    "the training split must precede the test split chronologically"
                )
            if "validation" in by_label:
                if "train" in by_label and by_label["train"].start >= by_label["validation"].start:
                    raise RewardValidationError(
                        "the training split must precede the validation split chronologically"
                    )
                if "test" in by_label and by_label["validation"].start >= by_label["test"].start:
                    raise RewardValidationError(
                        "the validation split must precede the test split chronologically"
                    )
            if str(self.split) not in splits:
                raise RewardValidationError(f"split {self.split!r} is not defined")
        elif self.split != "all":
            raise RewardValidationError(
                "split must be 'all' when no chronological splits are supplied"
            )
        max_transitions = _integer(self.max_transitions, "max_transitions")
        if not 1 <= max_transitions <= 5_000:
            raise RewardValidationError("max_transitions must be within [1, 5000]")
        for field_name in ("walk_forward_train", "walk_forward_test", "walk_forward_step"):
            interval = _integer(getattr(self, field_name), field_name)
            if interval < 1:
                raise RewardValidationError(f"{field_name} must be positive")
            object.__setattr__(self, field_name, interval)
        reward_config = _mapping(self.reward_config, "reward_config")
        RewardConfig.from_mapping(reward_config)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "splits", dict(sorted(splits.items())))
        object.__setattr__(self, "reward_config", reward_config)
        object.__setattr__(self, "max_transitions", max_transitions)
        object.__setattr__(self, "walk_forward", bool(self.walk_forward))
        object.__setattr__(self, "description", str(self.description or ""))

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "environment": dict(self.environment),
            "policies": list(self.policies),
            "reward_config": dict(self.reward_config),
            "split": self.split,
            "splits": {key: value.to_dict() for key, value in self.splits.items()},
            "max_transitions": self.max_transitions,
            "walk_forward": self.walk_forward,
            "walk_forward_train": self.walk_forward_train,
            "walk_forward_test": self.walk_forward_test,
            "walk_forward_step": self.walk_forward_step,
            "description": self.description,
            "schema_version": POLICY_EVALUATION_SCHEMA_VERSION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_hash": self.config_hash,
            **self.payload(),
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> EvaluationConfig:
        if not isinstance(payload, Mapping):
            raise RewardValidationError("evaluation configuration must be an object")
        values = dict(payload)
        for key in (
            "config_hash",
            "simulated",
            "advisory_only",
            "production_authority",
            "schema_version",
        ):
            values.pop(key, None)
        if "splits" in values and isinstance(values["splits"], Mapping):
            values["splits"] = {
                str(key): (
                    value
                    if isinstance(value, SplitDefinition)
                    else SplitDefinition.from_mapping(value)
                )
                for key, value in values["splits"].items()
            }
        if "policies" in values:
            values["policies"] = tuple(values["policies"])
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise RewardValidationError(
                "unknown evaluation configuration field(s): " + ", ".join(unknown)
            )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class PolicyEvaluationResult:
    evaluation_id: str
    status: str
    config_hash: str
    split: str
    policy_name: str
    metrics: Mapping[str, Any]
    reward_summary: Mapping[str, Any]
    action_distribution: Mapping[str, int]
    transitions: int
    dataset_version: str
    environment_version: str
    repro_hash: str
    created_at: datetime
    warnings: tuple[str, ...] = ()
    error: str = ""
    evaluation_config: Mapping[str, Any] = field(default_factory=dict)
    dataset: Mapping[str, Any] = field(default_factory=dict)
    conditions: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "status": self.status,
            "config_hash": self.config_hash,
            "split": self.split,
            "policy_name": self.policy_name,
            "metrics": dict(self.metrics),
            "reward_summary": dict(self.reward_summary),
            "action_distribution": dict(self.action_distribution),
            "transitions": self.transitions,
            "dataset_version": self.dataset_version,
            "environment_version": self.environment_version,
            "repro_hash": self.repro_hash,
            "created_at": self.created_at.isoformat(),
            "warnings": list(self.warnings),
            "error": self.error,
            "evaluation_config": dict(self.evaluation_config),
            "dataset": dict(self.dataset),
            "conditions": dict(self.conditions),
            "schema_version": POLICY_EVALUATION_SCHEMA_VERSION,
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
            "claim": "historical simulation and experimental evaluation; not live performance",
        }


__all__ = [
    "ComponentResult",
    "DEFAULT_COMPONENTS",
    "DEFAULT_PENALTIES",
    "DEFAULT_SCALES",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_WEIGHTS",
    "EvaluationConfig",
    "MAX_POLICY_EVALUATIONS",
    "MAX_REWARD_CONFIGS",
    "MAX_REWARD_RESULTS",
    "POLICY_EVALUATION_SCHEMA_VERSION",
    "PolicyEvaluationError",
    "PolicyEvaluationResult",
    "REWARD_CONFIG_SCHEMA_VERSION",
    "REWARD_CONTRACT_VERSION",
    "REWARD_PHASE",
    "REWARD_RESULT_SCHEMA_VERSION",
    "RewardConfig",
    "RewardContext",
    "RewardError",
    "RewardResult",
    "RewardValidationError",
    "SplitDefinition",
    "TransitionView",
    "canonical_hash",
    "default_reward_config",
]
