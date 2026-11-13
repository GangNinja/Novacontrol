"""NM-10 execution assumptions.

These are *simulation* assumptions, not exchange facts.  They are versioned as a
whole (``ExecutionConfig.version``) and every order and fill records the version
that produced it.  The defaults deliberately use a simple, disclosed Indian
cash-equity fee schedule and a participation-rate liquidity model; changing any
value changes the version recorded on future executions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Mapping


BPS = Decimal("10000")


def _dec(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - a configuration value may be anything
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """One immutable set of execution assumptions.

    Fees are illustrative and must not be presented as a broker quote.  The
    effective date is metadata, not a claim that the values match a live tariff.
    """

    version: str = "nm10-exec-v1"
    # Liquidity: only this fraction of the reference session's volume is assumed
    # available to one paper order.
    participation_rate: Decimal = Decimal("0.10")
    minimum_liquidity_quantity: Decimal = Decimal("0")
    # Synthetic spread around the last price.  NM-02 has no consolidated book;
    # this is explicitly an approximation, not a fabricated bid/ask.
    spread_bps: Decimal = Decimal("8")
    # Square-root market impact coefficient in basis points.
    impact_coefficient_bps: Decimal = Decimal("12")
    volatility_weight: Decimal = Decimal("0.35")
    maximum_slippage_bps: Decimal = Decimal("150")
    # A market order will not be filled from data older than this.
    stale_after_seconds: int = 300
    require_fresh_data: bool = True
    # Historical bars can be an explicit fallback; disabled by default.
    allow_bar_fallback: bool = False
    # Order expiry when a proposal does not supply one (proposals normally do).
    default_order_ttl_seconds: int = 86_400
    # Fees: basis points of gross notional, each recorded separately.
    brokerage_bps: Decimal = Decimal("3")
    exchange_bps: Decimal = Decimal("0.297")
    sebi_bps: Decimal = Decimal("0.01")
    stamp_duty_bps: Decimal = Decimal("0.015")
    gst_percent: Decimal = Decimal("18")
    minimum_total_fee: Decimal = Decimal("0")
    fee_effective_date: str = "2026-01-01"
    fee_disclaimer: str = (
        "Illustrative Indian cash-equity simulation schedule. Verify against the "
        "current broker/exchange tariff before relying on exact costs."
    )

    def __post_init__(self) -> None:
        if not str(self.version).strip():
            raise ValueError("execution config version is required")
        for name in (
            "participation_rate",
            "minimum_liquidity_quantity",
            "spread_bps",
            "impact_coefficient_bps",
            "volatility_weight",
            "maximum_slippage_bps",
            "brokerage_bps",
            "exchange_bps",
            "sebi_bps",
            "stamp_duty_bps",
            "gst_percent",
            "minimum_total_fee",
        ):
            object.__setattr__(self, name, _dec(getattr(self, name), name))
        if not Decimal("0") < self.participation_rate <= Decimal("1"):
            raise ValueError("participation_rate must be within (0, 1]")
        if self.maximum_slippage_bps > Decimal("1000"):
            raise ValueError("maximum_slippage_bps is unreasonably large")
        for name in ("stale_after_seconds", "default_order_ttl_seconds"):
            value = int(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)

    def with_changes(self, **changes: Any) -> "ExecutionConfig":
        return replace(self, **changes)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ExecutionConfig":
        if not isinstance(payload, Mapping):
            raise ValueError("execution configuration must be an object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(
                "unknown execution configuration field(s): " + ", ".join(unknown)
            )
        return cls(**{key: payload[key] for key in payload})

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, Decimal):
                result[name] = format(value.quantize(Decimal("0.0001"), ROUND_HALF_EVEN), "f")
            else:
                result[name] = value
        return result
