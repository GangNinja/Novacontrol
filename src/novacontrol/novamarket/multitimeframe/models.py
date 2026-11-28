"""Cross-timeframe decision contracts for intraday trading intelligence.

The hierarchy is an *analysis* layer:

* 20-minute context — the broader short-term market direction and condition;
* 5-minute setup — a candidate setup that must agree with the context;
* 1-minute timing — entry/exit timing from completed one-minute observations.

A decision is a candidate, never an order.  It carries no wallet, position,
proposal, risk-decision or execution field; NM-08 remains the only risk
authority and NM-10 the only paper-execution authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any, Mapping

MULTITIMEFRAME_CONTRACT_VERSION = "mtf-hierarchy-1.0"
MULTITIMEFRAME_POLICY_VERSION = "mtf-policy-1.0"


class TimeframeLabel(StrEnum):
    CONTEXT = "20m"
    SETUP = "5m"
    TIMING = "1m"


class TimeframeDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class MarketCondition(StrEnum):
    TRENDING = "trending"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"


class SetupState(StrEnum):
    CONFIRMED = "confirmed"
    WEAK = "weak"
    CONFLICTING = "conflicting"
    ABSENT = "absent"


class TimingState(StrEnum):
    ENTRY_LONG = "entry_long"
    ENTRY_SHORT = "entry_short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    WAIT = "wait"
    INVALID = "invalid"

    @property
    def entry_side(self) -> str:
        return {
            TimingState.ENTRY_LONG: "buy",
            TimingState.ENTRY_SHORT: "sell",
        }.get(self, "")


class TimeframeOutcome(StrEnum):
    BUY_CANDIDATE = "buy_candidate"
    SELL_CANDIDATE = "sell_candidate"
    HOLD = "hold"
    WAIT_FOR_CONFIRMATION = "wait_for_confirmation"
    REJECT_SIGNAL = "reject_signal"

    @property
    def side(self) -> str:
        return {
            TimeframeOutcome.BUY_CANDIDATE: "buy",
            TimeframeOutcome.SELL_CANDIDATE: "sell",
        }.get(self, "")


class ConflictStatus(StrEnum):
    ALIGNED = "aligned"
    PARTIAL = "partial"
    CONFLICT = "conflict"
    INSUFFICIENT_DATA = "insufficient_data"


class Uncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _decimal(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _clamp(value: Decimal, low: str = "0", high: str = "1") -> Decimal:
    return max(Decimal(low), min(Decimal(high), value))


@dataclass(frozen=True, slots=True)
class TimeframeLayer:
    """One timeframe's structured assessment."""

    label: TimeframeLabel
    interval: str
    direction: TimeframeDirection
    available: bool
    stale: bool
    observations: int
    confidence: Decimal
    as_of: datetime | None
    reasons: tuple[str, ...] = ()
    condition: MarketCondition | None = None
    setup_state: SetupState | None = None
    timing_state: TimingState | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _clamp(_decimal(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "interval": self.interval,
            "direction": self.direction.value,
            "available": self.available,
            "stale": self.stale,
            "observations": self.observations,
            "confidence": format(self.confidence, "f"),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "condition": self.condition.value if self.condition else None,
            "setup_state": self.setup_state.value if self.setup_state else None,
            "timing_state": self.timing_state.value if self.timing_state else None,
            "reasons": list(self.reasons),
            "metrics": _json_safe(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class MultiTimeframeConfig:
    """Thresholds and safety limits for the hierarchy.

    Values are deliberately conservative.  A missing or stale layer can only
    reduce actionability; it can never be coerced into a bullish/bearish vote.
    """

    context_interval: str = "5m"
    context_bucket_minutes: int = 20
    context_min_buckets: int = 8
    setup_min_bars: int = 30
    timing_min_bars: int = 15
    max_context_age_seconds: int = 1800
    max_setup_age_seconds: int = 900
    max_timing_age_seconds: int = 180
    context_max_gap_seconds: int = 1800
    setup_max_gap_seconds: int = 600
    timing_max_gap_seconds: int = 120
    trend_band_pct: float = 0.0008
    high_volatility_atr_pct: float = 0.02
    setup_rsi_low: float = 30.0
    setup_rsi_high: float = 70.0
    max_timing_return_pct: float = 0.03
    minimum_confidence: float = 0.55
    cache_ttl_seconds: float = 2.0
    allow_neutral_context: bool = False
    block_on_high_volatility: bool = True

    def __post_init__(self) -> None:
        if self.context_bucket_minutes < 5:
            raise ValueError("context_bucket_minutes must be at least 5")
        if self.context_bucket_minutes % 5:
            raise ValueError("context_bucket_minutes must be a multiple of 5")
        for name in (
            "context_min_buckets",
            "setup_min_bars",
            "timing_min_bars",
            "max_context_age_seconds",
            "max_setup_age_seconds",
            "max_timing_age_seconds",
            "context_max_gap_seconds",
            "setup_max_gap_seconds",
            "timing_max_gap_seconds",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.trend_band_pct < 1:
            raise ValueError("trend_band_pct must be in [0, 1)")
        if not 0 < self.high_volatility_atr_pct < 1:
            raise ValueError("high_volatility_atr_pct must be in (0, 1)")
        if not 0 <= self.setup_rsi_low < self.setup_rsi_high <= 100:
            raise ValueError("setup RSI bounds must satisfy 0 <= low < high <= 100")
        if not 0 < self.max_timing_return_pct < 1:
            raise ValueError("max_timing_return_pct must be in (0, 1)")
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be in [0, 1]")
        if self.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds cannot be negative")
        if self.context_interval not in {"1m", "5m"}:
            raise ValueError("context_interval must be 1m or 5m")

    def with_overrides(self, values: Mapping[str, Any]) -> "MultiTimeframeConfig":
        known = set(self.__dataclass_fields__)
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown multi-timeframe setting(s): {', '.join(unknown)}")
        return replace(self, **dict(values))

    def to_dict(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        payload["policy_version"] = MULTITIMEFRAME_POLICY_VERSION
        return payload


@dataclass(frozen=True, slots=True)
class MultiTimeframeDecision:
    """The combined 20m/5m/1m result.  Evidence, never an execution."""

    decision_id: str
    instrument_id: str
    generated_at: datetime
    context: TimeframeLayer
    setup: TimeframeLayer
    timing: TimeframeLayer
    outcome: TimeframeOutcome
    conflict_status: ConflictStatus
    direction: TimeframeDirection
    confidence: Decimal
    uncertainty: Uncertainty
    missing: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    policy_version: str = MULTITIMEFRAME_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _clamp(_decimal(self.confidence)))

    @property
    def candidate_side(self) -> str:
        return self.outcome.side

    def allows_signal(self, signal_type: str) -> bool:
        """Whether an actionable strategy signal may become a proposal.

        Only new entries are gated.  Exit signals stay available so protective
        exits are never blocked by the hierarchy.
        """

        side = str(signal_type or "").lower()
        if side in {"exit", "hold", "no_signal"}:
            return True
        return bool(side) and side == self.candidate_side

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "instrument_id": self.instrument_id,
            "generated_at": self.generated_at.isoformat(),
            "outcome": self.outcome.value,
            "candidate_side": self.candidate_side,
            "conflict_status": self.conflict_status.value,
            "direction": self.direction.value,
            "confidence": format(self.confidence, "f"),
            "uncertainty": self.uncertainty.value,
            "missing": list(self.missing),
            "reasons": list(self.reasons),
            "policy_version": self.policy_version,
            "contract_version": MULTITIMEFRAME_CONTRACT_VERSION,
            "timeframes": {
                "context_20m": self.context.to_dict(),
                "setup_5m": self.setup.to_dict(),
                "timing_1m": self.timing.to_dict(),
            },
            "authority": {
                "risk": "NM-08",
                "execution": "NM-10",
                "executes": False,
                "approves": False,
            },
        }


def decision_id(
    instrument_id: str,
    generated_at: datetime,
    context: TimeframeLayer,
    setup: TimeframeLayer,
    timing: TimeframeLayer,
) -> str:
    seed = "|".join(
        (
            str(instrument_id),
            generated_at.isoformat(),
            context.direction.value,
            setup.direction.value,
            timing.direction.value,
            timing.timing_state.value if timing.timing_state else "",
        )
    )
    return "mtf_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
