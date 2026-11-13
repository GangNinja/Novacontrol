"""NM-08 risk metrics: measurable definitions with explicit data requirements.

Every function returns a :class:`MetricResult`. When the minimum observation
count is not met the result is ``sufficient=False`` with a reason — a number is
never invented from too little data.

Definitions
-----------
- Volatility: population standard deviation of simple period returns, annualised
  by ``sqrt(periods_per_year)`` (252 for daily bars).
- Maximum drawdown: the largest peak-to-trough decline of the close series.
- Current drawdown: the decline from the running peak to the latest close.
- Historical VaR: the loss at the given confidence quantile of observed returns.
- Historical CVaR: the mean loss beyond that quantile.
- Sharpe: mean excess return / standard deviation, annualised.
- Sortino: mean excess return / downside deviation, annualised.
- Beta / correlation: covariance against a benchmark return series.
- Concentration: Herfindahl-Hirschman index over position weights.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from decimal import Decimal

from novacontrol.novamarket.risk.models import HUNDRED, MetricResult

__all__ = [
    "annualized_volatility",
    "beta",
    "capital_utilization",
    "concentration",
    "correlation",
    "current_drawdown",
    "historical_cvar",
    "historical_var",
    "liquidity_value",
    "max_drawdown",
    "returns_from_closes",
    "sharpe",
    "sortino",
    "turnover_ratio",
]


def _decimal(value: float, places: int = 6) -> Decimal:
    return Decimal(str(round(value, places)))


def returns_from_closes(closes: Sequence[float]) -> list[float]:
    """Simple period returns; non-positive prices break the chain and stop it."""
    returns: list[float] = []
    for previous, current in zip(closes, closes[1:]):
        if previous <= 0:
            break
        returns.append((current / previous) - 1.0)
    return returns


def annualized_volatility(
    closes: Sequence[float], *, minimum: int = 20, periods_per_year: int = 252
) -> MetricResult:
    returns = returns_from_closes(closes)
    if len(returns) < minimum:
        return MetricResult.insufficient(
            "annualized_volatility_pct",
            f"{len(returns)} return observation(s); at least {minimum} are required",
            observations=len(returns),
            unit="percent",
        )
    deviation = statistics.pstdev(returns)
    value = deviation * math.sqrt(periods_per_year) * 100.0
    return MetricResult(
        name="annualized_volatility_pct",
        value=_decimal(value),
        unit="percent",
        observations=len(returns),
        sufficient=True,
        assumptions=(
            f"population standard deviation of {len(returns)} simple returns",
            f"annualised with sqrt({periods_per_year})",
        ),
    )


def max_drawdown(closes: Sequence[float], *, minimum: int = 2) -> MetricResult:
    if len(closes) < minimum:
        return MetricResult.insufficient(
            "max_drawdown_pct",
            f"{len(closes)} close(s); at least {minimum} are required",
            observations=len(closes),
            unit="percent",
        )
    peak = closes[0]
    worst = 0.0
    for value in closes:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return MetricResult(
        name="max_drawdown_pct",
        value=_decimal(abs(worst) * 100.0),
        unit="percent",
        observations=len(closes),
        sufficient=True,
        assumptions=("peak-to-trough decline over the supplied close series",),
    )


def current_drawdown(closes: Sequence[float], *, minimum: int = 2) -> MetricResult:
    if len(closes) < minimum:
        return MetricResult.insufficient(
            "current_drawdown_pct",
            f"{len(closes)} close(s); at least {minimum} are required",
            observations=len(closes),
            unit="percent",
        )
    peak = max(closes)
    if peak <= 0:
        return MetricResult.insufficient(
            "current_drawdown_pct", "the peak close is not positive"
        )
    value = max(0.0, (peak - closes[-1]) / peak) * 100.0
    return MetricResult(
        name="current_drawdown_pct",
        value=_decimal(value),
        unit="percent",
        observations=len(closes),
        sufficient=True,
        assumptions=("decline from the highest observed close to the latest close",),
    )


def historical_var(
    returns: Sequence[float], *, confidence: float = 0.95, minimum: int = 20
) -> MetricResult:
    if len(returns) < minimum:
        return MetricResult.insufficient(
            "var_95_pct",
            f"{len(returns)} return observation(s); at least {minimum} are required",
            observations=len(returns),
            unit="percent",
        )
    if not 0.5 < confidence < 1.0:
        return MetricResult.insufficient("var_95_pct", "confidence must be in (0.5, 1)")
    ordered = sorted(returns)
    index = max(0, min(len(ordered) - 1, int(math.floor((1.0 - confidence) * len(ordered)))))
    loss = max(0.0, -ordered[index]) * 100.0
    return MetricResult(
        name="var_95_pct",
        value=_decimal(loss),
        unit="percent",
        observations=len(returns),
        sufficient=True,
        assumptions=(
            "historical simulation; no distributional assumption",
            f"loss at the {confidence:.0%} quantile of observed returns",
        ),
    )


def historical_cvar(
    returns: Sequence[float], *, confidence: float = 0.95, minimum: int = 20
) -> MetricResult:
    if len(returns) < minimum:
        return MetricResult.insufficient(
            "cvar_95_pct",
            f"{len(returns)} return observation(s); at least {minimum} are required",
            observations=len(returns),
            unit="percent",
        )
    if not 0.5 < confidence < 1.0:
        return MetricResult.insufficient("cvar_95_pct", "confidence must be in (0.5, 1)")
    ordered = sorted(returns)
    index = max(0, min(len(ordered) - 1, int(math.floor((1.0 - confidence) * len(ordered)))))
    tail = ordered[: index + 1]
    loss = max(0.0, -statistics.fmean(tail)) * 100.0
    return MetricResult(
        name="cvar_95_pct",
        value=_decimal(loss),
        unit="percent",
        observations=len(returns),
        sufficient=True,
        assumptions=(
            "mean loss beyond the historical VaR threshold",
            f"tail contains {len(tail)} observation(s)",
        ),
    )


def sharpe(
    returns: Sequence[float],
    *,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
    minimum: int = 20,
) -> MetricResult:
    if len(returns) < minimum:
        return MetricResult.insufficient(
            "sharpe_ratio",
            f"{len(returns)} return observation(s); at least {minimum} are required",
            observations=len(returns),
        )
    excess = [value - risk_free for value in returns]
    deviation = statistics.pstdev(excess)
    if deviation == 0:
        return MetricResult.insufficient(
            "sharpe_ratio", "zero return deviation; the ratio is undefined"
        )
    value = statistics.fmean(excess) / deviation * math.sqrt(periods_per_year)
    return MetricResult(
        name="sharpe_ratio",
        value=_decimal(value),
        observations=len(returns),
        sufficient=True,
        assumptions=(
            "risk-free rate is zero (no risk-free series exists in this build)",
            f"annualised with sqrt({periods_per_year})",
        ),
    )


def sortino(
    returns: Sequence[float],
    *,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
    minimum: int = 20,
) -> MetricResult:
    if len(returns) < minimum:
        return MetricResult.insufficient(
            "sortino_ratio",
            f"{len(returns)} return observation(s); at least {minimum} are required",
            observations=len(returns),
        )
    excess = [value - risk_free for value in returns]
    downside = [value for value in excess if value < 0]
    if not downside:
        return MetricResult.insufficient(
            "sortino_ratio", "no downside observations; the ratio is undefined"
        )
    deviation = math.sqrt(sum(value**2 for value in downside) / len(downside))
    if deviation == 0:
        return MetricResult.insufficient(
            "sortino_ratio", "zero downside deviation; the ratio is undefined"
        )
    value = statistics.fmean(excess) / deviation * math.sqrt(periods_per_year)
    return MetricResult(
        name="sortino_ratio",
        value=_decimal(value),
        observations=len(returns),
        sufficient=True,
        assumptions=("downside deviation over negative excess returns",),
    )


def beta(
    asset_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    minimum: int = 20,
) -> MetricResult:
    count = min(len(asset_returns), len(benchmark_returns))
    if count < minimum:
        return MetricResult.insufficient(
            "beta",
            f"{count} paired return(s); at least {minimum} are required",
            observations=count,
        )
    pairs = list(zip(asset_returns[-count:], benchmark_returns[-count:]))
    mean_a = statistics.fmean(pair[0] for pair in pairs)
    mean_b = statistics.fmean(pair[1] for pair in pairs)
    covariance = sum(
        (a - mean_a) * (b - mean_b) for a, b in pairs
    ) / count
    variance = sum((b - mean_b) ** 2 for _a, b in pairs) / count
    if variance == 0:
        return MetricResult.insufficient(
            "beta", "zero benchmark variance; beta is undefined"
        )
    return MetricResult(
        name="beta",
        value=_decimal(covariance / variance),
        observations=count,
        sufficient=True,
        assumptions=("population covariance against the supplied benchmark",),
    )


def correlation(
    left: Sequence[float], right: Sequence[float], *, minimum: int = 20
) -> MetricResult:
    count = min(len(left), len(right))
    if count < minimum:
        return MetricResult.insufficient(
            "correlation",
            f"{count} paired return(s); at least {minimum} are required",
            observations=count,
        )
    pairs = list(zip(left[-count:], right[-count:]))
    mean_a = statistics.fmean(pair[0] for pair in pairs)
    mean_b = statistics.fmean(pair[1] for pair in pairs)
    covariance = sum((a - mean_a) * (b - mean_b) for a, b in pairs) / count
    dev_a = math.sqrt(sum((a - mean_a) ** 2 for a, _b in pairs) / count)
    dev_b = math.sqrt(sum((b - mean_b) ** 2 for _a, b in pairs) / count)
    if dev_a == 0 or dev_b == 0:
        return MetricResult.insufficient(
            "correlation", "zero deviation in one series; correlation is undefined"
        )
    return MetricResult(
        name="correlation",
        value=_decimal(covariance / (dev_a * dev_b)),
        observations=count,
        sufficient=True,
        assumptions=("Pearson correlation of the paired return series",),
    )


def concentration(weights: Sequence[float]) -> MetricResult:
    total = sum(weight for weight in weights if weight > 0)
    if total <= 0:
        return MetricResult.insufficient(
            "concentration_hhi", "no positive weights to measure"
        )
    normalised = [weight / total for weight in weights if weight > 0]
    hhi = sum(weight**2 for weight in normalised)
    return MetricResult(
        name="concentration_hhi",
        value=_decimal(hhi),
        observations=len(normalised),
        sufficient=True,
        assumptions=(
            "Herfindahl-Hirschman index over normalised position weights",
            "1.0 is a single holding; lower is more diversified",
        ),
    )


def capital_utilization(invested: Decimal, total: Decimal) -> MetricResult:
    if total <= 0:
        return MetricResult.insufficient(
            "capital_utilization", "the portfolio has no value to compare against"
        )
    return MetricResult(
        name="capital_utilization",
        value=(invested / total).quantize(Decimal("0.0001")),
        observations=1,
        sufficient=True,
        assumptions=("invested value divided by total portfolio value",),
    )


def turnover_ratio(notional: Decimal, mean_value: Decimal) -> MetricResult:
    if mean_value <= 0:
        return MetricResult.insufficient(
            "turnover", "no mean portfolio value to compare against"
        )
    return MetricResult(
        name="turnover",
        value=(notional / mean_value).quantize(Decimal("0.0001")),
        observations=1,
        sufficient=True,
        assumptions=("traded notional divided by mean portfolio value",),
    )


def liquidity_value(bars: Sequence[object]) -> MetricResult:
    """Average daily traded value from bars that actually carry volume."""
    values: list[float] = []
    for bar in bars:
        volume = getattr(bar, "volume", None)
        close = getattr(bar, "close", None)
        if volume is None or close is None:
            continue
        try:
            values.append(float(volume) * float(close))
        except (TypeError, ValueError):
            continue
    if len(values) < 5:
        return MetricResult.insufficient(
            "average_daily_value",
            f"{len(values)} bar(s) with volume; at least 5 are required",
            observations=len(values),
            unit="currency",
        )
    return MetricResult(
        name="average_daily_value",
        value=_decimal(statistics.fmean(values), 2),
        observations=len(values),
        sufficient=True,
        unit="currency",
        assumptions=("mean of close × volume over the supplied bars",),
    )
