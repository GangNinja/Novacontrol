"""Deterministic forecasting models (NM-06).

Each model turns a price history into a **predictive distribution** of the
``h``-session return, never a single guaranteed number. Two point baselines
(``zero_return``) and five distributional models are implemented; nothing here
is random except the bootstrap, whose seed is derived from the inputs, so the
same history always produces the same forecast.

Models are deliberately simple and transparent: with a few hundred daily bars an
ARIMA/GARCH fit or a tree ensemble would add tuning surface, not demonstrated
skill, and the phase's rule is that a complex model is only claimed better when
validation shows it.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import dataclass
from typing import Sequence

from novacontrol.novamarket.forecasting.models import ForecastPolicy

#: Every model this build knows. ``zero_return`` is a validation baseline only.
MODEL_IDS: tuple[str, ...] = (
    "zero_return",
    "random_walk",
    "historical_mean",
    "drift",
    "ewma",
    "bootstrap",
)
PROBABILISTIC_MODELS: tuple[str, ...] = MODEL_IDS[1:]

MODEL_METADATA: dict[str, tuple[str, str]] = {
    "zero_return": (
        "baseline",
        "Point baseline: the next h-session return is exactly zero.",
    ),
    "random_walk": (
        "baseline",
        "Persistence: zero point forecast with the empirical h-session return "
        "distribution of this instrument.",
    ),
    "historical_mean": (
        "statistical",
        "The mean historical h-session return, with the empirical residual "
        "distribution around it.",
    ),
    "drift": (
        "statistical",
        "Trailing 21-session mean return extrapolated over the horizon, with "
        "the empirical residual distribution.",
    ),
    "ewma": (
        "statistical",
        "Normal predictive distribution: trailing drift and an exponentially "
        "weighted volatility scaled by sqrt(h).",
    ),
    "bootstrap": (
        "statistical",
        "Seeded bootstrap of daily returns summed over the horizon.",
    ),
}


@dataclass(frozen=True, slots=True)
class ForecastOutput:
    """One model's predictive distribution at one origin."""

    model_id: str
    predicted_return: float
    predicted_price: float
    lower_return: float | None
    upper_return: float | None
    p_up: float | None
    p_down: float | None
    p_sideways: float | None
    notes: tuple[str, ...] = ()


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _h_returns(closes: Sequence[float], horizon: int) -> list[float]:
    """Non-overlapping historical h-session returns (never overlapping windows)."""
    values: list[float] = []
    index = horizon
    while index < len(closes):
        previous = closes[index - horizon]
        if previous > 0:
            values.append((closes[index] / previous) - 1.0)
        index += horizon
    return values


def _daily_returns(closes: Sequence[float]) -> list[float]:
    return [
        (current / previous) - 1.0
        for previous, current in zip(closes, closes[1:])
        if previous > 0
    ]


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("a quantile needs at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = _clamp(probability) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _ewma_volatility(returns: Sequence[float], span: int = 21) -> float:
    if len(returns) < 2:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    variance = returns[0] ** 2
    for value in returns[1:]:
        variance = alpha * value**2 + (1.0 - alpha) * variance
    return math.sqrt(max(variance, 0.0))


def _from_samples(
    model_id: str,
    samples: Sequence[float],
    *,
    last_close: float,
    threshold: float,
    policy: ForecastPolicy,
    notes: tuple[str, ...] = (),
    predicted: float | None = None,
) -> ForecastOutput:
    if len(samples) < 3:
        raise ValueError(f"{model_id} needs at least three return samples")
    p_up = sum(1 for value in samples if value > threshold) / len(samples)
    p_down = sum(1 for value in samples if value < -threshold) / len(samples)
    p_sideways = max(0.0, 1.0 - p_up - p_down)
    total = p_up + p_down + p_sideways
    # A model that predicts a mean or a drift says so; only models whose point
    # forecast is the distribution's centre leave this as the median.
    point = _quantile(samples, 0.5) if predicted is None else float(predicted)
    lower = min(_quantile(samples, policy.interval_low), point)
    upper = max(_quantile(samples, policy.interval_high), point)
    return ForecastOutput(
        model_id=model_id,
        predicted_return=point,
        predicted_price=last_close * (1.0 + point),
        lower_return=lower,
        upper_return=upper,
        p_up=p_up / total,
        p_down=p_down / total,
        p_sideways=p_sideways / total,
        notes=notes,
    )


def _from_normal(
    model_id: str,
    *,
    mean: float,
    sigma: float,
    last_close: float,
    threshold: float,
    policy: ForecastPolicy,
    notes: tuple[str, ...] = (),
) -> ForecastOutput:
    distribution = statistics.NormalDist(mu=mean, sigma=max(sigma, 1e-9))
    p_up = 1.0 - distribution.cdf(threshold)
    p_down = distribution.cdf(-threshold)
    p_sideways = max(0.0, 1.0 - p_up - p_down)
    total = p_up + p_down + p_sideways
    return ForecastOutput(
        model_id=model_id,
        predicted_return=mean,
        predicted_price=last_close * (1.0 + mean),
        lower_return=distribution.inv_cdf(policy.interval_low),
        upper_return=distribution.inv_cdf(policy.interval_high),
        p_up=p_up / total,
        p_down=p_down / total,
        p_sideways=p_sideways / total,
        notes=notes,
    )


def _bootstrap_returns(
    returns: Sequence[float], horizon: int, *, paths: int, seed: int
) -> list[float]:
    generator = random.Random(seed)
    population = list(returns)
    samples: list[float] = []
    for _ in range(paths):
        total = 1.0
        for _ in range(horizon):
            total *= 1.0 + population[generator.randrange(len(population))]
        samples.append(total - 1.0)
    return samples


def forecast(
    model_id: str,
    closes: Sequence[float],
    horizon_sessions: int,
    policy: ForecastPolicy,
) -> ForecastOutput:
    """Forecast one horizon from closes ordered oldest-first.

    The caller must pass only closes observed at or before the forecast cutoff;
    this function has no clock and cannot reach later data.
    """
    if horizon_sessions < 1:
        raise ValueError("horizon_sessions must be positive")
    if len(closes) < max(policy.min_history_bars, horizon_sessions + 2):
        raise ValueError("not enough closes for this horizon")
    threshold = policy.directional_threshold_pct / 100.0
    last_close = closes[-1]
    daily = _daily_returns(closes)
    h_returns = _h_returns(closes, horizon_sessions)

    if model_id == "zero_return":
        return ForecastOutput(
            model_id=model_id,
            predicted_return=0.0,
            predicted_price=last_close,
            lower_return=None,
            upper_return=None,
            p_up=None,
            p_down=None,
            p_sideways=None,
            notes=("point baseline: no distribution is claimed",),
        )

    if model_id == "random_walk":
        return _from_samples(
            model_id,
            h_returns,
            last_close=last_close,
            threshold=threshold,
            policy=policy,
            notes=("persistence: zero drift, empirical h-session returns",),
        )

    if model_id == "historical_mean":
        if len(h_returns) < 3:
            raise ValueError("historical_mean needs three h-session returns")
        mean = statistics.fmean(h_returns)
        shifted = [mean + (value - mean) for value in h_returns]
        return _from_samples(
            model_id,
            shifted,
            last_close=last_close,
            threshold=threshold,
            policy=policy,
            notes=(f"historical mean {mean:.4%} over {len(h_returns)} windows",),
            predicted=mean,
        )

    if model_id == "drift":
        window = daily[-21:]
        if len(window) < 5 or len(h_returns) < 3:
            raise ValueError("drift needs 21 daily and three h-session returns")
        drift = statistics.fmean(window) * horizon_sessions
        residual_mean = statistics.fmean(h_returns)
        shifted = [drift + (value - residual_mean) for value in h_returns]
        return _from_samples(
            model_id,
            shifted,
            last_close=last_close,
            threshold=threshold,
            policy=policy,
            notes=(f"21-session drift {drift:.4%} extrapolated",),
            predicted=drift,
        )

    if model_id == "ewma":
        window = daily[-21:]
        if len(window) < 5:
            raise ValueError("ewma needs 21 daily returns")
        mean = statistics.fmean(window) * horizon_sessions
        sigma = _ewma_volatility(window) * math.sqrt(horizon_sessions)
        return _from_normal(
            model_id,
            mean=mean,
            sigma=sigma,
            last_close=last_close,
            threshold=threshold,
            policy=policy,
            notes=(f"EWMA volatility {sigma:.4%} over {horizon_sessions} sessions",),
        )

    if model_id == "bootstrap":
        if len(daily) < 20:
            raise ValueError("bootstrap needs twenty daily returns")
        seed = int.from_bytes(
            hashlib.sha256(
                (",".join(f"{value:.8f}" for value in closes[-80:])).encode("utf-8")
            ).digest()[:8],
            "big",
        )
        samples = _bootstrap_returns(
            daily[-policy.max_bootstrap_bars :],
            horizon_sessions,
            paths=policy.bootstrap_paths,
            seed=seed,
        )
        return _from_samples(
            model_id,
            samples,
            last_close=last_close,
            threshold=threshold,
            policy=policy,
            notes=(
                f"seeded bootstrap: {policy.bootstrap_paths} paths of "
                f"{horizon_sessions} sessions",
            ),
        )

    raise ValueError(f"unknown forecast model {model_id!r}")


__all__ = [
    "MODEL_IDS",
    "MODEL_METADATA",
    "PROBABILISTIC_MODELS",
    "ForecastOutput",
    "forecast",
]
