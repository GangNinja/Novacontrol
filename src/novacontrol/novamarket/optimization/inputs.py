"""Authoritative input adapters for PI-18.

The optimizer never queries a broker or invents a price.  This module turns the
read-only NM-07 snapshot, NM-02 bars, PI-04 regime/feature records and
PI-05..PI-10 forecasts into validated research inputs, and reports every
exclusion reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import (
    FORECAST_QUALITY_RANK,
    AllocationMethod,
    ExpectedReturn,
    OptimizationConfig,
    OptimizationValidationError,
    PortfolioPosition,
    PortfolioSnapshot,
    PriceSeries,
    canonical_horizon,
    digest_payload,
    optional_time,
    stable_id,
    text,
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _number(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _first_number(*values: object) -> float | None:
    for value in values:
        result = _number(value)
        if result is not None:
            return result
    return None


def _dt(value: object) -> datetime | None:
    try:
        return optional_time(value)
    except OptimizationValidationError:
        return None


def portfolio_snapshot(
    detail: Mapping[str, Any],
    *,
    portfolio_id: str,
    as_of: datetime,
    pending_orders: Sequence[Mapping[str, Any]] = (),
) -> PortfolioSnapshot:
    portfolio = _mapping(detail.get("portfolio"))
    valuation = _mapping(detail.get("valuation"))
    cash = _number(valuation.get("cash"))
    total_value = _number(valuation.get("total_value"))
    invested = _number(valuation.get("invested"))
    if cash is None or total_value is None or total_value <= 0:
        raise OptimizationValidationError(
            "the authoritative portfolio valuation is missing a usable cash/total value"
        )
    realized = (
        _number(valuation.get("realized_pnl")) or _number(portfolio.get("realized_pnl")) or 0.0
    )
    unrealized = _number(valuation.get("unrealized_pnl")) or 0.0
    positions: list[PortfolioPosition] = []
    stale = False
    for row in _rows(detail.get("holdings")):
        instrument_id = text(row.get("instrument_id"))
        if not instrument_id:
            continue
        price = _number(row.get("last_price"))
        value = _number(row.get("market_value"))
        weight = _number(row.get("weight"))
        position_stale = bool(row.get("stale")) or price is None or value is None
        stale = stale or position_stale
        positions.append(
            PortfolioPosition(
                instrument_id=instrument_id,
                quantity=_number(row.get("quantity")) or 0.0,
                average_cost=_number(row.get("average_cost")) or 0.0,
                price=price,
                market_value=value,
                weight=weight if weight is not None else 0.0,
                sector=text(row.get("sector"), "unknown").lower(),
                asset_class=text(row.get("asset_class"), "unknown").lower(),
                stale=position_stale,
                priced_at=_dt(row.get("priced_at")),
            )
        )
    reserve = 0.0
    useful_orders: list[dict[str, Any]] = []
    for row in pending_orders:
        body = dict(row)
        status = text(body.get("status"), "validated").lower()
        if status not in {"validated", "pending", "pending_validation", "accepted", "open"}:
            continue
        side = text(body.get("side")).lower()
        intents = _rows(body.get("intents"))
        notional = _number(body.get("notional")) or 0.0
        for intent in intents:
            intent_side = text(intent.get("side")).lower()
            if intent_side == "buy":
                reserve += _number(intent.get("notional")) or 0.0
        if side == "buy":
            reserve += notional
        useful_orders.append(body)
    currency = text(valuation.get("currency") or portfolio.get("base_currency"), "INR").upper()
    if currency != "INR":
        raise OptimizationValidationError(
            f"portfolio valuation currency {currency} is not supported; expected INR"
        )
    version = text(
        valuation.get("snapshot_id"),
        stable_id(
            "pos_",
            portfolio_id,
            valuation.get("as_of") or portfolio.get("updated_at") or as_of.isoformat(),
            cash,
            total_value,
            len(positions),
        ),
    )
    return PortfolioSnapshot(
        portfolio_id=portfolio_id,
        as_of=as_of,
        version=version,
        cash=cash,
        invested=invested if invested is not None else max(0.0, total_value - cash),
        total_value=total_value,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        positions=tuple(positions),
        pending_orders=tuple(useful_orders),
        pending_buy_reserve=reserve,
        currency=currency,
        stale=stale,
    )


def price_series(
    context: Mapping[str, Any],
    instrument_id: str,
    *,
    interval: str,
    as_of: datetime,
) -> PriceSeries:
    raw = context.get("bars")
    value: object = None
    if isinstance(raw, Mapping):
        value = raw.get(instrument_id)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        value = [
            row
            for row in raw
            if isinstance(row, Mapping) and text(row.get("instrument_id")) == instrument_id
        ]
    rows = _rows(_mapping(value).get("bars") if isinstance(value, Mapping) else value)
    by_time: dict[datetime, tuple[float, float | None]] = {}
    currencies: set[str] = set()
    for row in rows:
        close = _number(row.get("close") or row.get("price") or row.get("last"))
        observed = _dt(row.get("observed_at") or row.get("at") or row.get("timestamp"))
        if close is None or close <= 0 or observed is None:
            continue
        if observed > as_of:
            continue
        volume = _number(row.get("volume"))
        currency = text(row.get("currency")).upper()
        if currency:
            currencies.add(currency)
        by_time[observed] = (close, volume)
    ordered = sorted(by_time)
    currency = "MIXED" if len(currencies) > 1 else next(iter(currencies), "INR")
    return PriceSeries(
        instrument_id=instrument_id,
        interval=interval,
        observed_at=tuple(ordered),
        close=tuple(by_time[item][0] for item in ordered),
        volume=tuple(by_time[item][1] for item in ordered),
        source_reference=text(_mapping(value).get("source_reference")),
        currency=currency,
    )


def instrument_metadata(context: Mapping[str, Any], instrument_id: str) -> Mapping[str, Any]:
    raw = context.get("instruments")
    if isinstance(raw, Mapping):
        value = raw.get(instrument_id)
        if isinstance(value, Mapping):
            return value
    rows = _rows(raw)
    for row in rows:
        if text(row.get("instrument_id")) == instrument_id:
            return row
    return {}


def forecast_input(
    rows: Sequence[Mapping[str, Any]],
    *,
    instrument_id: str,
    horizon: str,
    as_of: datetime,
    max_age_hours: float,
    current_price: float | None,
) -> tuple[ExpectedReturn | None, tuple[str, ...]]:
    warnings: list[str] = []
    usable: list[Mapping[str, Any]] = []
    resolved_returns: list[float] = []
    for row in rows:
        row_horizon = canonical_horizon(row.get("horizon") or row.get("source_horizon") or horizon)
        if row_horizon != horizon:
            continue
        generated = _dt(
            row.get("available_at")
            or row.get("generated_at")
            or row.get("created_at")
            or row.get("forecast_generation_timestamp")
        )
        if generated is None:
            warnings.append("forecast without a generation timestamp was excluded")
            continue
        if generated > as_of:
            warnings.append("future forecast was excluded")
            continue
        age_hours = max(0.0, (as_of - generated).total_seconds() / 3600.0)
        if age_hours > max_age_hours:
            warnings.append(f"stale forecast excluded ({age_hours:.1f}h old)")
            continue
        validity = text(row.get("validity") or row.get("forecast_validity"), "valid").lower()
        if validity in {"expired", "invalid", "revoked"}:
            warnings.append(f"forecast validity is {validity}")
            continue
        target_at = _dt(row.get("target_at"))
        if target_at is not None and target_at <= as_of:
            warnings.append("forecast target already passed")
            continue
        expected = _first_number(
            row.get("expected_return"),
            row.get("predicted_return"),
            row.get("expected_return_fraction"),
        )
        if expected is None:
            target_price = _number(row.get("target_price") or row.get("predicted_price"))
            if target_price is not None and current_price and current_price > 0:
                expected = target_price / current_price - 1.0
        if expected is None:
            point = _number(row.get("expected_price_change") or row.get("predicted_price_change"))
            if point is not None and current_price and current_price > 0:
                expected = point / current_price
        if expected is None:
            warnings.append("forecast has no usable expected return and was excluded")
            continue
        if not -1.0 < expected < 5.0:
            warnings.append("forecast expected return is outside the supported range")
            continue
        usable.append(row)
        resolved_returns.append(float(expected))
    if not usable:
        return None, tuple(dict.fromkeys(warnings))
    raw_return = sum(resolved_returns) / len(resolved_returns)
    uncertainty_rows = [
        text(row.get("uncertainty") or row.get("forecast_uncertainty"), "unknown").lower()
        for row in usable
    ]
    uncertainty = next(
        (item for item in ("very_high", "high", "medium", "low") if item in uncertainty_rows),
        uncertainty_rows[0] if uncertainty_rows else "unknown",
    )
    widths: list[float] = []
    for row in usable:
        width = _number(row.get("interval_width"))
        if width is None:
            lower = _number(row.get("lower_bound") or row.get("prediction_lower"))
            upper = _number(row.get("upper_bound") or row.get("prediction_upper"))
            if lower is not None and upper is not None:
                width = abs(upper - lower)
        if width is not None and current_price and current_price > 0:
            width = width / current_price
        if width is not None:
            widths.append(abs(width))
    interval_width = sum(widths) / len(widths) if widths else None
    penalty = {"low": 0.15, "medium": 0.35, "high": 0.60, "very_high": 0.85}.get(uncertainty, 0.35)
    if interval_width is not None:
        penalty += min(1.5, interval_width * 2.0)
    adjusted = raw_return / (1.0 + penalty)
    agreement = (
        max(resolved_returns) - min(resolved_returns) if len(resolved_returns) >= 2 else None
    )
    calibrated: list[float] = []
    for row in usable:
        probability = _number(row.get("p_up"))
        status = text(
            row.get("probability_status") or row.get("calibration") or row.get("calibration_status")
        ).lower()
        if probability is not None and status == "calibrated":
            calibrated.append(probability)
    generated_values = [
        _dt(row.get("available_at") or row.get("generated_at") or row.get("created_at"))
        for row in usable
    ]
    generated_at = max(
        (value for value in generated_values if value is not None),
        default=None,
    )
    computed_quality = (
        "high"
        if len(usable) >= 2 and (agreement is None or agreement <= 0.05)
        else "medium"
        if usable
        else "missing"
    )
    declared = [text(row.get("quality") or row.get("forecast_quality")).lower() for row in usable]
    declared_rank = min(
        (FORECAST_QUALITY_RANK[item] for item in declared if item in FORECAST_QUALITY_RANK),
        default=FORECAST_QUALITY_RANK["high"],
    )
    computed_rank = FORECAST_QUALITY_RANK.get(computed_quality, 0)
    quality = next(
        name
        for name, rank in FORECAST_QUALITY_RANK.items()
        if rank == min(computed_rank, declared_rank)
    )
    return (
        ExpectedReturn(
            instrument_id=instrument_id,
            horizon=horizon,
            expected_return=raw_return,
            adjusted_return=adjusted,
            uncertainty=uncertainty,
            interval_width=interval_width,
            forecast_ids=tuple(
                dict.fromkeys(
                    text(row.get("forecast_id") or row.get("ensemble_id") or row.get("id"))
                    for row in usable
                    if text(row.get("forecast_id") or row.get("ensemble_id") or row.get("id"))
                )
            ),
            model_versions=tuple(
                dict.fromkeys(
                    text(row.get("model_version") or row.get("model_id"))
                    for row in usable
                    if text(row.get("model_version") or row.get("model_id"))
                )
            ),
            generated_at=generated_at,
            validity="valid",
            calibrated_probability=(sum(calibrated) / len(calibrated) if calibrated else None),
            probability_is_calibrated=bool(calibrated),
            disagreement=agreement,
            quality=quality,
            warnings=tuple(dict.fromkeys(warnings)),
        ),
        tuple(dict.fromkeys(warnings)),
    )


@dataclass(frozen=True, slots=True)
class EligibleAsset:
    instrument_id: str
    exchange: str
    sector: str
    asset_class: str
    price: float
    series: PriceSeries
    forecast: ExpectedReturn | None
    current_weight: float
    current_value: float
    liquidity_cap_value: float | None
    liquidity_status: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "sector": self.sector,
            "asset_class": self.asset_class,
            "price": self.price,
            "currency": self.series.currency,
            "observations": len(self.series.close),
            "current_weight": self.current_weight,
            "current_value": self.current_value,
            "liquidity_cap_value": self.liquidity_cap_value,
            "liquidity_status": self.liquidity_status,
            "forecast": None if self.forecast is None else self.forecast.to_dict(),
            "reasons": list(self.reasons),
        }


def eligible_universe(
    context: Mapping[str, Any],
    snapshot: PortfolioSnapshot,
    config: OptimizationConfig,
    *,
    as_of: datetime,
) -> tuple[list[EligibleAsset], list[dict[str, Any]], dict[str, PriceSeries]]:
    """Filter the requested universe with explicit exclusion reasons."""
    forecasts_raw = context.get("forecasts")
    forecast_map: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(forecasts_raw, Mapping):
        for key, value in forecasts_raw.items():
            if isinstance(value, Mapping):
                forecast_map[text(key)] = _rows(value.get("forecasts") or value.get("items"))
            else:
                forecast_map[text(key)] = _rows(value)
    elif isinstance(forecasts_raw, Sequence) and not isinstance(
        forecasts_raw, (str, bytes, bytearray)
    ):
        for row in _rows(forecasts_raw):
            forecast_map.setdefault(text(row.get("instrument_id")), []).append(row)
    positions = {item.instrument_id: item for item in snapshot.positions}
    eligible: list[EligibleAsset] = []
    excluded: list[dict[str, Any]] = []
    series_map: dict[str, PriceSeries] = {}
    requested = list(config.instrument_ids)
    for instrument_id in requested[config.max_instruments :]:
        excluded.append(
            {
                "instrument_id": instrument_id,
                "reasons": ["outside the configured max_instruments universe"],
            }
        )
    for instrument_id in requested[: config.max_instruments]:
        reasons: list[str] = []
        exchange = instrument_id.split(":", 1)[0].upper()
        metadata = instrument_metadata(context, instrument_id)
        exchange = text(metadata.get("exchange") or exchange).upper()
        if exchange not in config.allowed_exchanges:
            excluded.append(
                {"instrument_id": instrument_id, "reasons": [f"exchange {exchange} is not allowed"]}
            )
            continue
        status = text(metadata.get("trading_status"), "unknown").lower()
        if status in {"delisted", "halted", "closed"}:
            reasons.append(f"instrument trading status is {status}")
        series = price_series(
            context,
            instrument_id,
            interval=str(context.get("interval") or _interval_for(config.horizon)),
            as_of=as_of,
        )
        series_map[instrument_id] = series
        if not series.close:
            excluded.append(
                {
                    "instrument_id": instrument_id,
                    "reasons": reasons + ["no usable price observations at or before as_of"],
                }
            )
            continue
        price = series.close[-1]
        observed = series.latest_at
        if observed is None:
            reasons.append("price observation has no timestamp")
        else:
            age_hours = max(0.0, (as_of - observed).total_seconds() / 3600.0)
            if age_hours > config.max_price_age_hours:
                reasons.append(f"price is stale ({age_hours:.1f}h old)")
        if len(series.close) - 1 < config.min_history_observations:
            reasons.append(
                f"insufficient price history ({len(series.close) - 1} returns; "
                f"need {config.min_history_observations})"
            )
        forecast, forecast_warnings = forecast_input(
            forecast_map.get(instrument_id, ()),
            instrument_id=instrument_id,
            horizon=config.horizon,
            as_of=as_of,
            max_age_hours=config.max_forecast_age_hours,
            current_price=price,
        )
        requires_forecast = _requires_forecast(config)
        if forecast is None:
            if requires_forecast:
                reasons.extend(forecast_warnings or ("no horizon-compatible forecast",))
                excluded.append({"instrument_id": instrument_id, "reasons": reasons})
                continue
        else:
            minimum_quality = FORECAST_QUALITY_RANK.get(config.min_forecast_quality, 1)
            actual_quality = FORECAST_QUALITY_RANK.get(forecast.quality, 0)
            if actual_quality < minimum_quality:
                reasons.append(
                    f"forecast quality {forecast.quality} is below the configured minimum "
                    f"{config.min_forecast_quality}"
                )
            if (
                config.min_expected_return is not None
                and (forecast.adjusted_return or 0.0) < config.min_expected_return
            ):
                reasons.append(
                    f"adjusted expected return {forecast.adjusted_return:.4f} is below the minimum"
                )
        if series.currency == "MIXED":
            reasons.append("price series contains inconsistent currencies")
        elif series.currency != "INR":
            reasons.append(f"price currency {series.currency} is not INR")
        position = positions.get(instrument_id)
        current_weight = position.weight if position else 0.0
        current_value = position.market_value if position and position.market_value else 0.0
        volumes = [
            float(volume) * float(price_value)
            for volume, price_value in zip(series.volume, series.close, strict=False)
            if volume is not None
        ]
        average_bar_notional = sum(volumes) / len(volumes) if volumes else None
        bars_per_day = float(_bars_per_day(config.horizon))
        average_notional = (
            average_bar_notional * bars_per_day if average_bar_notional is not None else None
        )
        cap = (
            average_notional * config.max_participation_rate
            if average_notional is not None
            else None
        )
        liquidity_status = "estimated" if cap is not None else "missing"
        if cap is None:
            if config.liquidity_policy == "exclude":
                reasons.append("no reliable liquidity observation")
        elif cap <= 0:
            reasons.append("estimated liquidity capacity is zero")
        sector = (position.sector if position else "") or text(
            metadata.get("sector"), "unknown"
        ).lower()
        if reasons:
            excluded.append({"instrument_id": instrument_id, "reasons": reasons})
            continue
        eligible.append(
            EligibleAsset(
                instrument_id=instrument_id,
                exchange=exchange,
                sector=sector,
                asset_class=(position.asset_class if position else "")
                or text(metadata.get("asset_class"), "unknown").lower(),
                price=price,
                series=series,
                forecast=forecast,
                current_weight=current_weight,
                current_value=current_value,
                liquidity_cap_value=cap,
                liquidity_status=liquidity_status,
                reasons=tuple(forecast_warnings),
            )
        )
    return eligible, excluded, series_map


def _interval_for(horizon: str) -> str:
    from .models import HORIZONS

    return str(HORIZONS[canonical_horizon(horizon)]["interval"])


def _bars_per_day(horizon: str) -> float:
    from .models import HORIZONS

    return float(HORIZONS[canonical_horizon(horizon)].get("bars_per_day", 1.0))


def _requires_forecast(config: OptimizationConfig) -> bool:
    """Only objectives/methods that actually consume expected return require it."""
    if config.method != AllocationMethod.MEAN_VARIANCE.value:
        return False
    return config.objective in {
        "max_expected_return",
        "risk_adjusted",
        "max_sharpe",
        "min_downside_risk",
        "multi_objective",
    }


def snapshot_digest(snapshot: PortfolioSnapshot) -> str:
    return digest_payload(snapshot.to_dict())


__all__ = [
    "EligibleAsset",
    "eligible_universe",
    "forecast_input",
    "instrument_metadata",
    "portfolio_snapshot",
    "price_series",
    "snapshot_digest",
]
