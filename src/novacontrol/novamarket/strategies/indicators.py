"""Deterministic technical indicators used by NM-09 strategies.

Every function returns ``None`` when its minimum data is not present. No metric
is invented from a short series, and no indicator uses data after the supplied
cutoff.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_UP


def q(value: Decimal, places: str = "0.0001") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def sma(values: Sequence[float], period: int) -> Decimal | None:
    if period < 1 or len(values) < period:
        return None
    window = values[-period:]
    return q(Decimal(str(sum(window) / period)))


def sma_series(values: Sequence[float], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            out.append(None)
        else:
            window = values[index + 1 - period : index + 1]
            out.append(q(Decimal(str(sum(window) / period))))
    return out


def ema(values: Sequence[float], period: int) -> Decimal | None:
    if period < 1 or len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return q(Decimal(str(current)))


def returns(values: Sequence[float]) -> list[float]:
    return [
        (current / previous) - 1.0
        for previous, current in zip(values, values[1:])
        if previous > 0
    ]


def rsi(values: Sequence[float], period: int = 14) -> Decimal | None:
    if len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[-period - 1 :], values[-period:]):
        change = current - previous
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss == 0:
        return q(Decimal("100"))
    strength = average_gain / average_loss
    return q(Decimal(str(100.0 - (100.0 / (1.0 + strength)))))


def bollinger(
    values: Sequence[float], period: int = 20, k: float = 2.0
) -> tuple[Decimal, Decimal, Decimal] | None:
    if period < 2 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((value - mean) ** 2 for value in window) / period
    deviation = variance**0.5
    return (
        q(Decimal(str(mean))),
        q(Decimal(str(mean + k * deviation))),
        q(Decimal(str(mean - k * deviation))),
    )


def average_volume(volumes: Sequence[float], period: int = 20) -> Decimal | None:
    if period < 1 or len(volumes) < period:
        return None
    return q(Decimal(str(sum(volumes[-period:]) / period)), "0.01")


def highest(values: Sequence[float], period: int) -> Decimal | None:
    if period < 1 or len(values) < period:
        return None
    return q(Decimal(str(max(values[-period:]))))


def lowest(values: Sequence[float], period: int) -> Decimal | None:
    if period < 1 or len(values) < period:
        return None
    return q(Decimal(str(min(values[-period:]))))


def true_range(high: float, low: float, previous_close: float) -> float:
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Decimal | None:
    if min(len(highs), len(lows), len(closes)) < period + 1:
        return None
    ranges = [
        true_range(highs[index], lows[index], closes[index - 1])
        for index in range(len(closes) - period, len(closes))
    ]
    return q(Decimal(str(sum(ranges) / period)))


# ---------------------------------------------------------------------------
# PI-04 additions.  These live here, in the ONE technical-indicator module,
# rather than in a parallel feature-engine copy.  Existing NM-09 indicators
# keep their published definitions; the Wilder variants below are explicit
# where the feature contract requires Wilder smoothing.
# ---------------------------------------------------------------------------


def wma(values: Sequence[float], period: int) -> Decimal | None:
    """Linearly weighted moving average (oldest weight 1 ... newest weight N)."""
    if period < 1 or len(values) < period:
        return None
    window = values[-period:]
    denominator = period * (period + 1) / 2.0
    weighted = sum((index + 1) * value for index, value in enumerate(window))
    return q(Decimal(str(weighted / denominator)))


def ema_series(values: Sequence[float], period: int) -> list[Decimal | None]:
    """EMA seeded with the first SMA, one value per input position."""
    out: list[Decimal | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    multiplier = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    out[period - 1] = q(Decimal(str(current)))
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        out[index] = q(Decimal(str(current)))
    return out


def rsi_wilder(values: Sequence[float], period: int = 14) -> Decimal | None:
    """Wilder's RSI (the definition used by the PI-04 feature contract).

    A completely flat series has no gains and no losses; it is reported as 50
    rather than the undefined 0/0 ratio.  ``rsi`` above remains the simpler
    NM-09 definition so its existing strategy behaviour does not change.
    """
    if period < 1 or len(values) < period + 1:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes[:period]]
    losses = [max(-change, 0.0) for change in changes[:period]]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    for change in changes[period:]:
        average_gain = ((period - 1) * average_gain + max(change, 0.0)) / period
        average_loss = ((period - 1) * average_loss + max(-change, 0.0)) / period
    if average_gain == 0 and average_loss == 0:
        return q(Decimal("50"))
    if average_loss == 0:
        return q(Decimal("100"))
    strength = average_gain / average_loss
    return q(Decimal(str(100.0 - (100.0 / (1.0 + strength)))))


def atr_wilder(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Decimal | None:
    """Wilder-smoothed ATR (``atr`` above is the simple-mean variant)."""
    if period < 1 or min(len(highs), len(lows), len(closes)) < period + 1:
        return None
    ranges = [
        true_range(highs[index], lows[index], closes[index - 1])
        for index in range(1, len(closes))
    ]
    current = sum(ranges[:period]) / period
    for value in ranges[period:]:
        current = ((period - 1) * current + value) / period
    return q(Decimal(str(current)))


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Decimal, Decimal, Decimal] | None:
    """MACD line, signal EMA and histogram using EMA seeds at each warm-up."""
    if fast < 1 or slow <= fast or signal < 1 or len(values) < slow + signal - 1:
        return None
    fast_values = ema_series(values, fast)
    slow_values = ema_series(values, slow)
    line: list[float] = []
    for fast_value, slow_value in zip(fast_values[slow - 1 :], slow_values[slow - 1 :]):
        if fast_value is None or slow_value is None:
            return None
        line.append(float(fast_value) - float(slow_value))
    signal_values = ema_series(line, signal)
    signal_value = signal_values[-1] if signal_values else None
    if signal_value is None:
        return None
    macd_value = q(Decimal(str(line[-1])))
    return macd_value, signal_value, q(macd_value - signal_value)


def directional_movement(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    """ADX and +DI/-DI using Wilder smoothing; returned as (adx, +DI, -DI, DX).

    The returned DX is the last directional-index value, useful for a regime
    transition diagnostic; it is not another ADX.
    """
    if period < 1 or min(len(highs), len(lows), len(closes)) < period * 2 + 1:
        return None
    true_ranges: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, len(closes)):
        true_ranges.append(true_range(highs[index], lows[index], closes[index - 1]))
        up = highs[index] - highs[index - 1]
        down = lows[index - 1] - lows[index]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    # Seed the Wilder averages once, then update them in a single forward pass.
    average_tr = sum(true_ranges[:period]) / period
    average_plus = sum(plus_dm[:period]) / period
    average_minus = sum(minus_dm[:period]) / period
    dx_values: list[float] = []
    last_plus = last_minus = last_dx = 0.0
    for index in range(period - 1, len(true_ranges)):
        if index >= period:
            average_tr = ((period - 1) * average_tr + true_ranges[index]) / period
            average_plus = ((period - 1) * average_plus + plus_dm[index]) / period
            average_minus = ((period - 1) * average_minus + minus_dm[index]) / period
        if average_tr <= 0:
            continue
        plus_di = 100.0 * average_plus / average_tr
        minus_di = 100.0 * average_minus / average_tr
        denominator = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denominator if denominator > 0 else 0.0
        dx_values.append(dx)
        last_plus, last_minus, last_dx = plus_di, minus_di, dx
    if len(dx_values) < period:
        return None
    adx_value = sum(dx_values[:period]) / period
    for value in dx_values[period:]:
        adx_value = ((period - 1) * adx_value + value) / period
    return (
        q(Decimal(str(adx_value))),
        q(Decimal(str(last_plus))),
        q(Decimal(str(last_minus))),
        q(Decimal(str(last_dx))),
    )


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
    smoothing: int = 3,
) -> tuple[Decimal, Decimal] | None:
    """Stochastic %K and %D on completed bars."""
    if period < 1 or smoothing < 1 or min(len(highs), len(lows), len(closes)) < period + smoothing - 1:
        return None
    k_values: list[float] = []
    for index in range(period - 1, len(closes)):
        highest_high = max(highs[index - period + 1 : index + 1])
        lowest_low = min(lows[index - period + 1 : index + 1])
        if highest_high == lowest_low:
            k_values.append(50.0)
        else:
            k_values.append(100.0 * (closes[index] - lowest_low) / (highest_high - lowest_low))
    if len(k_values) < smoothing:
        return None
    d_value = sum(k_values[-smoothing:]) / smoothing
    return q(Decimal(str(k_values[-1]))), q(Decimal(str(d_value)))


def roc(values: Sequence[float], period: int = 10) -> Decimal | None:
    if period < 1 or len(values) < period + 1 or values[-period - 1] <= 0:
        return None
    return q(Decimal(str((values[-1] / values[-period - 1] - 1.0) * 100.0)))


def momentum(values: Sequence[float], period: int = 10) -> Decimal | None:
    if period < 1 or len(values) < period + 1:
        return None
    return q(Decimal(str(values[-1] - values[-period - 1])))


def williams_r(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Decimal | None:
    if period < 1 or min(len(highs), len(lows), len(closes)) < period:
        return None
    highest_high = max(highs[-period:])
    lowest_low = min(lows[-period:])
    if highest_high == lowest_low:
        return q(Decimal("-50"))
    return q(Decimal(str(-100.0 * (highest_high - closes[-1]) / (highest_high - lowest_low))))


def rolling_std(values: Sequence[float], period: int = 20) -> Decimal | None:
    if period < 2 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((value - mean) ** 2 for value in window) / period
    return q(Decimal(str(variance**0.5)))


def historical_volatility(
    values: Sequence[float], period: int = 20, annualization: float = 252.0
) -> Decimal | None:
    """Annualized sample standard deviation of log returns."""
    if period < 2 or len(values) < period + 1:
        return None
    window = values[-period - 1 :]
    returns = [
        math.log(current / previous)
        for previous, current in zip(window, window[1:])
        if previous > 0 and current > 0
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return q(Decimal(str((variance**0.5) * (annualization**0.5))))


def obv_series(closes: Sequence[float], volumes: Sequence[float]) -> list[float] | None:
    if len(closes) != len(volumes) or not closes:
        return None
    result = [0.0]
    for index in range(1, len(closes)):
        if closes[index] > closes[index - 1]:
            result.append(result[-1] + volumes[index])
        elif closes[index] < closes[index - 1]:
            result.append(result[-1] - volumes[index])
        else:
            result.append(result[-1])
    return result


def obv(closes: Sequence[float], volumes: Sequence[float]) -> Decimal | None:
    values = obv_series(closes, volumes)
    return q(Decimal(str(values[-1]))) if values else None


def vpt_series(closes: Sequence[float], volumes: Sequence[float]) -> list[float] | None:
    if len(closes) != len(volumes) or len(closes) < 2:
        return None
    result = [0.0]
    for index in range(1, len(closes)):
        if closes[index - 1] <= 0:
            return None
        result.append(result[-1] + volumes[index] * ((closes[index] / closes[index - 1]) - 1.0))
    return result


def vpt(closes: Sequence[float], volumes: Sequence[float]) -> Decimal | None:
    values = vpt_series(closes, volumes)
    return q(Decimal(str(values[-1]))) if values else None


def money_flow_index(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    period: int = 14,
) -> Decimal | None:
    if period < 1 or min(len(highs), len(lows), len(closes), len(volumes)) < period + 1:
        return None
    typical = [(high + low + close) / 3.0 for high, low, close in zip(highs, lows, closes)]
    positive = 0.0
    negative = 0.0
    for index in range(len(typical) - period, len(typical)):
        flow = typical[index] * volumes[index]
        if typical[index] > typical[index - 1]:
            positive += flow
        elif typical[index] < typical[index - 1]:
            negative += flow
    if positive + negative == 0:
        return q(Decimal("50"))
    if negative == 0:
        return q(Decimal("100"))
    ratio = positive / negative
    return q(Decimal(str(100.0 - (100.0 / (1.0 + ratio)))))


def vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> Decimal | None:
    """Volume-weighted average of typical price over the supplied session range."""
    if not closes or len(highs) != len(closes) or len(lows) != len(closes) or len(volumes) != len(closes):
        return None
    total_volume = sum(volumes)
    if total_volume <= 0:
        return None
    total = sum(
        ((high + low + close) / 3.0) * volume
        for high, low, close, volume in zip(highs, lows, closes, volumes)
    )
    return q(Decimal(str(total / total_volume)))


__all__ = [
    "atr",
    "average_volume",
    "atr_wilder",
    "bollinger",
    "directional_movement",
    "ema",
    "ema_series",
    "highest",
    "historical_volatility",
    "lowest",
    "macd",
    "money_flow_index",
    "momentum",
    "obv",
    "obv_series",
    "q",
    "returns",
    "roc",
    "rolling_std",
    "rsi",
    "rsi_wilder",
    "sma",
    "sma_series",
    "stochastic",
    "true_range",
    "vpt",
    "vpt_series",
    "vwap",
    "williams_r",
    "wma",
]
