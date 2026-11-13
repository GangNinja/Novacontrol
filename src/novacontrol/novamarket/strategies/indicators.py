"""Deterministic technical indicators used by NM-09 strategies.

Every function returns ``None`` when its minimum data is not present. No metric
is invented from a short series, and no indicator uses data after the supplied
cutoff.
"""

from __future__ import annotations

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


__all__ = [
    "atr",
    "average_volume",
    "bollinger",
    "ema",
    "highest",
    "lowest",
    "q",
    "returns",
    "rsi",
    "sma",
    "sma_series",
    "true_range",
]
