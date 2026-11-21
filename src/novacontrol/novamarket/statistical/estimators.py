"""PI-05 baseline and statistical estimators.

Each function turns a bounded, oldest-first close history into a
:class:`ModelForecast`.  The models are deliberately explicit about what they
forecast: a moving average is used as a *level forecast* here, not merely as a
technical indicator; a seasonal model refuses a series without enough seasonal
cycles; ARIMA records its stationarity decision and residual diagnostics.

No estimator reads a clock or a later bar.  The caller supplies only observations
made at or before the forecast cutoff.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.statistical.arima import (
    fit_arima,
    forecast_arima,
    ljung_box,
)
from novacontrol.novamarket.statistical.models import (
    MODEL_IDS,
    MODEL_METADATA,
    STATISTICAL_MODEL_VERSION,
    FitStatus,
    IntervalMethod,
    ModelConfig,
    ModelFamily,
    ModelFit,
    ModelFitError,
    ModelForecast,
    ModelUnavailableError,
    StatisticalValidationError,
    classify_direction,
    model_family,
    normalise_model_id,
    price_from_return,
)

MINIMUM_BY_MODEL: dict[str, int] = {
    model_id: int(metadata["min_observations"])
    for model_id, metadata in MODEL_METADATA.items()
}


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(item) for item in values if math.isfinite(float(item)))
    if not ordered:
        raise ModelUnavailableError("a quantile needs at least one finite observation")
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def _returns(closes: Sequence[float]) -> list[float]:
    return [
        current / previous - 1.0
        for previous, current in zip(closes, closes[1:])
        if previous > 0 and math.isfinite(current)
    ]


def _h_returns(closes: Sequence[float], horizon: int) -> list[float]:
    values: list[float] = []
    index = horizon
    while index < len(closes):
        previous = closes[index - horizon]
        if previous > 0:
            values.append(closes[index] / previous - 1.0)
        index += horizon
    return values


def _safe_interval(
    point: float,
    residuals: Sequence[float],
    *,
    level: float,
    steps: int,
    last_price: float,
) -> tuple[float | None, float | None, dict[str, Any]]:
    """Empirical residual interval; refuse rather than inventing a narrow one."""
    clean = [float(item) for item in residuals if math.isfinite(float(item))]
    if len(clean) < 5:
        return None, None, {"reason": "fewer than five finite residuals"}
    scaled = [item * math.sqrt(max(steps, 1)) for item in clean]
    tail = (1.0 - level) / 2.0
    lower = point + _quantile(scaled, tail)
    upper = point + _quantile(scaled, 1.0 - tail)
    lower = min(lower, point)
    upper = max(upper, point)
    if lower <= -1.0:
        lower = -0.999999
    if last_price * (1.0 + lower) <= 0:
        lower = -0.999999
    return lower, upper, {
        "method": "empirical_residual_quantiles",
        "residual_samples": len(clean),
        "horizon_scaling": "sqrt(steps)",
        "interval_level": level,
    }


def _fit_record(
    model_id: str,
    config: ModelConfig,
    bars: Sequence[Any],
    *,
    parameters: dict[str, Any],
    residuals: Sequence[float] = (),
    stationarity: dict[str, Any] | None = None,
    notes: tuple[str, ...] = (),
) -> ModelFit:
    if not bars:
        raise ModelUnavailableError("a model fit needs at least one bar")
    clean_residuals = [float(item) for item in residuals if math.isfinite(float(item))]
    diagnostics = ljung_box(clean_residuals)
    parameters = dict(parameters)
    parameters.setdefault("residual_samples", len(clean_residuals))
    return ModelFit(
        model_id=model_id,
        version=STATISTICAL_MODEL_VERSION,
        family=model_family(model_id),
        config=config,
        status=FitStatus.FITTED,
        training_start=bars[0].observed_at,
        training_end=bars[-1].observed_at,
        observations=len(bars),
        parameters=parameters,
        residual_mean=(
            round(_mean(clean_residuals), 12) if clean_residuals else None
        ),
        residual_std=(
            round(_stdev(clean_residuals), 12) if clean_residuals else None
        ),
        residual_autocorrelation=diagnostics.get(
            "residual_autocorrelation_lag1"
        ),
        ljung_box_p=diagnostics.get("p_value"),
        stationarity=dict(stationarity or {}),
        reason="",
        notes=notes,
    )


def _finalise(
    model_id: str,
    config: ModelConfig,
    fit: ModelFit,
    *,
    predicted_price: float,
    last_price: float,
    lower_return: float | None,
    upper_return: float | None,
    interval_method: IntervalMethod,
    notes: tuple[str, ...],
    diagnostics: dict[str, Any],
) -> ModelForecast:
    if predicted_price <= 0 or not math.isfinite(predicted_price):
        raise ModelFitError("a model produced a non-positive forecast price")
    expected_return = predicted_price / last_price - 1.0
    supported = lower_return is not None and upper_return is not None
    if supported:
        assert lower_return is not None and upper_return is not None
        lower_return = min(float(lower_return), expected_return)
        upper_return = max(float(upper_return), expected_return)
        if last_price * (1.0 + lower_return) <= 0:
            lower_return = -0.999999
        if last_price * (1.0 + upper_return) <= 0:
            raise ModelFitError("a model produced a non-positive upper price bound")
    direction, rule = classify_direction(
        expected_return,
        lower_return if lower_return is not None else expected_return,
        upper_return if upper_return is not None else expected_return,
        neutral_band_pct=config.neutral_band_pct,
        interval_supported=supported,
    )
    return ModelForecast(
        model_id=model_id,
        family=model_family(model_id),
        config=config,
        fit=fit,
        predicted_price=predicted_price,
        predicted_return=expected_return,
        lower_return=lower_return,
        upper_return=upper_return,
        lower_price=(
            last_price * (1.0 + lower_return) if lower_return is not None else None
        ),
        upper_price=(
            last_price * (1.0 + upper_return) if upper_return is not None else None
        ),
        interval_level=config.interval_level,
        interval_method=interval_method if supported else IntervalMethod.NONE,
        interval_supported=supported,
        direction=direction,
        direction_rule=rule,
        notes=tuple(notes),
        diagnostics=dict(diagnostics),
    )


def _rolling_errors(
    closes: Sequence[float],
    *,
    window: int,
    kind: str,
) -> list[float]:
    """One-step return errors of a rolling level forecast."""
    errors: list[float] = []
    for index in range(window, len(closes)):
        history = closes[index - window : index]
        if kind == "sma":
            level = _mean(history)
        elif kind == "wma":
            weights = list(range(1, window + 1))
            level = sum(value * weight for value, weight in zip(history, weights)) / sum(
                weights
            )
        else:
            alpha = 2.0 / (window + 1.0)
            level = history[0]
            for value in history[1:]:
                level = alpha * value + (1.0 - alpha) * level
        previous = closes[index - 1]
        if previous > 0:
            actual = closes[index] / previous - 1.0
            predicted = level / previous - 1.0
            errors.append(actual - predicted)
    return errors


def _last_value(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    last = closes[-1]
    fit = _fit_record(
        "naive_last",
        config,
        bars,
        parameters={"method": "last_completed_close", "last_close": last},
        notes=("point baseline: no predictive interval is claimed",),
    )
    return _finalise(
        "naive_last",
        config,
        fit,
        predicted_price=last,
        last_price=last,
        lower_return=None,
        upper_return=None,
        interval_method=IntervalMethod.NONE,
        notes=("last completed close; this is a reference forecast, not an edge",),
        diagnostics={"interval_supported": False},
    )


def _random_walk(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    last = closes[-1]
    samples = _h_returns(closes, steps)
    if len(samples) < 3:
        raise ModelUnavailableError(
            f"random_walk needs at least three non-overlapping {steps}-bar returns"
        )
    lower, upper, diagnostics = _safe_interval(
        0.0,
        samples,
        level=config.interval_level,
        steps=1,
        last_price=last,
    )
    fit = _fit_record(
        "random_walk",
        config,
        bars,
        parameters={"method": "zero_drift", "return_samples": len(samples)},
        residuals=samples,
        notes=("persistence: zero point forecast with empirical horizon returns",),
    )
    return _finalise(
        "random_walk",
        config,
        fit,
        predicted_price=last,
        last_price=last,
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=("random-walk baseline; the distribution is historical, not a promise",),
        diagnostics=diagnostics,
    )


def _historical_return(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    last = closes[-1]
    samples = _h_returns(closes, steps)
    if len(samples) < 3:
        raise ModelUnavailableError(
            f"historical_return needs three non-overlapping {steps}-bar returns"
        )
    point = _mean(samples)
    lower, upper, diagnostics = _safe_interval(
        point,
        [value - point for value in samples],
        level=config.interval_level,
        steps=1,
        last_price=last,
    )
    fit = _fit_record(
        "historical_return",
        config,
        bars,
        parameters={
            "method": "historical_horizon_mean",
            "samples": len(samples),
            "mean_return": point,
        },
        residuals=samples,
    )
    return _finalise(
        "historical_return",
        config,
        fit,
        predicted_price=price_from_return(last, point),
        last_price=last,
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=(f"historical {steps}-bar mean return {point:.4%}",),
        diagnostics=diagnostics,
    )


def _seasonal_naive(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    season = config.seasonal_period
    if len(closes) < max(season * 2, config.min_observations):
        raise ModelUnavailableError(
            f"seasonal_naive needs at least two {season}-bar cycles"
        )
    last = closes[-1]
    phase = (steps - 1) % season
    analog_index = len(closes) - season + phase
    if analog_index < 0:
        raise ModelUnavailableError("no equivalent seasonal observation is available")
    predicted = closes[analog_index]
    point = predicted / last - 1.0
    errors: list[float] = []
    for index in range(season, len(closes)):
        phase_index = index - season
        previous = closes[index - 1]
        if previous > 0:
            predicted_return = closes[phase_index] / previous - 1.0
            actual_return = closes[index] / previous - 1.0
            errors.append(actual_return - predicted_return)
    lower, upper, diagnostics = _safe_interval(
        point, errors, level=config.interval_level, steps=steps, last_price=last
    )
    fit = _fit_record(
        "seasonal_naive",
        config,
        bars,
        parameters={
            "method": "equivalent_seasonal_phase",
            "seasonal_period": season,
            "analog_index": analog_index,
        },
        residuals=errors,
    )
    return _finalise(
        "seasonal_naive",
        config,
        fit,
        predicted_price=predicted,
        last_price=last,
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=(f"seasonal phase {phase} from a {season}-bar cycle",),
        diagnostics=diagnostics,
    )


def _moving_average(
    closes: Sequence[float],
    bars: Sequence[Any],
    config: ModelConfig,
    steps: int,
    model_id: str,
) -> ModelForecast:
    window = config.window
    if len(closes) < max(window + 2, config.min_observations):
        raise ModelUnavailableError(
            f"{model_id} needs at least {max(window + 2, config.min_observations)} closes"
        )
    last = closes[-1]
    history = closes[-window:]
    if model_id == "sma":
        level = _mean(history)
        parameters = {"method": "sma_level_forecast", "window": window}
    elif model_id == "wma":
        weights = list(range(1, window + 1))
        level = sum(value * weight for value, weight in zip(history, weights)) / sum(
            weights
        )
        parameters = {"method": "wma_level_forecast", "window": window}
    else:
        alpha = 2.0 / (window + 1.0)
        level = history[0]
        for value in history[1:]:
            level = alpha * value + (1.0 - alpha) * level
        parameters = {
            "method": "ema_level_forecast",
            "window": window,
            "alpha": alpha,
        }
    point = level / last - 1.0
    errors = _rolling_errors(closes, window=window, kind=model_id)
    lower, upper, diagnostics = _safe_interval(
        point, errors, level=config.interval_level, steps=steps, last_price=last
    )
    fit = _fit_record(model_id, config, bars, parameters=parameters, residuals=errors)
    return _finalise(
        model_id,
        config,
        fit,
        predicted_price=level,
        last_price=last,
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=(
            f"{model_id} forecasts the future price as its recent level, "
            "not as a guaranteed continuation of the average",
        ),
        diagnostics=diagnostics,
    )


def _ses(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    alpha = config.alpha
    level = closes[0]
    errors: list[float] = []
    for index in range(1, len(closes)):
        previous_level = level
        level = alpha * closes[index] + (1.0 - alpha) * previous_level
        if closes[index - 1] > 0:
            actual = closes[index] / closes[index - 1] - 1.0
            predicted = previous_level / closes[index - 1] - 1.0
            errors.append(actual - predicted)
    point = level / closes[-1] - 1.0
    lower, upper, diagnostics = _safe_interval(
        point, errors, level=config.interval_level, steps=steps, last_price=closes[-1]
    )
    fit = _fit_record(
        "ses",
        config,
        bars,
        parameters={"method": "simple_exponential_smoothing", "alpha": alpha},
        residuals=errors,
    )
    return _finalise(
        "ses",
        config,
        fit,
        predicted_price=level,
        last_price=closes[-1],
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=("no trend or seasonality is assumed by SES",),
        diagnostics=diagnostics,
    )


def _holt(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    if len(closes) < max(10, config.min_observations):
        raise ModelUnavailableError("holt needs more observations")
    alpha, beta = config.alpha, config.beta
    level = closes[0]
    trend = closes[1] - closes[0]
    errors: list[float] = []
    for index in range(1, len(closes)):
        previous_level = level
        previous_trend = trend
        level = alpha * closes[index] + (1.0 - alpha) * (previous_level + previous_trend)
        trend = beta * (level - previous_level) + (1.0 - beta) * previous_trend
        if closes[index - 1] > 0:
            actual = closes[index] / closes[index - 1] - 1.0
            predicted_price = previous_level + previous_trend
            predicted = predicted_price / closes[index - 1] - 1.0
            errors.append(actual - predicted)
    damping = 1.0
    if config.damped_trend:
        damping = 0.9
        effective_steps = sum(damping**index for index in range(1, steps + 1))
    else:
        effective_steps = float(steps)
    predicted = level + trend * effective_steps
    if predicted <= 0:
        raise ModelFitError("Holt's trend extrapolation crossed below zero")
    point = predicted / closes[-1] - 1.0
    lower, upper, diagnostics = _safe_interval(
        point,
        errors,
        level=config.interval_level,
        steps=steps,
        last_price=closes[-1],
    )
    fit = _fit_record(
        "holt",
        config,
        bars,
        parameters={
            "method": "holt_linear_trend",
            "alpha": alpha,
            "beta": beta,
            "level": level,
            "trend": trend,
            "damped_trend": config.damped_trend,
        },
        residuals=errors,
    )
    return _finalise(
        "holt",
        config,
        fit,
        predicted_price=predicted,
        last_price=closes[-1],
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=("Holt linear trend; no seasonal component",),
        diagnostics=diagnostics,
    )


def _holt_winters(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    season = config.seasonal_period
    if len(closes) < max(season * 2, config.min_observations):
        raise ModelUnavailableError(
            f"holt_winters needs at least two complete {season}-bar cycles"
        )
    alpha, beta, gamma = config.alpha, config.beta, config.gamma
    level = _mean(closes[:season])
    trend = _mean(
        [closes[season + index] - closes[index] for index in range(season)]
    ) / max(season, 1)
    seasonals = [
        closes[index] - level for index in range(season)
    ]
    errors: list[float] = []
    for index in range(season, len(closes)):
        seasonal = seasonals[index % season]
        previous_level, previous_trend = level, trend
        predicted_price = previous_level + previous_trend + seasonal
        level = alpha * (closes[index] - seasonal) + (1.0 - alpha) * (
            previous_level + previous_trend
        )
        trend = beta * (level - previous_level) + (1.0 - beta) * previous_trend
        seasonals[index % season] = gamma * (closes[index] - level) + (1.0 - gamma) * seasonal
        if closes[index - 1] > 0:
            actual = closes[index] / closes[index - 1] - 1.0
            predicted = predicted_price / closes[index - 1] - 1.0
            errors.append(actual - predicted)
    amplitude = max(seasonals) - min(seasonals)
    noise = _stdev(errors) * closes[-1]
    diagnostics_seasonality = {
        "seasonal_amplitude": amplitude,
        "residual_noise_price": noise,
        "seasonal_strength_ratio": (
            None if noise <= 0 else round(amplitude / noise, 8)
        ),
        "seasonal_period": season,
    }
    if (
        not config.allow_low_seasonality
        and noise > 0
        and amplitude <= noise * 1.05
    ):
        raise ModelUnavailableError(
            "holt_winters refused: the seasonal amplitude is not distinguishable "
            "from residual noise"
        )
    prediction = level + trend * steps + seasonals[(len(closes) + steps - 1) % season]
    if prediction <= 0:
        raise ModelFitError("Holt-Winters forecast crossed below zero")
    point = prediction / closes[-1] - 1.0
    lower, upper, diagnostics = _safe_interval(
        point,
        errors,
        level=config.interval_level,
        steps=steps,
        last_price=closes[-1],
    )
    fit = _fit_record(
        "holt_winters",
        config,
        bars,
        parameters={
            "method": "holt_winters_additive",
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma,
            "seasonal_period": season,
            "level": level,
            "trend": trend,
            "seasonals": [round(item, 10) for item in seasonals],
        },
        residuals=errors,
    )
    diagnostics.update(diagnostics_seasonality)
    return _finalise(
        "holt_winters",
        config,
        fit,
        predicted_price=prediction,
        last_price=closes[-1],
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.EMPIRICAL,
        notes=(
            "additive Holt-Winters fitted only because the seasonal amplitude "
            "was checked against residual noise",
        ),
        diagnostics=diagnostics,
    )


def _arima(
    closes: Sequence[float], bars: Sequence[Any], config: ModelConfig, steps: int
) -> ModelForecast:
    result, effective = fit_arima(closes, config)
    point, lower, upper, diagnostics = forecast_arima(
        result,
        steps,
        last_price=closes[-1],
        interval_level=config.interval_level,
    )
    predicted_price = closes[-1] * (1.0 + point)
    fit = _fit_record(
        "arima",
        effective,
        bars,
        parameters={
            "method": "conditional_least_squares_arima",
            "order": list(result.order),
            "coefficients": list(result.coefficients),
            "differencing": result.differencing,
            "stationarity": result.stationarity,
            "residual_diagnostics": result.residual_diagnostics,
        },
        residuals=result.residuals,
        stationarity=result.stationarity,
        notes=(
            "bounded ARIMA implementation; no SARIMA or GARCH extension",
            "statsmodels is not installed in this project; the estimator is "
            "the documented in-repo conditional-least-squares implementation",
        ),
    )
    fit = replace(fit, config=effective)
    return _finalise(
        "arima",
        effective,
        fit,
        predicted_price=predicted_price,
        last_price=closes[-1],
        lower_return=lower,
        upper_return=upper,
        interval_method=IntervalMethod.NORMAL_RESIDUAL,
        notes=(
            "ARIMA residual interval; parameter uncertainty is not included",
        ),
        diagnostics=diagnostics,
    )


def forecast_model(
    model_id: str,
    closes: Sequence[float],
    steps: int,
    config: ModelConfig | Mapping[str, Any] | None = None,
    *,
    bars: Sequence[Any] | None = None,
) -> ModelForecast:
    """Fit one model to ``closes`` and forecast ``steps`` bars ahead."""
    model = normalise_model_id(model_id)
    if config is None:
        resolved = ModelConfig()
    elif isinstance(config, ModelConfig):
        resolved = config
    elif isinstance(config, Mapping):
        resolved = ModelConfig.from_dict(config)
    else:
        raise StatisticalValidationError(
            "model config must be a ModelConfig or a configuration object"
        )
    config = resolved
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise StatisticalValidationError("forecast steps must be a positive integer")
    values = [float(item) for item in closes]
    if not all(math.isfinite(item) and item > 0 for item in values):
        raise StatisticalValidationError("closes must be finite and positive")
    # The last-value baseline is valid with a single observed close; a
    # general smoothing guard must not make the simplest baseline unusable.
    minimum = (
        MINIMUM_BY_MODEL[model]
        if model == "naive_last"
        else max(config.min_observations, MINIMUM_BY_MODEL[model])
    )
    if len(values) < minimum:
        raise ModelUnavailableError(
            f"{model} needs at least {minimum} closes; got {len(values)}"
        )
    if bars is None:
        raise StatisticalValidationError(
            "forecast_model needs the observed bar timestamps for a reproducible fit"
        )
    if len(bars) != len(values):
        raise StatisticalValidationError("bars and closes must have the same length")
    if model == "naive_last":
        return _last_value(values, bars, config, steps)
    if model == "random_walk":
        return _random_walk(values, bars, config, steps)
    if model == "historical_return":
        return _historical_return(values, bars, config, steps)
    if model == "seasonal_naive":
        return _seasonal_naive(values, bars, config, steps)
    if model in {"sma", "wma", "ema"}:
        return _moving_average(values, bars, config, steps, model)
    if model == "ses":
        return _ses(values, bars, config, steps)
    if model == "holt":
        return _holt(values, bars, config, steps)
    if model == "holt_winters":
        return _holt_winters(values, bars, config, steps)
    if model == "arima":
        return _arima(values, bars, config, steps)
    raise StatisticalValidationError(f"no implementation for model {model!r}")


__all__ = [
    "MINIMUM_BY_MODEL",
    "MODEL_IDS",
    "MODEL_METADATA",
    "forecast_model",
]
