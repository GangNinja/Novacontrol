"""Deterministic, data-aware paper execution pricing and Indian fee simulation."""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.execution.models import (
    MarketSnapshot,
    OrderSide,
    OrderType,
    money,
    quantity,
)


class ExecutionPricingError(ValueError):
    """The market data or order shape cannot support a paper execution."""


def _decimal(value: object, *, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ExecutionPricingError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise ExecutionPricingError(f"{name} must be finite")
    return result


def _volatility(closes: list[Decimal]) -> Decimal:
    if len(closes) < 2:
        return Decimal("0")
    returns: list[float] = []
    for previous, current in zip(closes, closes[1:]):
        if previous <= 0:
            continue
        returns.append(float((current - previous) / previous))
    if len(returns) < 2:
        return Decimal("0")
    return Decimal(str(statistics.pstdev(returns) * 100)).quantize(Decimal("0.0001"))


def _observation_price(observation: Any) -> Decimal:
    value = getattr(observation, "price", None)
    if value is None:
        raise ExecutionPricingError("latest market observation has no price")
    return money(value)


def _volume_from_bars(bars: list[Any]) -> Decimal:
    volumes = [
        _decimal(getattr(bar, "volume", None), name="bar volume")
        for bar in bars
        if getattr(bar, "volume", None) is not None and not getattr(bar, "partial", False)
    ]
    if not volumes:
        return Decimal("0")
    return quantity(sum(volumes, Decimal("0")) / Decimal(len(volumes)))


def snapshot_for(
    market_data: Any,
    instrument_id: str,
    *,
    now: datetime,
    config: ExecutionConfig,
) -> MarketSnapshot:
    """Build one execution snapshot from NM-02, refusing fabricated depth."""
    if market_data is None:
        raise ExecutionPricingError("market-data service is unavailable")
    try:
        instrument = market_data.get_instrument(instrument_id)
    except Exception as exc:  # noqa: BLE001
        raise ExecutionPricingError(f"unknown instrument {instrument_id!r}: {exc}") from exc
    status = getattr(getattr(instrument, "trading_status", None), "value", None) or str(
        getattr(instrument, "trading_status", "unknown")
    )
    if status in {"halted", "delisted", "closed"}:
        raise ExecutionPricingError(f"instrument {instrument_id} is not tradable (status={status})")

    observation = None
    latest_error = ""
    try:
        reconciliation = market_data.latest(instrument_id)
        if getattr(reconciliation, "usable", True) is False:
            latest_error = "the latest market reconciliation is not usable"
        else:
            observation = getattr(reconciliation, "selected", None)
    except Exception as exc:  # noqa: BLE001
        latest_error = f"{type(exc).__name__}: {exc}"

    bars: list[Any] = []
    try:
        page = market_data.bars(instrument_id, limit=30)
        bars = list(getattr(page, "items", page) or [])
    except Exception:
        bars = []

    price: Decimal | None = None
    observed: datetime | None = None
    source = "nm02.latest"
    is_fallback = False
    fresh = True
    quality_usable = True
    reasons: list[str] = []
    if observation is not None:
        price = _observation_price(observation)
        observed = getattr(observation, "observed_at", None)
        freshness = getattr(observation, "freshness", None)
        freshness_value = getattr(freshness, "value", str(freshness or "")).lower()
        if freshness_value and freshness_value not in {"fresh", "unknown"}:
            fresh = False
            reasons.append(f"latest observation freshness={freshness_value}")
        quality = getattr(observation, "quality", None)
        quality_value = getattr(quality, "value", str(quality or "")).lower()
        if quality_value in {"invalid", "suspect"}:
            quality_usable = False
            reasons.append(f"latest observation quality={quality_value}")
    if price is None:
        if latest_error:
            reasons.append(f"latest unavailable: {latest_error}")
        if not config.allow_bar_fallback or not bars:
            raise ExecutionPricingError(
                "no usable latest market price"
                + (f" ({latest_error})" if latest_error and not bars else "")
            )
        usable = [bar for bar in bars if not getattr(bar, "partial", False)]
        if not usable:
            raise ExecutionPricingError("no completed historical bar for fallback")
        price = money(getattr(usable[-1], "close"))
        observed = getattr(usable[-1], "observed_at", None)
        source = "nm02.bar.close.fallback"
        is_fallback = True
        reasons.append("explicit bar-close fallback configured")

    if observed is None:
        raise ExecutionPricingError("market observation has no timestamp")
    if getattr(observed, "tzinfo", None) is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = (now - observed).total_seconds()
    if config.require_fresh_data:
        if age > config.stale_after_seconds:
            raise ExecutionPricingError(
                f"market data is stale by {age:.0f}s (limit {config.stale_after_seconds}s)"
            )
        if not fresh:
            raise ExecutionPricingError("market data is not fresh: " + "; ".join(reasons))
        if not quality_usable:
            raise ExecutionPricingError("market data quality is not usable: " + "; ".join(reasons))
    else:
        reasons.append("freshness policy disabled by execution configuration")

    closes: list[Decimal] = []
    for bar in bars:
        close = getattr(bar, "close", None)
        if close is not None and not getattr(bar, "partial", False):
            closes.append(_decimal(close, name="bar close"))
    if not closes and price:
        closes = [price]
    volatility = _volatility(closes)
    volume = _volume_from_bars(bars)
    if volume <= 0:
        volume = Decimal("0")
        reasons.append("no historical traded volume; paper liquidity is unavailable")
    available = quantity(volume * config.participation_rate)
    if available < config.minimum_liquidity_quantity:
        available = Decimal("0")
    currency = getattr(instrument, "currency", "INR") or "INR"
    return MarketSnapshot(
        instrument_id=instrument_id,
        reference_price=price,
        observed_at=observed,
        source=source,
        volume=volume,
        available_quantity=available,
        volatility_pct=volatility,
        trading_status=status,
        currency=currency,
        is_fallback=is_fallback,
        fresh=fresh,
        reasons=tuple(reasons),
    )


def fee_breakdown(notional: Decimal, side: OrderSide, config: ExecutionConfig) -> dict[str, Decimal]:
    """Per-component, deterministic illustrative Indian cash-equity fees."""
    gross = money(notional)
    bps = Decimal("10000")
    brokerage = money(gross * config.brokerage_bps / bps)
    exchange = money(gross * config.exchange_bps / bps)
    sebi = money(gross * config.sebi_bps / bps)
    stamp = money(gross * config.stamp_duty_bps / bps) if side is OrderSide.BUY else Decimal("0.00")
    gst = money((brokerage + exchange + sebi) * config.gst_percent / Decimal("100"))
    components = {
        "brokerage": brokerage,
        "exchange_transaction": exchange,
        "sebi_turnover": sebi,
        "stamp_duty": money(stamp),
        "gst": gst,
    }
    total = money(sum(components.values(), Decimal("0")))
    if total < config.minimum_total_fee:
        components["minimum_fee_adjustment"] = money(config.minimum_total_fee - total)
        total = money(config.minimum_total_fee)
    else:
        components["minimum_fee_adjustment"] = Decimal("0.00")
    components["total"] = total
    return components


def execution_price(
    snapshot: MarketSnapshot,
    *,
    side: OrderSide,
    order_type: OrderType,
    quantity: Decimal,
    config: ExecutionConfig,
    limit_price: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (execution price, slippage amount per unit, slippage percent).

    The model is half of a disclosed synthetic spread plus square-root impact.
    There is no order-book depth and no bid/ask feed, so this must be labelled an
    approximation.  A limit is a hard cap: a buy never pays above it and a sell
    never receives below it; if the modelled price violates the limit the caller
    must not fill.
    """
    if quantity <= 0:
        raise ExecutionPricingError("fill quantity must be positive")
    reference = snapshot.reference_price
    if reference <= 0:
        raise ExecutionPricingError("reference price must be positive")
    available = snapshot.available_quantity
    if available <= 0:
        raise ExecutionPricingError("no available simulated liquidity")
    participation = quantity / available
    if participation > 1:
        participation = Decimal("1")
    import math

    root = Decimal(str(math.sqrt(float(participation))))
    vol_scale = Decimal("1") + (snapshot.volatility_pct / Decimal("100")) * config.volatility_weight
    impact_bps = config.impact_coefficient_bps * root * vol_scale
    half_spread = config.spread_bps / Decimal("2")
    raw_bps = half_spread + impact_bps
    if raw_bps > config.maximum_slippage_bps:
        raw_bps = config.maximum_slippage_bps
    direction = Decimal("1") if side is OrderSide.BUY else Decimal("-1")
    price = money(reference * (Decimal("1") + direction * raw_bps / Decimal("10000")))
    if price <= 0:
        raise ExecutionPricingError("modelled execution price is not positive")
    if limit_price is not None:
        limit = money(limit_price)
        if side is OrderSide.BUY and price > limit:
            price = limit
        elif side is OrderSide.SELL and price < limit:
            price = limit
    slip_per_unit = abs(price - reference)
    slip_pct = (slip_per_unit / reference * Decimal("100")).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_EVEN
    )
    return price, money(slip_per_unit), slip_pct


def exits_limit(
    *, side: OrderSide, price: Decimal, limit_price: Decimal | None
) -> bool:
    if limit_price is None:
        return True
    if side is OrderSide.BUY:
        return price <= limit_price
    return price >= limit_price
