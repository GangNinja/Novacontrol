"""Deterministic PI-22 analytics.

The functions are pure: they receive normalised observations and return
measurements with explicit sample sizes, assumptions and unavailable states.
NM-08's metric implementations are reused for volatility, VaR/CVaR, HHI and
pairwise correlation so PI-22 does not create competing definitions.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from novacontrol.novamarket.risk.metrics import (
    annualized_volatility,
    historical_cvar,
    historical_var,
)
from novacontrol.novamarket.risk.metrics import (
    concentration as nm08_concentration,
)
from novacontrol.novamarket.risk.metrics import (
    correlation as nm08_correlation,
)
from novacontrol.novamarket.risk_intelligence.models import (
    LiquidityClass,
    RiskLevel,
    RiskThresholds,
    number,
    parse_time,
)


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in ("items", "bars", "rows", "results", "data"):
            body = value.get(key)
            if isinstance(body, Sequence) and not isinstance(body, (str, bytes, bytearray)):
                return [row for row in body if isinstance(row, Mapping)]
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _get(row: object, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def normalise_bars(value: Any, *, max_points: int = 252) -> list[dict[str, Any]]:
    """Normalise OHLCV mappings/objects, rejecting invalid and partial bars."""

    points: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for raw in _rows(value):
        if bool(_get(raw, "partial", False)):
            continue
        quality = str(_get(raw, "quality", "") or "").lower()
        if quality in {"invalid", "stale", "suspect", "missing", "unavailable"}:
            continue
        at = parse_time(_get(raw, "observed_at") or _get(raw, "at") or _get(raw, "timestamp"))
        close = number(_get(raw, "close") or _get(raw, "price") or _get(raw, "last_price"))
        if at is None or close is None or close <= 0 or at in seen:
            continue
        high = number(_get(raw, "high")) or close
        low = number(_get(raw, "low")) or close
        open_ = number(_get(raw, "open")) or close
        if high < low or low <= 0 or high <= 0:
            continue
        if not low <= open_ <= high or not low <= close <= high:
            continue
        volume = number(_get(raw, "volume"))
        if volume is not None and volume < 0:
            volume = None
        provider = str(_get(raw, "provider", "") or "")
        points.append(
            {
                "at": at,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "provider": provider,
            }
        )
        seen.add(at)
    points.sort(key=lambda row: row["at"])
    return points[-max(1, min(int(max_points), 1000)) :]


def returns_by_time(points: Sequence[Mapping[str, Any]]) -> dict[datetime, float]:
    returns: dict[datetime, float] = {}
    for previous, current in zip(points, points[1:], strict=False):
        before = number(previous.get("close"))
        after = number(current.get("close"))
        at = current.get("at")
        if before is None or after is None or before <= 0 or not isinstance(at, datetime):
            continue
        returns[at] = (after / before) - 1.0
    return returns


def aligned_returns(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
    *,
    max_points: int = 252,
) -> list[tuple[datetime, float, float]]:
    """Pair returns only on identical observation timestamps.

    Two series with different trading calendars are intersected; they are never
    zipped positionally after a mismatch.
    """

    left_map = returns_by_time(left)
    right_map = returns_by_time(right)
    common = sorted(set(left_map) & set(right_map))
    return [(at, left_map[at], right_map[at]) for at in common[-max_points:]]


def _metric_value(metric: Any) -> dict[str, Any]:
    body = metric.to_dict()
    body["value"] = number(body.get("value"))
    return cast(dict[str, Any], body)


def volatility_metrics(
    points: Sequence[Mapping[str, Any]],
    thresholds: RiskThresholds,
    *,
    role: str = "position",
) -> dict[str, Any]:
    closes = [float(row["close"]) for row in points]
    returns = list(returns_by_time(points).values())
    minimum = int(thresholds.min_observations)
    vol = _metric_value(
        annualized_volatility(
            closes,
            minimum=minimum,
            periods_per_year=int(thresholds.annualization_periods),
        )
    )
    var = _metric_value(
        historical_var(returns, confidence=float(thresholds.var_confidence), minimum=minimum)
    )
    cvar = _metric_value(
        historical_cvar(returns, confidence=float(thresholds.var_confidence), minimum=minimum)
    )
    downside = [min(0.0, value) for value in returns]
    downside_deviation = None
    if len(returns) >= minimum:
        downside_deviation = math.sqrt(sum(value**2 for value in downside) / len(downside))
    level = RiskLevel.INSUFFICIENT_DATA.value
    if vol["sufficient"] and vol["value"] is not None:
        if float(vol["value"]) >= float(thresholds.volatility_high_pct):
            level = RiskLevel.HIGH.value
        elif float(vol["value"]) >= float(thresholds.volatility_warning_pct):
            level = RiskLevel.ELEVATED.value
        else:
            level = RiskLevel.NORMAL.value
    return {
        "role": role,
        "observations": len(returns),
        "annualized_volatility_pct": vol,
        "downside_deviation_pct": None
        if downside_deviation is None
        else round(downside_deviation * 100.0, 6),
        "var": var,
        "cvar": cvar,
        "risk_level": level,
        "distribution_assumption": "historical simulation; no normal-distribution assumption",
        "var_is_not_maximum_loss": True,
        "methodology": {
            "confidence": str(thresholds.var_confidence),
            "window": f"last {min(len(closes) - 1, int(thresholds.max_series_points))} returns",
            "annualization_periods": int(thresholds.annualization_periods),
            "sample_size": len(returns),
            "minimum_sample": minimum,
        },
        "limitations": [
            "Historical estimates are not forecasts.",
            "VaR is a quantile loss, not the maximum possible loss.",
            "The result depends on the stored observation window and venue.",
        ],
    }


def pairwise_correlation(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
    thresholds: RiskThresholds,
) -> dict[str, Any]:
    pairs = aligned_returns(left, right, max_points=int(thresholds.max_series_points))
    metric = _metric_value(
        nm08_correlation(
            [row[1] for row in pairs],
            [row[2] for row in pairs],
            minimum=int(thresholds.min_correlation_observations),
        )
    )
    value = metric["value"]
    if value is None:
        level = RiskLevel.INSUFFICIENT_DATA.value
        change = None
        trend = "unknown"
    else:
        if float(value) >= float(thresholds.correlation_high):
            level = RiskLevel.HIGH.value
        elif float(value) >= float(thresholds.correlation_warning):
            level = RiskLevel.ELEVATED.value
        else:
            level = RiskLevel.NORMAL.value
        change, trend = _correlation_change(pairs, thresholds)
    return {
        **metric,
        "paired_observations": len(pairs),
        "risk_level": level,
        "change": change,
        "change_state": trend,
        "alignment": "exact timestamp intersection; unmatched observations are excluded",
        "limitations": [
            "Historical correlation is not stable and is not a forecast.",
            "Different calendars reduce the paired sample rather than shifting it.",
        ],
    }


def _correlation_change(
    pairs: Sequence[tuple[datetime, float, float]], thresholds: RiskThresholds
) -> tuple[float | None, str]:
    window = int(thresholds.rolling_window)
    minimum = int(thresholds.min_correlation_observations)
    if len(pairs) < max(minimum, window * 2):
        return None, "unknown"
    latest = pairs[-window:]
    previous = pairs[-2 * window : -window]
    current_metric = nm08_correlation(
        [row[1] for row in latest], [row[2] for row in latest], minimum=minimum
    )
    previous_metric = nm08_correlation(
        [row[1] for row in previous], [row[2] for row in previous], minimum=minimum
    )
    current = number(current_metric.value)
    prior = number(previous_metric.value)
    if current is None or prior is None:
        return None, "unknown"
    change = round(current - prior, 6)
    if change >= float(thresholds.correlation_change_warning):
        return change, "increasing"
    if change <= -float(thresholds.correlation_change_warning):
        return change, "breakdown"
    return change, "normal"


def drawdown_episodes(
    values: Sequence[Mapping[str, Any] | float | int | str],
) -> dict[str, Any]:
    """Peak-to-trough episodes; values must be authoritative valuations."""

    rows: list[tuple[datetime | None, float]] = []
    for item in values:
        if isinstance(item, Mapping):
            at = parse_time(item.get("at") or item.get("created_at") or item.get("valuation_at"))
            value = number(item.get("total_value") or item.get("value"))
        else:
            at = None
            value = number(item)
        if value is not None and value > 0:
            rows.append((at, value))
    if not rows:
        return {
            "status": "insufficient_data",
            "current_drawdown_pct": None,
            "max_drawdown_pct": None,
            "episodes": [],
            "reason": "no authoritative positive valuation observations",
        }
    if any(at is not None for at, _value in rows):
        # The caller supplies chronological history today, but the calculation
        # must not silently reverse if an authoritative source changes order.
        rows.sort(key=lambda item: (item[0] is None, item[0] or datetime.min.replace(tzinfo=UTC)))
    peak = rows[0][1]
    peak_at = rows[0][0]
    in_drawdown = False
    trough = peak
    trough_at = peak_at
    episodes: list[dict[str, Any]] = []
    worst = 0.0
    for at, value in rows[1:]:
        if value >= peak:
            if in_drawdown:
                episodes.append(
                    _episode(peak, peak_at, trough, trough_at, value, at, recovered=True)
                )
                in_drawdown = False
            peak = value
            peak_at = at
            trough = value
            trough_at = at
            continue
        if not in_drawdown or value < trough:
            if not in_drawdown:
                in_drawdown = True
            trough = value
            trough_at = at
        drawdown = (peak - value) / peak * 100.0
        worst = max(worst, drawdown)
    if in_drawdown:
        episodes.append(
            _episode(peak, peak_at, trough, trough_at, rows[-1][1], rows[-1][0], recovered=False)
        )
    latest = rows[-1][1]
    current_peak = max(value for _at, value in rows)
    current = max(0.0, (current_peak - latest) / current_peak * 100.0)
    duration_days = None
    if rows[-1][0] and peak_at:
        duration_days = max(0.0, (rows[-1][0] - peak_at).total_seconds() / 86400.0)
    max_drawdown_nc = max(
        (
            float(episode["peak_value_nc"]) * float(episode["drawdown_pct"]) / 100.0
            for episode in episodes
        ),
        default=0.0,
    )
    return {
        "status": "completed" if len(rows) >= 2 else "insufficient_data",
        "observations": len(rows),
        "peak_value_nc": round(current_peak, 2),
        "current_value_nc": round(latest, 2),
        "current_drawdown_pct": round(current, 6),
        "max_drawdown_pct": round(worst, 6),
        "max_drawdown_nc": round(max_drawdown_nc, 2),
        "current_drawdown_nc": round(max(0.0, current_peak - latest), 2),
        "drawdown_duration_days": None if duration_days is None else round(duration_days, 2),
        "episodes": episodes[-20:],
        "methodology": "peak-to-trough declines of authoritative valuation snapshots",
        "limitations": ["Valuation history is used as stored; no prices are reconstructed."],
    }


def _episode(
    peak: float,
    peak_at: datetime | None,
    trough: float,
    trough_at: datetime | None,
    end_value: float,
    end_at: datetime | None,
    *,
    recovered: bool,
) -> dict[str, Any]:
    loss = max(0.0, (peak - trough) / peak) if peak > 0 else 0.0
    duration = None
    if peak_at and end_at:
        duration = max(0.0, (end_at - peak_at).total_seconds() / 86400.0)
    return {
        "peak_at": peak_at.isoformat() if peak_at else None,
        "trough_at": trough_at.isoformat() if trough_at else None,
        "recovery_at": end_at.isoformat() if recovered and end_at else None,
        "peak_value_nc": round(peak, 2),
        "trough_value_nc": round(trough, 2),
        "end_value_nc": round(end_value, 2),
        "drawdown_pct": round(loss * 100.0, 6),
        "duration_days": None if duration is None else round(duration, 2),
        "recovered": recovered,
    }


def concentration_metrics(
    positions: Sequence[Mapping[str, Any]], thresholds: RiskThresholds
) -> dict[str, Any]:
    weights = [max(0.0, number(row.get("weight")) or 0.0) for row in positions]
    if not any(weights):
        return {
            "status": "insufficient_data",
            "reason": "no positive position weights",
            "hhi": None,
            "effective_holdings": None,
            "top_one_weight": None,
            "top_three_weight": None,
        }
    total = sum(weights)
    normalised = [value / total for value in weights]
    hhi_metric = nm08_concentration(normalised).to_dict()
    hhi = number(hhi_metric.get("value"))
    top_one = max(normalised)
    top_three = sum(sorted(normalised, reverse=True)[:3])
    sector_weights: dict[str, float] = {}
    industry_weights: dict[str, float] = {}
    for row, weight in zip(positions, normalised, strict=True):
        sector = str(row.get("sector") or "unknown")
        industry = str(row.get("industry") or sector or "unknown")
        sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
        industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
    sector_top = max(sector_weights.values()) if sector_weights else 0.0
    industry_top = max(industry_weights.values()) if industry_weights else 0.0
    level = RiskLevel.NORMAL.value
    if (
        top_one >= float(thresholds.max_position_weight)
        or sector_top >= float(thresholds.max_sector_weight)
        or industry_top >= float(thresholds.max_industry_weight)
        or top_three >= float(thresholds.top_three_high)
    ):
        level = RiskLevel.HIGH.value
    elif (
        (hhi is not None and hhi >= float(thresholds.hhi_warning))
        or top_three >= float(thresholds.top_three_warning)
        or sector_top >= float(thresholds.max_sector_weight) * 0.8
    ):
        level = RiskLevel.ELEVATED.value
    if hhi is not None and hhi >= float(thresholds.hhi_high):
        level = RiskLevel.HIGH.value
    return {
        "status": "completed",
        "hhi": None if hhi is None else round(hhi, 6),
        "hhi_kind": "nm08_herfindahl_hirschman",
        "effective_holdings": None if not hhi else round(1.0 / hhi, 4),
        "top_one_weight": round(top_one, 6),
        "top_three_weight": round(top_three, 6),
        "largest_sector": max(sector_weights, key=lambda key: sector_weights[key])
        if sector_weights
        else "",
        "largest_sector_weight": round(sector_top, 6),
        "largest_industry": max(industry_weights, key=lambda key: industry_weights[key])
        if industry_weights
        else "",
        "largest_industry_weight": round(industry_top, 6),
        "sector_weights": [
            {"sector": key, "weight": round(value, 6)}
            for key, value in sorted(sector_weights.items(), key=lambda item: -item[1])
        ],
        "industry_weights": [
            {"industry": key, "weight": round(value, 6)}
            for key, value in sorted(industry_weights.items(), key=lambda item: -item[1])
        ],
        "risk_level": level,
        "methodology": "normalised position weights and NM-08 HHI",
        "limitations": ["Sector/industry labels come from stored instrument metadata."],
    }


def liquidity_metrics(
    points: Sequence[Mapping[str, Any]],
    position_value: float | None,
    thresholds: RiskThresholds,
    *,
    spread_bps: float | None = None,
) -> dict[str, Any]:
    values = [
        float(row["volume"]) * float(row["close"])
        for row in points[-20:]
        if row.get("volume") is not None and row.get("close") is not None
    ]
    if len(values) < 5:
        return {
            "status": "insufficient_data",
            "liquidity_class": LiquidityClass.INSUFFICIENT_DATA.value,
            "risk_level": RiskLevel.INSUFFICIENT_DATA.value,
            "reason": f"{len(values)} bar(s) carry volume; at least 5 are required",
            "average_daily_value_nc": None,
            "position_value_nc": None if position_value is None else round(position_value, 2),
            "position_to_adv": None,
            "days_to_liquidate": None,
            "market_depth": "unavailable",
            "bid_ask_spread_bps": spread_bps,
            "estimated_impact_bps_range": None,
        }
    average_daily = statistics.fmean(values)
    ratio = None if not position_value or average_daily <= 0 else position_value / average_daily
    if ratio is None:
        cls = LiquidityClass.INSUFFICIENT_DATA.value
    elif ratio <= float(thresholds.liquidity_low_days):
        cls = LiquidityClass.HIGH_LIQUIDITY.value
    elif ratio <= float(thresholds.liquidity_high_days):
        cls = LiquidityClass.NORMAL_LIQUIDITY.value
    elif ratio <= float(thresholds.liquidity_very_low_days):
        cls = LiquidityClass.LOW_LIQUIDITY.value
    else:
        cls = LiquidityClass.VERY_LOW_LIQUIDITY.value
    level = RiskLevel.NORMAL.value
    if cls == LiquidityClass.VERY_LOW_LIQUIDITY.value:
        level = RiskLevel.CRITICAL.value
    elif cls == LiquidityClass.LOW_LIQUIDITY.value:
        level = RiskLevel.HIGH.value
    elif cls == LiquidityClass.INSUFFICIENT_DATA.value:
        level = RiskLevel.INSUFFICIENT_DATA.value
    impact_low = impact_high = None
    if ratio is not None:
        # Explicit participation assumptions; no order-book depth is claimed.
        impact_low = round(max(0.0, ratio / 0.20) * 10.0, 4)
        impact_high = round(max(0.0, ratio / 0.05) * 10.0, 4)
    return {
        "status": "completed",
        "liquidity_class": cls,
        "risk_level": level,
        "average_daily_value_nc": round(average_daily, 2),
        "position_value_nc": None if position_value is None else round(position_value, 2),
        "position_to_adv": None if ratio is None else round(ratio, 6),
        "days_to_liquidate": None if ratio is None else round(ratio, 4),
        "bid_ask_spread_bps": spread_bps,
        "market_depth": "unavailable" if spread_bps is None else "quote_only",
        "estimated_impact_bps_range": None
        if impact_low is None
        else {"low": impact_low, "high": impact_high},
        "assumptions": [
            "ADV is mean close × volume over up to 20 stored daily bars.",
            "Impact range assumes 5%–20% participation; it excludes an unobserved spread.",
            "No order-book depth is fabricated when the provider does not supply it.",
        ],
        "limitations": [
            "The full position may not be liquidated at the last traded price.",
            "Volume history may be incomplete or venue-specific.",
        ],
    }


def scenario_positions(
    positions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in positions:
        value = number(
            row.get("market_value")
            or row.get("market_value_nc")
            or row.get("current_value")
            or row.get("value")
        )
        weight = number(row.get("weight")) or 0.0
        if value is None or value < 0:
            continue
        result.append(
            {
                "instrument_id": str(row.get("instrument_id") or ""),
                "sector": str(row.get("sector") or "unknown"),
                "industry": str(row.get("industry") or row.get("sector") or "unknown"),
                "value_nc": float(value),
                "weight": weight,
            }
        )
    return result


def scenario_impact(
    positions: Sequence[Mapping[str, Any]],
    *,
    shock_pct: float,
    sector: str = "",
    instrument: str = "",
) -> dict[str, Any]:
    contributions: list[dict[str, Any]] = []
    total_loss = 0.0
    before = 0.0
    for row in scenario_positions(positions):
        value = float(row["value_nc"])
        before += value
        applies = True
        if sector:
            applies = row["sector"].lower() == sector.lower()
        if instrument:
            applies = row["instrument_id"].upper() == instrument.upper()
        change = value * (shock_pct / 100.0) if applies else 0.0
        total_loss += change
        if applies:
            contributions.append(
                {
                    "instrument_id": row["instrument_id"],
                    "sector": row["sector"],
                    "before_value_nc": round(value, 2),
                    "estimated_change_nc": round(change, 2),
                    "estimated_value_nc": round(value + change, 2),
                }
            )
    after = before + total_loss
    return {
        "portfolio_value_before_nc": round(before, 2),
        "estimated_portfolio_value_nc": round(after, 2),
        "estimated_change_nc": round(total_loss, 2),
        "estimated_change_pct": round((total_loss / before * 100.0) if before > 0 else 0.0, 6),
        "position_contributions": sorted(contributions, key=lambda row: row["estimated_change_nc"]),
        "hypothetical": True,
        "is_prediction": False,
    }


def data_freshness(
    timestamps: Sequence[object], *, now: datetime, stale_hours: float
) -> dict[str, Any]:
    parsed = [value for value in (parse_time(item) for item in timestamps) if value]
    if not parsed:
        return {
            "status": "unavailable",
            "oldest_at": None,
            "newest_at": None,
            "age_hours": None,
            "stale": True,
            "reason": "no timestamped observations",
        }
    oldest = min(parsed)
    newest = max(parsed)
    age = max(0.0, (now - newest).total_seconds() / 3600.0)
    return {
        "status": "stale" if age > stale_hours else "fresh",
        "oldest_at": oldest.isoformat(),
        "newest_at": newest.isoformat(),
        "age_hours": round(age, 4),
        "stale": age > stale_hours,
        "threshold_hours": stale_hours,
    }


def level_from_drawdown(value: float | None, thresholds: RiskThresholds) -> str:
    if value is None:
        return RiskLevel.INSUFFICIENT_DATA.value
    if value >= float(thresholds.drawdown_critical_pct):
        return RiskLevel.CRITICAL.value
    if value >= float(thresholds.drawdown_high_pct):
        return RiskLevel.HIGH.value
    if value >= float(thresholds.drawdown_warning_pct):
        return RiskLevel.ELEVATED.value
    return RiskLevel.NORMAL.value


def level_from_loss(value: float | None, thresholds: RiskThresholds) -> str:
    """Negative return alone is not CRITICAL; this is only a loss-size input."""

    if value is None:
        return RiskLevel.INSUFFICIENT_DATA.value
    magnitude = abs(min(0.0, value)) * 100.0
    if magnitude >= float(thresholds.drawdown_critical_pct):
        return RiskLevel.HIGH.value
    if magnitude >= float(thresholds.drawdown_high_pct):
        return RiskLevel.ELEVATED.value
    return RiskLevel.NORMAL.value


__all__ = [
    "aligned_returns",
    "concentration_metrics",
    "data_freshness",
    "drawdown_episodes",
    "level_from_drawdown",
    "level_from_loss",
    "liquidity_metrics",
    "normalise_bars",
    "pairwise_correlation",
    "returns_by_time",
    "scenario_impact",
    "scenario_positions",
    "volatility_metrics",
]
