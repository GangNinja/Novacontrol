"""PI-18 portfolio-optimization contracts.

The optimizer is a research layer over authoritative NovaMarket state.  It
reads NM-07 portfolios, NM-02 bars, PI-04 features/regimes, PI-05..PI-10
forecasts and NM-08 reports, then persists *recommendations* only.  It has no
portfolio, ledger, risk-approval or execution handle.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

OPTIMIZATION_PHASE = "PI-18"
OPTIMIZATION_CONTRACT_VERSION = "pi18-portfolio-optimization-1.0"
OPTIMIZATION_SCHEMA_VERSION = 1
CONFIG_VERSION = "pi18-config-1.0"
SOLVER_VERSION = "pi18-numpy-projected-1.0"

MAX_CONFIGS = 500
MAX_RUNS = 1_000
MAX_BACKTESTS = 300
MAX_SCENARIOS_PER_RUN = 50
MAX_INSTRUMENTS = 50
MAX_CONCURRENT_RUNS = 2
DEFAULT_RUN_TIMEOUT_SECONDS = 60
MAX_RUN_TIMEOUT_SECONDS = 120
MIN_HISTORY_DEFAULT = 30
FORECAST_QUALITY_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
MAX_FORECASTS_PER_INSTRUMENT = 20
MAX_WEIGHT_SUM = 1.0


class OptimizationError(ValueError):
    """An optimization request is invalid."""


class OptimizationValidationError(OptimizationError):
    """Configuration, snapshot or market input failed validation."""


class OptimizationInfeasibleError(OptimizationError):
    """The configured constraints cannot be satisfied by any allocation.

    This is a distinct outcome from invalid input and from a solver defect: the
    inputs are well-formed, but the instrument, sector or liquidity caps leave
    no allocation that honours them.  It is reported as
    ``INFEASIBLE_CONSTRAINTS``, never as a validation or solver failure.
    """


class OptimizationNotFoundError(LookupError):
    """A requested PI-18 record does not exist."""


class OptimizationStateError(RuntimeError):
    """The record state does not permit the requested operation."""


class OptimizationResourceError(RuntimeError):
    """A bounded PI-18 limit would be exceeded."""


class OptimizationStoreError(RuntimeError):
    """The isolated PI-18 store could not complete the request."""


class OptimizationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_INPUT = "invalid_input"
    INFEASIBLE_CONSTRAINTS = "infeasible_constraints"
    SOLVER_FAILED = "solver_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset(
    {
        OptimizationStatus.COMPLETED,
        OptimizationStatus.COMPLETED_WITH_WARNINGS,
        OptimizationStatus.INSUFFICIENT_DATA,
        OptimizationStatus.INVALID_INPUT,
        OptimizationStatus.INFEASIBLE_CONSTRAINTS,
        OptimizationStatus.SOLVER_FAILED,
        OptimizationStatus.TIMED_OUT,
        OptimizationStatus.CANCELLED,
        OptimizationStatus.FAILED,
    }
)


class OptimizationObjective(StrEnum):
    MAX_EXPECTED_RETURN = "max_expected_return"
    MIN_VARIANCE = "min_variance"
    RISK_ADJUSTED = "risk_adjusted"
    MAX_SHARPE = "max_sharpe"
    MIN_DOWNSIDE_RISK = "min_downside_risk"
    DIVERSIFICATION = "diversification"
    MULTI_OBJECTIVE = "multi_objective"


class AllocationMethod(StrEnum):
    MEAN_VARIANCE = "mean_variance"
    MIN_VARIANCE = "min_variance"
    RISK_PARITY = "risk_parity"
    EQUAL_WEIGHT = "equal_weight"
    INVERSE_VOLATILITY = "inverse_volatility"
    CURRENT = "current"


class CovarianceMethod(StrEnum):
    SAMPLE = "sample"
    EWMA = "ewma"
    SHRINKAGE = "shrinkage"
    MARKET_MODEL = "market_model"


class LiquidityPolicy(StrEnum):
    EXCLUDE = "exclude"
    WARN = "warn"


class RebalanceAction(StrEnum):
    INCREASE_POSITION = "increase_position"
    REDUCE_POSITION = "reduce_position"
    MAINTAIN_POSITION = "maintain_position"
    OPEN_POSITION = "open_position"
    CLOSE_POSITION = "close_position"
    HOLD_CASH = "hold_cash"
    NO_REBALANCING_REQUIRED = "no_rebalancing_required"


class ScenarioKind(StrEnum):
    HISTORICAL = "historical"
    HYPOTHETICAL = "hypothetical"


HORIZONS: dict[str, dict[str, Any]] = {
    "5m": {
        "group": "intraday",
        "interval": "5m",
        "bars_per_day": 78,
        "periods_per_year": 19_656,
        "max_forecast_age_hours": 2.0,
        "max_price_age_hours": 2.0,
        "cash_reserve": 0.10,
        "max_instruments": 8,
        "max_instrument_weight": 0.20,
        "max_sector_weight": 0.35,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "15m": {
        "group": "intraday",
        "interval": "15m",
        "bars_per_day": 26,
        "periods_per_year": 6_552,
        "max_forecast_age_hours": 4.0,
        "max_price_age_hours": 4.0,
        "cash_reserve": 0.10,
        "max_instruments": 8,
        "max_instrument_weight": 0.20,
        "max_sector_weight": 0.35,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    # This profile's calibration uses five-minute bars and treats the 20m
    # horizon as four observation periods.  A native 20m DataInterval now
    # exists, but this mapping is intentionally left on its calibrated input;
    # never label 15m data as a 20m horizon.
    "20m": {
        "group": "intraday",
        "interval": "5m",
        "bars_per_day": 78,
        "periods_per_year": 19_656,
        "max_forecast_age_hours": 4.0,
        "max_price_age_hours": 4.0,
        "cash_reserve": 0.10,
        "max_instruments": 8,
        "max_instrument_weight": 0.20,
        "max_sector_weight": 0.35,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "60m": {
        "group": "intraday",
        "interval": "1h",
        "bars_per_day": 6.5,
        "periods_per_year": 1_638,
        "max_forecast_age_hours": 8.0,
        "max_price_age_hours": 8.0,
        "cash_reserve": 0.10,
        "max_instruments": 8,
        "max_instrument_weight": 0.20,
        "max_sector_weight": 0.35,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "1d": {
        "group": "swing",
        "interval": "1d",
        "bars_per_day": 1,
        "periods_per_year": 252,
        "max_forecast_age_hours": 24.0,
        "max_price_age_hours": 24.0,
        "cash_reserve": 0.08,
        "max_instruments": 12,
        "max_instrument_weight": 0.18,
        "max_sector_weight": 0.30,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "3d": {
        "group": "swing",
        "interval": "1d",
        "bars_per_day": 1,
        "periods_per_year": 252,
        "max_forecast_age_hours": 48.0,
        "max_price_age_hours": 48.0,
        "cash_reserve": 0.08,
        "max_instruments": 12,
        "max_instrument_weight": 0.18,
        "max_sector_weight": 0.30,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "5d": {
        "group": "swing",
        "interval": "1d",
        "bars_per_day": 1,
        "periods_per_year": 252,
        "max_forecast_age_hours": 72.0,
        "max_price_age_hours": 72.0,
        "cash_reserve": 0.08,
        "max_instruments": 12,
        "max_instrument_weight": 0.18,
        "max_sector_weight": 0.30,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "30d": {
        "group": "long_term",
        "interval": "1d",
        "bars_per_day": 1,
        "periods_per_year": 252,
        "max_forecast_age_hours": 168.0,
        "max_price_age_hours": 168.0,
        "cash_reserve": 0.05,
        "max_instruments": 20,
        "max_instrument_weight": 0.15,
        "max_sector_weight": 0.35,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
    "90d": {
        "group": "long_term",
        "interval": "1d",
        "bars_per_day": 1,
        "periods_per_year": 252,
        "max_forecast_age_hours": 168.0,
        "max_price_age_hours": 168.0,
        "cash_reserve": 0.05,
        "max_instruments": 20,
        "max_instrument_weight": 0.15,
        "max_sector_weight": 0.35,
        "default_objective": OptimizationObjective.RISK_ADJUSTED.value,
        "default_method": AllocationMethod.MEAN_VARIANCE.value,
    },
}

SUPPORTED_HORIZONS = tuple(HORIZONS)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def parse_time(value: object, name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        raise OptimizationValidationError(f"{name} is required")
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise OptimizationValidationError(f"{name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OptimizationValidationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def optional_time(value: object) -> datetime | None:
    return None if value in (None, "") else parse_time(value)


def text(value: object, default: str = "") -> str:
    return default if value is None else str(value).strip() or default


def number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise OptimizationValidationError(f"{name} must be a finite number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise OptimizationValidationError(f"{name} must be a finite number") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise OptimizationValidationError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise OptimizationValidationError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise OptimizationValidationError(f"{name} must be at most {maximum}")
    return result


def whole(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise OptimizationValidationError(f"{name} must be an integer")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise OptimizationValidationError(f"{name} must be an integer") from exc
    if result < minimum:
        raise OptimizationValidationError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise OptimizationValidationError(f"{name} must be at most {maximum}")
    return result


def canonical_horizon(value: object) -> str:
    horizon = text(value).lower()
    if horizon not in HORIZONS:
        raise OptimizationValidationError(
            "horizon must be one of: " + ", ".join(SUPPORTED_HORIZONS)
        )
    return horizon


def _default_volatility(horizon: str) -> float:
    return {
        "5m": 2.0,
        "15m": 1.5,
        "20m": 1.5,
        "60m": 1.0,
        "1d": 0.55,
        "3d": 0.50,
        "5d": 0.45,
        "30d": 0.35,
        "90d": 0.30,
    }.get(horizon, 0.30)


def sequence(value: object) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(value)
    raise OptimizationValidationError("value must be a list")


def stable_id(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256("|".join(str(item) for item in parts).encode()).hexdigest()[:20]


def digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class OptimizationConfig:
    config_id: str
    portfolio_id: str
    horizon: str
    objective: str
    method: str
    instrument_ids: tuple[str, ...] = ()
    max_instruments: int = 12
    max_instrument_weight: float = 0.20
    max_sector_weight: float = 0.35
    min_cash_reserve: float = 0.08
    max_positions: int = 12
    min_position_weight: float = 0.0
    max_turnover: float = 0.35
    rebalance_threshold: float = 0.02
    min_trade_notional: float = 100.0
    transaction_cost_bps: float = 10.0
    slippage_bps: float = 5.0
    max_participation_rate: float = 0.01
    min_history_observations: int = MIN_HISTORY_DEFAULT
    covariance_method: str = CovarianceMethod.SHRINKAGE.value
    ewma_halflife: int = 20
    shrinkage: float = 0.10
    risk_free_rate: float = 0.05
    risk_aversion: float = 2.0
    max_volatility: float = 0.30
    max_drawdown: float = 0.25
    max_forecast_age_hours: float = 72.0
    max_price_age_hours: float = 72.0
    min_forecast_quality: str = "low"
    liquidity_policy: str = LiquidityPolicy.EXCLUDE.value
    allowed_exchanges: tuple[str, ...] = ("XNSE", "XBOM")
    sector_limits: Mapping[str, float] = field(default_factory=dict)
    objective_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "return": 0.35,
            "risk": 0.35,
            "diversification": 0.15,
            "turnover": 0.10,
            "liquidity": 0.05,
        }
    )
    benchmark: str = "equal_weight"
    benchmark_instrument_id: str = ""
    min_expected_return: float | None = None
    version: str = CONFIG_VERSION
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.portfolio_id.strip():
            raise OptimizationValidationError("portfolio_id is required")
        canonical_horizon(self.horizon)
        if self.objective not in {item.value for item in OptimizationObjective}:
            raise OptimizationValidationError("unknown optimization objective")
        if self.method not in {item.value for item in AllocationMethod}:
            raise OptimizationValidationError("unknown allocation method")
        if self.covariance_method not in {item.value for item in CovarianceMethod}:
            raise OptimizationValidationError("unknown covariance method")
        if self.liquidity_policy not in {item.value for item in LiquidityPolicy}:
            raise OptimizationValidationError("unknown liquidity policy")
        if not self.instrument_ids or len(self.instrument_ids) > MAX_INSTRUMENTS:
            raise OptimizationValidationError(
                f"instrument_ids must contain 1..{MAX_INSTRUMENTS} instruments"
            )
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise OptimizationValidationError("instrument_ids must be unique")
        if any(":" not in item for item in self.instrument_ids):
            raise OptimizationValidationError(
                "every instrument id must be exchange-qualified (MIC:SYMBOL)"
            )
        if not 1 <= self.max_instruments <= MAX_INSTRUMENTS:
            raise OptimizationValidationError(
                f"max_instruments must be within 1..{MAX_INSTRUMENTS}"
            )
        if not 0.0 < self.max_instrument_weight <= 1.0:
            raise OptimizationValidationError("max_instrument_weight must be within (0, 1]")
        if not 0.0 < self.max_sector_weight <= 1.0:
            raise OptimizationValidationError("max_sector_weight must be within (0, 1]")
        if not 0.0 <= self.min_cash_reserve < 1.0:
            raise OptimizationValidationError("min_cash_reserve must be within [0, 1)")
        if not 1 <= self.max_positions <= MAX_INSTRUMENTS:
            raise OptimizationValidationError("max_positions must be within 1..50")
        if not 0.0 <= self.min_position_weight < self.max_instrument_weight:
            raise OptimizationValidationError(
                "min_position_weight must be below max_instrument_weight"
            )
        if not 0.0 < self.max_turnover <= 2.0:
            raise OptimizationValidationError("max_turnover must be within (0, 2]")
        if not 0.0 <= self.rebalance_threshold <= 0.25:
            raise OptimizationValidationError("rebalance_threshold must be within [0, 0.25]")
        if self.min_trade_notional < 0:
            raise OptimizationValidationError("min_trade_notional cannot be negative")
        if not 0 <= self.transaction_cost_bps <= 500:
            raise OptimizationValidationError("transaction_cost_bps must be within 0..500")
        if not 0 <= self.slippage_bps <= 500:
            raise OptimizationValidationError("slippage_bps must be within 0..500")
        if not 0.0 < self.max_participation_rate <= 0.25:
            raise OptimizationValidationError("max_participation_rate must be within (0, 0.25]")
        if not 5 <= self.min_history_observations <= 1_000:
            raise OptimizationValidationError("min_history_observations must be within 5..1000")
        if self.covariance_method == CovarianceMethod.EWMA and self.ewma_halflife < 2:
            raise OptimizationValidationError("ewma_halflife must be at least 2")
        benchmark_instrument = str(self.benchmark_instrument_id or "").strip()
        if benchmark_instrument and ":" not in benchmark_instrument:
            raise OptimizationValidationError(
                "benchmark_instrument_id must be exchange-qualified (MIC:SYMBOL)"
            )
        if benchmark_instrument and benchmark_instrument in self.instrument_ids:
            raise OptimizationValidationError(
                "benchmark_instrument_id must be a distinct market series, not an allocated instrument"
            )
        if (
            self.covariance_method == CovarianceMethod.MARKET_MODEL
            and not benchmark_instrument
        ):
            raise OptimizationValidationError(
                "market_model covariance requires a benchmark_instrument_id"
            )
        if not 0.0 <= self.shrinkage <= 1.0:
            raise OptimizationValidationError("shrinkage must be within [0, 1]")
        if not -0.5 <= self.risk_free_rate <= 1.0:
            raise OptimizationValidationError("risk_free_rate must be within [-0.5, 1]")
        if not 0.0 <= self.risk_aversion <= 20.0:
            raise OptimizationValidationError("risk_aversion must be within [0, 20]")
        if self.max_volatility <= 0 or self.max_drawdown <= 0:
            raise OptimizationValidationError("risk limits must be positive")
        if self.max_forecast_age_hours <= 0 or self.max_price_age_hours <= 0:
            raise OptimizationValidationError("freshness limits must be positive")
        if self.min_forecast_quality not in FORECAST_QUALITY_RANK:
            raise OptimizationValidationError(
                "min_forecast_quality must be one of unknown, low, medium or high"
            )
        if not self.allowed_exchanges:
            raise OptimizationValidationError("allowed_exchanges cannot be empty")
        if self.min_expected_return is not None:
            number(self.min_expected_return, "min_expected_return")
        total_weight = sum(float(value) for value in self.sector_limits.values())
        if any(
            float(value) < 0 or float(value) > self.max_sector_weight
            for value in self.sector_limits.values()
        ):
            raise OptimizationValidationError("sector limits must be within [0, max_sector_weight]")
        if total_weight < 0:
            raise OptimizationValidationError("sector limits are invalid")
        for key, value in self.objective_weights.items():
            number(value, f"objective_weights.{key}", minimum=0.0)
        if (
            not self.objective_weights
            or sum(float(value) for value in self.objective_weights.values()) <= 0
        ):
            raise OptimizationValidationError("objective_weights must have a positive total")
        if self.method == AllocationMethod.CURRENT.value and not self.portfolio_id:
            raise OptimizationValidationError("current allocation needs a portfolio")

    @property
    def investable_fraction(self) -> float:
        return 1.0 - self.min_cash_reserve

    @property
    def config_version(self) -> str:
        return self.config_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "portfolio_id": self.portfolio_id,
            "horizon": self.horizon,
            "objective": self.objective,
            "method": self.method,
            "instrument_ids": list(self.instrument_ids),
            "max_instruments": self.max_instruments,
            "max_instrument_weight": self.max_instrument_weight,
            "max_sector_weight": self.max_sector_weight,
            "min_cash_reserve": self.min_cash_reserve,
            "max_positions": self.max_positions,
            "min_position_weight": self.min_position_weight,
            "max_turnover": self.max_turnover,
            "rebalance_threshold": self.rebalance_threshold,
            "min_trade_notional": self.min_trade_notional,
            "transaction_cost_bps": self.transaction_cost_bps,
            "slippage_bps": self.slippage_bps,
            "max_participation_rate": self.max_participation_rate,
            "min_history_observations": self.min_history_observations,
            "covariance_method": self.covariance_method,
            "ewma_halflife": self.ewma_halflife,
            "shrinkage": self.shrinkage,
            "risk_free_rate": self.risk_free_rate,
            "risk_aversion": self.risk_aversion,
            "max_volatility": self.max_volatility,
            "max_drawdown": self.max_drawdown,
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "max_price_age_hours": self.max_price_age_hours,
            "min_forecast_quality": self.min_forecast_quality,
            "liquidity_policy": self.liquidity_policy,
            "allowed_exchanges": list(self.allowed_exchanges),
            "sector_limits": dict(self.sector_limits),
            "objective_weights": dict(self.objective_weights),
            "benchmark": self.benchmark,
            "benchmark_instrument_id": self.benchmark_instrument_id,
            "min_expected_return": self.min_expected_return,
            "version": self.version,
            "investable_fraction": self.investable_fraction,
            "currency": "INR",
            "nova_coin_rate": "1 NC = 1 INR",
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, now: datetime | None = None
    ) -> OptimizationConfig:
        horizon = canonical_horizon(payload.get("horizon") or "5d")
        defaults = HORIZONS[horizon]
        objective = text(payload.get("objective"), defaults["default_objective"]).lower()
        method = text(payload.get("method"), defaults["default_method"]).lower()
        portfolio_id = text(payload.get("portfolio_id"))
        instrument_ids = tuple(
            dict.fromkeys(
                text(item) for item in sequence(payload.get("instrument_ids")) if text(item)
            )
        )
        sector_limits_raw = payload.get("sector_limits") or {}
        if not isinstance(sector_limits_raw, Mapping):
            raise OptimizationValidationError("sector_limits must be an object")
        sector_limits = {
            str(key).strip().lower(): number(value, f"sector_limits.{key}", minimum=0.0)
            for key, value in sector_limits_raw.items()
        }
        weights_raw = payload.get("objective_weights") or {
            "return": 0.35,
            "risk": 0.35,
            "diversification": 0.15,
            "turnover": 0.10,
            "liquidity": 0.05,
        }
        if not isinstance(weights_raw, Mapping):
            raise OptimizationValidationError("objective_weights must be an object")
        objective_weights = {
            str(key).strip().lower(): number(value, f"objective_weights.{key}", minimum=0.0)
            for key, value in weights_raw.items()
        }
        min_expected = payload.get("min_expected_return")
        config = cls(
            config_id="",
            portfolio_id=portfolio_id,
            horizon=horizon,
            objective=objective,
            method=method,
            instrument_ids=instrument_ids,
            max_instruments=whole(
                payload.get("max_instruments", defaults["max_instruments"]),
                "max_instruments",
                minimum=1,
                maximum=MAX_INSTRUMENTS,
            ),
            max_instrument_weight=number(
                payload.get("max_instrument_weight", defaults["max_instrument_weight"]),
                "max_instrument_weight",
                minimum=0.0001,
                maximum=1.0,
            ),
            max_sector_weight=number(
                payload.get("max_sector_weight", defaults["max_sector_weight"]),
                "max_sector_weight",
                minimum=0.0001,
                maximum=1.0,
            ),
            min_cash_reserve=number(
                payload.get("min_cash_reserve", defaults["cash_reserve"]),
                "min_cash_reserve",
                minimum=0.0,
                maximum=0.99,
            ),
            max_positions=whole(
                payload.get("max_positions", defaults["max_instruments"]),
                "max_positions",
                minimum=1,
                maximum=MAX_INSTRUMENTS,
            ),
            min_position_weight=number(
                payload.get("min_position_weight", 0.0),
                "min_position_weight",
                minimum=0.0,
                maximum=0.99,
            ),
            max_turnover=number(
                payload.get("max_turnover", 0.35), "max_turnover", minimum=0.0001, maximum=2.0
            ),
            rebalance_threshold=number(
                payload.get("rebalance_threshold", 0.02),
                "rebalance_threshold",
                minimum=0.0,
                maximum=0.25,
            ),
            min_trade_notional=number(
                payload.get("min_trade_notional", 100.0), "min_trade_notional", minimum=0.0
            ),
            transaction_cost_bps=number(
                payload.get("transaction_cost_bps", 10.0),
                "transaction_cost_bps",
                minimum=0.0,
                maximum=500.0,
            ),
            slippage_bps=number(
                payload.get("slippage_bps", 5.0),
                "slippage_bps",
                minimum=0.0,
                maximum=500.0,
            ),
            max_participation_rate=number(
                payload.get("max_participation_rate", 0.01),
                "max_participation_rate",
                minimum=0.000001,
                maximum=0.25,
            ),
            min_history_observations=whole(
                payload.get("min_history_observations", MIN_HISTORY_DEFAULT),
                "min_history_observations",
                minimum=5,
                maximum=1_000,
            ),
            covariance_method=text(
                payload.get("covariance_method"), CovarianceMethod.SHRINKAGE.value
            ).lower(),
            ewma_halflife=whole(
                payload.get("ewma_halflife", 20), "ewma_halflife", minimum=2, maximum=500
            ),
            shrinkage=number(payload.get("shrinkage", 0.10), "shrinkage", minimum=0.0, maximum=1.0),
            risk_free_rate=number(
                payload.get("risk_free_rate", 0.05),
                "risk_free_rate",
                minimum=-0.5,
                maximum=1.0,
            ),
            risk_aversion=number(
                payload.get("risk_aversion", 2.0), "risk_aversion", minimum=0.0, maximum=20.0
            ),
            max_volatility=number(
                payload.get("max_volatility", _default_volatility(horizon)),
                "max_volatility",
                minimum=0.0001,
                maximum=10.0,
            ),
            max_drawdown=number(
                payload.get("max_drawdown", 0.25), "max_drawdown", minimum=0.0001, maximum=1.0
            ),
            max_forecast_age_hours=number(
                payload.get("max_forecast_age_hours", defaults["max_forecast_age_hours"]),
                "max_forecast_age_hours",
                minimum=0.01,
            ),
            max_price_age_hours=number(
                payload.get("max_price_age_hours", defaults["max_price_age_hours"]),
                "max_price_age_hours",
                minimum=0.01,
            ),
            min_forecast_quality=text(payload.get("min_forecast_quality"), "low").lower(),
            liquidity_policy=text(
                payload.get("liquidity_policy"), LiquidityPolicy.EXCLUDE.value
            ).lower(),
            allowed_exchanges=tuple(
                str(item).strip().upper()
                for item in sequence(payload.get("allowed_exchanges") or ("XNSE", "XBOM"))
                if str(item).strip()
            ),
            sector_limits=sector_limits,
            objective_weights=objective_weights,
            benchmark=text(payload.get("benchmark"), "equal_weight"),
            benchmark_instrument_id=text(payload.get("benchmark_instrument_id")).strip().upper(),
            min_expected_return=(
                None if min_expected in (None, "") else number(min_expected, "min_expected_return")
            ),
            created_at=now or utcnow(),
        )
        digest = digest_payload(config.to_dict())
        return replace(config, config_id="poc_" + digest[:20])

    def digest(self) -> str:
        return digest_payload(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OptimizationConfig:
        return cls.from_payload(payload, now=optional_time(payload.get("created_at")) or utcnow())


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    instrument_id: str
    quantity: float
    average_cost: float
    price: float | None
    market_value: float | None
    weight: float
    sector: str
    asset_class: str
    stale: bool
    priced_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "quantity": self.quantity,
            "average_cost": self.average_cost,
            "price": self.price,
            "market_value": self.market_value,
            "weight": self.weight,
            "sector": self.sector,
            "asset_class": self.asset_class,
            "stale": self.stale,
            "priced_at": None if self.priced_at is None else self.priced_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    portfolio_id: str
    as_of: datetime
    version: str
    cash: float
    invested: float
    total_value: float
    realized_pnl: float
    unrealized_pnl: float
    positions: tuple[PortfolioPosition, ...]
    pending_orders: tuple[Mapping[str, Any], ...] = ()
    pending_buy_reserve: float = 0.0
    currency: str = "INR"
    stale: bool = False
    source: str = "NM-07 read-only portfolio detail"

    @property
    def available_cash(self) -> float:
        return max(0.0, self.cash - self.pending_buy_reserve)

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "as_of": self.as_of.isoformat(),
            "version": self.version,
            "cash": self.cash,
            "available_cash": self.available_cash,
            "invested": self.invested,
            "total_value": self.total_value,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "positions": [row.to_dict() for row in self.positions],
            "pending_orders": [dict(row) for row in self.pending_orders],
            "pending_buy_reserve": self.pending_buy_reserve,
            "currency": self.currency,
            "nova_coin_rate": "1 NC = 1 INR",
            "stale": self.stale,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class PriceSeries:
    instrument_id: str
    interval: str
    observed_at: tuple[datetime, ...]
    close: tuple[float, ...]
    volume: tuple[float | None, ...]
    source_reference: str = ""
    currency: str = "INR"

    def __post_init__(self) -> None:
        if not self.instrument_id.strip() or not self.interval.strip():
            raise OptimizationValidationError("price series identity and interval are required")
        if not len(self.observed_at) == len(self.close) == len(self.volume):
            raise OptimizationValidationError(
                "price series timestamps, closes and volumes must have equal lengths"
            )
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in self.close):
            raise OptimizationValidationError("price series closes must be finite and positive")
        if any(
            value is not None and (not math.isfinite(float(value)) or float(value) < 0)
            for value in self.volume
        ):
            raise OptimizationValidationError(
                "price series volumes must be finite and non-negative"
            )
        if any(
            self.observed_at[index] >= self.observed_at[index + 1]
            for index in range(len(self.observed_at) - 1)
        ):
            raise OptimizationValidationError("price series timestamps must be strictly increasing")

    @property
    def latest_price(self) -> float | None:
        return self.close[-1] if self.close else None

    @property
    def latest_at(self) -> datetime | None:
        return self.observed_at[-1] if self.observed_at else None


@dataclass(frozen=True, slots=True)
class CovarianceResult:
    instruments: tuple[str, ...]
    matrix: tuple[tuple[float, ...], ...]
    correlation: tuple[tuple[float, ...], ...]
    observations: int
    method: str
    interval: str
    periods_per_year: int
    condition_number: float
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruments": list(self.instruments),
            "matrix": [list(row) for row in self.matrix],
            "correlation": [list(row) for row in self.correlation],
            "observations": self.observations,
            "method": self.method,
            "interval": self.interval,
            "periods_per_year": self.periods_per_year,
            "condition_number": self.condition_number,
            "warnings": list(self.warnings),
            "definition": (
                "period covariance of aligned log returns; optimizer uses the "
                "matrix as a research estimate, never as a risk approval"
            ),
        }


@dataclass(frozen=True, slots=True)
class ExpectedReturn:
    instrument_id: str
    horizon: str
    expected_return: float | None
    adjusted_return: float | None
    uncertainty: str
    interval_width: float | None
    forecast_ids: tuple[str, ...]
    model_versions: tuple[str, ...]
    generated_at: datetime | None
    validity: str
    calibrated_probability: float | None
    probability_is_calibrated: bool
    disagreement: float | None
    quality: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "expected_return": self.expected_return,
            "adjusted_return": self.adjusted_return,
            "uncertainty": self.uncertainty,
            "interval_width": self.interval_width,
            "forecast_ids": list(self.forecast_ids),
            "model_versions": list(self.model_versions),
            "generated_at": None if self.generated_at is None else self.generated_at.isoformat(),
            "validity": self.validity,
            "calibrated_probability": self.calibrated_probability,
            "probability_is_calibrated": self.probability_is_calibrated,
            "disagreement": self.disagreement,
            "quality": self.quality,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    kind: str
    name: str
    description: str
    portfolio_value_change: float
    portfolio_value_change_pct: float
    position_contributions: tuple[Mapping[str, Any], ...]
    sector_contributions: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "portfolio_value_change": self.portfolio_value_change,
            "portfolio_value_change_pct": self.portfolio_value_change_pct,
            "position_contributions": [dict(row) for row in self.position_contributions],
            "sector_contributions": [dict(row) for row in self.sector_contributions],
            "warnings": list(self.warnings),
            "assumptions": list(self.assumptions),
            "is_forecast": False,
            "is_hypothetical": self.kind == ScenarioKind.HYPOTHETICAL.value,
        }


__all__ = [
    "AllocationMethod",
    "CONFIG_VERSION",
    "CovarianceMethod",
    "CovarianceResult",
    "ExpectedReturn",
    "HORIZONS",
    "LiquidityPolicy",
    "MAX_BACKTESTS",
    "MAX_CONFIGS",
    "MAX_CONCURRENT_RUNS",
    "MAX_INSTRUMENTS",
    "MAX_RUN_TIMEOUT_SECONDS",
    "MAX_RUNS",
    "MAX_SCENARIOS_PER_RUN",
    "OPTIMIZATION_CONTRACT_VERSION",
    "OPTIMIZATION_PHASE",
    "OPTIMIZATION_SCHEMA_VERSION",
    "OptimizationConfig",
    "OptimizationError",
    "OptimizationInfeasibleError",
    "OptimizationNotFoundError",
    "OptimizationObjective",
    "OptimizationResourceError",
    "OptimizationStateError",
    "OptimizationStatus",
    "OptimizationStoreError",
    "OptimizationValidationError",
    "PortfolioPosition",
    "PortfolioSnapshot",
    "PriceSeries",
    "RebalanceAction",
    "SOLVER_VERSION",
    "SUPPORTED_HORIZONS",
    "ScenarioKind",
    "ScenarioResult",
    "TERMINAL_STATUSES",
    "canonical_horizon",
    "digest_payload",
    "number",
    "optional_time",
    "parse_time",
    "sequence",
    "stable_id",
    "text",
    "utcnow",
    "whole",
]
