"""A small, deterministic ARIMA implementation for PI-05.

``statsmodels`` is not installed in this project and adding its pandas/scipy
dependency chain solely for the baseline phase would change the runtime
footprint of every NovaControl install.  This module therefore implements the
bounded ARIMA(p,d,q) estimator the phase needs, in the standard library, with the
same safeguards an established library would provide at this scope:

* an augmented Dickey-Fuller style stationarity check selects differencing;
* conditional least squares estimates AR/MA coefficients with a ridge-stabilised
  normal-equation solve;
* an unstable AR companion polynomial is refused;
* residual diagnostics and an approximate Ljung-Box p-value are recorded;
* forecasts and normal-residual prediction intervals are generated without
  touching any observation after the fit cutoff.

It is intentionally not a general ARIMA/SARIMA replacement.  The limitations are
recorded with every fit and exposed in the PI-05 contract.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from novacontrol.novamarket.statistical.models import (
    ModelConfig,
    ModelFitError,
)


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _normal_quantile(probability: float) -> float:
    return statistics.NormalDist().inv_cdf(probability)


def _chi2_survival(value: float, degrees: int) -> float | None:
    """Wilson-Hilferty approximation, adequate for a diagnostic p-value."""
    if degrees <= 0 or not math.isfinite(value) or value < 0:
        return None
    z = ((value / degrees) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * degrees))) / math.sqrt(
        2.0 / (9.0 * degrees)
    )
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse with partial pivoting and a small ridge."""
    size = len(matrix)
    if size == 0:
        return []
    aug = [
        [float(value) for value in matrix[row]]
        + [1.0 if row == column else 0.0 for column in range(size)]
        for row in range(size)
    ]
    for row in range(size):
        aug[row][row] += 1e-10
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(aug[row][column]))
        if abs(aug[pivot][column]) < 1e-12:
            return None
        aug[column], aug[pivot] = aug[pivot], aug[column]
        divisor = aug[column][column]
        aug[column] = [value / divisor for value in aug[column]]
        for row in range(size):
            if row == column:
                continue
            factor = aug[row][column]
            if factor == 0.0:
                continue
            aug[row] = [
                aug[row][index] - factor * aug[column][index]
                for index in range(size * 2)
            ]
    return [row[size:] for row in aug]


def _ols(
    rows: Sequence[Sequence[float]], target: Sequence[float]
) -> tuple[list[float], list[float], float]:
    """Ordinary least squares with a ridge-stabilised normal-equation solve."""
    if not rows or len(rows) != len(target):
        raise ModelFitError("OLS needs equally sized, non-empty rows and targets")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ModelFitError("OLS design rows have inconsistent widths")
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for row, value in zip(rows, target):
        for left in range(width):
            rhs[left] += row[left] * value
            for right in range(width):
                normal[left][right] += row[left] * row[right]
    inverse = _inverse(normal)
    if inverse is None:
        raise ModelFitError("the normal equations are singular")
    beta = [
        sum(inverse[row][column] * rhs[column] for column in range(width))
        for row in range(width)
    ]
    residuals = [
        value - sum(coefficient * cell for coefficient, cell in zip(beta, row))
        for row, value in zip(rows, target)
    ]
    if len(residuals) < 2:
        raise ModelFitError("too few residuals for a variance estimate")
    variance = sum(item * item for item in residuals) / (len(residuals) - width)
    if not math.isfinite(variance) or variance < 0:
        raise ModelFitError("the residual variance is not finite")
    return beta, residuals, variance


def difference(series: Sequence[float], order: int) -> tuple[list[float], list[float]]:
    """Difference ``order`` times and return (differenced, last value per level)."""
    if order < 0:
        raise ValueError("a differencing order cannot be negative")
    if len(series) <= order:
        raise ModelFitError("the series is too short for this differencing order")
    values = [float(item) for item in series]
    levels: list[float] = [values[-1]]
    for _ in range(order):
        values = [current - previous for previous, current in zip(values, values[1:])]
        if not values:
            raise ModelFitError("differencing consumed the whole series")
        levels.append(values[-1])
    if not all(math.isfinite(item) for item in values):
        raise ModelFitError("differencing produced non-finite values")
    return values, levels


def adf_test(series: Sequence[float], *, lags: int = 1) -> dict[str, Any]:
    """A one-lag ADF-style regression; negative statistics indicate stationarity."""
    values = [float(item) for item in series]
    if len(values) < max(20, lags + 8):
        return {
            "statistic": None,
            "stationary": None,
            "lags": lags,
            "reason": "too few observations for a stationarity check",
        }
    differences = [current - previous for previous, current in zip(values, values[1:])]
    rows: list[list[float]] = []
    target: list[float] = []
    for index in range(lags + 1, len(values)):
        row = [1.0, values[index - 1]]
        row.extend(differences[index - 1 - lag] for lag in range(1, lags + 1))
        rows.append(row)
        target.append(differences[index - 1])
    try:
        beta, residuals, variance = _ols(rows, target)
    except ModelFitError as exc:
        return {
            "statistic": None,
            "stationary": None,
            "lags": lags,
            "reason": str(exc),
        }
    width = len(rows[0])
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    for row in rows:
        for left in range(width):
            for right in range(width):
                normal[left][right] += row[left] * row[right]
    inverse = _inverse(normal)
    if inverse is None:
        return {
            "statistic": None,
            "stationary": None,
            "lags": lags,
            "reason": "the stationarity regression is singular",
        }
    standard_error = math.sqrt(max(variance * inverse[1][1], 1e-18))
    statistic = beta[1] / standard_error
    # Approximate 5% critical value for a constant-only ADF regression.
    return {
        "statistic": round(statistic, 8),
        "stationary": statistic < -2.86,
        "lags": lags,
        "critical_value_5pct": -2.86,
        "observations": len(rows),
        "residual_std": math.sqrt(max(variance, 0.0)),
        "coefficients": [round(item, 10) for item in beta],
    }


def _fit_ar(values: Sequence[float], order: int) -> tuple[list[float], list[float], float]:
    if order <= 0:
        return [0.0], [0.0 for _ in values], statistics.pvariance(values) if len(values) > 1 else 0.0
    if len(values) <= order + 2:
        raise ModelFitError("not enough observations for the AR order")
    rows: list[list[float]] = []
    target: list[float] = []
    for index in range(order, len(values)):
        rows.append([1.0, *(values[index - lag] for lag in range(1, order + 1))])
        target.append(values[index])
    beta, residuals, variance = _ols(rows, target)
    aligned = [0.0 for _ in range(order)] + residuals
    return beta, aligned, variance


def _arma_design(
    values: Sequence[float],
    errors: Sequence[float],
    p: int,
    q: int,
) -> tuple[list[list[float]], list[float], int]:
    start = max(p, q)
    rows: list[list[float]] = []
    target: list[float] = []
    for index in range(start, len(values)):
        row = [1.0]
        row.extend(values[index - lag] for lag in range(1, p + 1))
        row.extend(errors[index - lag] for lag in range(1, q + 1))
        rows.append(row)
        target.append(values[index])
    return rows, target, start


def _fit_arma(
    values: Sequence[float], p: int, q: int
) -> tuple[list[float], list[float], float]:
    if p == 0 and q == 0:
        mean = _mean(values)
        residuals = [value - mean for value in values]
        variance = statistics.pvariance(values) if len(values) > 1 else 0.0
        return [mean], residuals, variance
    long_order = min(p + q, max(p, 1))
    _, errors, _ = _fit_ar(values, long_order)
    errors = list(errors)
    beta = [0.0 for _ in range(1 + p + q)]
    variance = statistics.pvariance(values) if len(values) > 1 else 0.0
    for _ in range(4):
        rows, target, start = _arma_design(values, errors, p, q)
        beta, _, variance = _ols(rows, target)
        new_errors = [0.0 for _ in range(len(values))]
        for index in range(start, len(values)):
            prediction = beta[0]
            prediction += sum(
                beta[lag] * values[index - lag] for lag in range(1, p + 1)
            )
            prediction += sum(
                beta[p + lag] * errors[index - lag] for lag in range(1, q + 1)
            )
            new_errors[index] = values[index] - prediction
        if all(
            abs(new_errors[index] - errors[index]) < 1e-10
            for index in range(start, len(values))
        ):
            errors = new_errors
            break
        errors = new_errors
    return beta, errors, variance


def _spectral_radius(coefficients: Sequence[float]) -> float:
    """Power iteration on the AR companion matrix's largest eigenvalue magnitude."""
    order = len(coefficients)
    if order == 0:
        return 0.0
    vector = [1.0 for _ in range(order)]
    for _ in range(200):
        next_vector = [0.0 for _ in range(order)]
        next_vector[0] = sum(coefficients[index] * vector[index] for index in range(order))
        for index in range(1, order):
            next_vector[index] = vector[index - 1]
        norm = math.sqrt(sum(item * item for item in next_vector))
        if norm < 1e-15:
            return 0.0
        vector = [item / norm for item in next_vector]
    # Rayleigh quotient of the companion matrix.
    image = [0.0 for _ in range(order)]
    image[0] = sum(coefficients[index] * vector[index] for index in range(order))
    for index in range(1, order):
        image[index] = vector[index - 1]
    return abs(sum(left * right for left, right in zip(vector, image)))


def _autocorrelation(values: Sequence[float], lag: int) -> float:
    if lag <= 0 or len(values) <= lag:
        return 0.0
    mean = _mean(values)
    denominator = sum((item - mean) ** 2 for item in values)
    if denominator <= 0:
        return 0.0
    numerator = sum(
        (values[index] - mean) * (values[index - lag] - mean)
        for index in range(lag, len(values))
    )
    return numerator / denominator


def ljung_box(residuals: Sequence[float], *, lags: int = 10) -> dict[str, Any]:
    values = [float(item) for item in residuals if math.isfinite(item)]
    if len(values) < 12:
        return {"statistic": None, "p_value": None, "lags": 0}
    chosen = max(1, min(int(lags), len(values) // 5))
    total = 0.0
    for lag in range(1, chosen + 1):
        rho = _autocorrelation(values, lag)
        total += rho * rho / (len(values) - lag)
    statistic = len(values) * (len(values) + 2) * total
    survival = _chi2_survival(statistic, chosen)
    return {
        "statistic": round(statistic, 8),
        "p_value": None if survival is None else round(float(survival), 8),
        "lags": chosen,
        "residual_autocorrelation_lag1": round(_autocorrelation(values, 1), 8),
    }


@dataclass(frozen=True, slots=True)
class ARIMAResult:
    order: tuple[int, int, int]
    differencing: int
    coefficients: tuple[float, ...]
    residuals: tuple[float, ...]
    sigma2: float
    levels: tuple[float, ...]
    differenced: tuple[float, ...]
    fitted_values: tuple[float, ...]
    stationarity: dict[str, Any]
    residual_diagnostics: dict[str, Any]
    training_observations: int
    version: str = "pi05-arima-1.0"

    @property
    def residual_std(self) -> float:
        return math.sqrt(max(self.sigma2, 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": list(self.order),
            "differencing": self.differencing,
            "coefficients": [round(item, 10) for item in self.coefficients],
            "sigma2": round(self.sigma2, 12),
            "residual_std": round(self.residual_std, 12),
            "levels": [round(item, 10) for item in self.levels],
            "stationarity": dict(self.stationarity),
            "residual_diagnostics": dict(self.residual_diagnostics),
            "training_observations": self.training_observations,
            "version": self.version,
        }


def _choose_differencing(
    series: Sequence[float],
    requested: int,
    *,
    auto: bool = False,
    max_differencing: int = 2,
) -> tuple[int, dict[str, Any]]:
    if not auto:
        try:
            differenced, _ = difference(series, requested)
        except ModelFitError as exc:
            raise ModelFitError(f"cannot difference {requested} time(s): {exc}") from exc
        before = adf_test(series)
        after = adf_test(differenced)
        return requested, {
            "selected": requested,
            "source": "configured",
            "before": before,
            "after": after,
        }
    before = adf_test(series)
    if before.get("stationary") is True:
        return 0, {"selected": 0, "source": "adf", "before": before, "after": before}
    current = list(series)
    last = before
    for order in range(1, max_differencing + 1):
        current, _ = difference(current, 1)
        tested = adf_test(current)
        last = tested
        if tested.get("stationary") is True:
            return order, {
                "selected": order,
                "source": "adf",
                "before": before,
                "after": tested,
            }
    return max_differencing, {
        "selected": max_differencing,
        "source": "adf-limit",
        "before": before,
        "after": last,
    }


def fit_arima(
    closes: Sequence[float],
    config: ModelConfig,
) -> tuple[ARIMAResult, ModelConfig]:
    """Fit the configured ARIMA and return its result plus the effective config."""
    values = [float(item) for item in closes]
    if len(values) < max(30, config.min_observations):
        raise ModelFitError(
            f"ARIMA needs at least {max(30, config.min_observations)} closes; "
            f"got {len(values)}"
        )
    if not all(math.isfinite(item) and item > 0 for item in values):
        raise ModelFitError("ARIMA closes must be finite and positive")
    p, requested_d, q = config.arima_order
    if p + q > config.max_arima_order:
        raise ModelFitError("the configured ARIMA p+q exceeds the allowed bound")
    d, stationarity = _choose_differencing(
        values,
        requested_d,
        auto=config.auto_differencing,
        max_differencing=2,
    )
    differenced, levels = difference(values, d)
    minimum = max(p + q + 5, 20)
    if len(differenced) < minimum:
        raise ModelFitError(
            f"after differencing only {len(differenced)} observations remain; "
            f"{minimum} are needed"
        )
    beta, errors, variance = _fit_arma(differenced, p, q)
    coefficients = tuple(beta)
    if p:
        phi = coefficients[1 : 1 + p]
        radius = _spectral_radius(phi)
        if not math.isfinite(radius) or radius >= 1.0:
            raise ModelFitError(
                f"AR coefficients are unstable (companion radius {radius:.4f} >= 1)"
            )
    else:
        radius = 0.0
    fitted = []
    for index in range(len(differenced)):
        if index < max(p, q):
            fitted.append(0.0)
            continue
        prediction = coefficients[0]
        prediction += sum(
            coefficients[lag] * differenced[index - lag]
            for lag in range(1, p + 1)
        )
        prediction += sum(
            coefficients[p + lag] * errors[index - lag]
            for lag in range(1, q + 1)
        )
        fitted.append(prediction)
    diagnostics = ljung_box(errors)
    diagnostics["companion_radius"] = round(radius, 8)
    diagnostics["coefficient_stability"] = radius < 1.0
    result = ARIMAResult(
        order=(p, d, q),
        differencing=d,
        coefficients=coefficients,
        residuals=tuple(errors),
        sigma2=float(variance),
        levels=tuple(levels),
        differenced=tuple(differenced),
        fitted_values=tuple(fitted),
        stationarity=stationarity,
        residual_diagnostics=diagnostics,
        training_observations=len(values),
    )
    return result, config.with_changes(arima_order=(p, d, q))


def forecast_arima(
    result: ARIMAResult,
    steps: int,
    *,
    last_price: float,
    interval_level: float,
) -> tuple[float, float | None, float | None, dict[str, Any]]:
    """Forecast the level path and a normal-residual interval for the horizon."""
    if steps < 1:
        raise ModelFitError("an ARIMA forecast horizon must be positive")
    p, _d, q = result.order
    coefficients = list(result.coefficients)
    history = list(result.differenced)
    errors = list(result.residuals)
    forecasts: list[float] = []
    for _ in range(steps):
        index = len(history)
        prediction = coefficients[0]
        prediction += sum(
            coefficients[lag] * history[index - lag] for lag in range(1, p + 1)
        )
        prediction += sum(
            coefficients[p + lag] * errors[index - lag] for lag in range(1, q + 1)
        )
        if not math.isfinite(prediction):
            raise ModelFitError("the ARIMA recursion produced a non-finite value")
        forecasts.append(prediction)
        history.append(prediction)
        errors.append(0.0)
    # Undifference the forecast path, one stage at a time.
    current = list(forecasts)
    for stage in range(result.differencing - 1, -1, -1):
        level = result.levels[stage]
        accumulated: list[float] = []
        for value in current:
            level += value
            accumulated.append(level)
        current = accumulated
    predicted_price = current[-1]
    if not math.isfinite(predicted_price) or predicted_price <= 0:
        raise ModelFitError("the ARIMA level forecast is non-positive")
    expected_return = predicted_price / last_price - 1.0
    z = _normal_quantile((1.0 + interval_level) / 2.0)
    # A conservative random-walk-like horizon scaling of the residual error.
    relative_sigma = result.residual_std / max(abs(last_price), 1e-9)
    horizon_sigma = relative_sigma * math.sqrt(max(steps, 1))
    lower = expected_return - z * horizon_sigma
    upper = expected_return + z * horizon_sigma
    return (
        expected_return,
        max(lower, -0.999999),
        upper,
        {
            "forecast_path": [round(item, 10) for item in current],
            "steps": steps,
            "relative_sigma": round(horizon_sigma, 12),
            "interval_z": round(z, 8),
            "interval_note": (
                "normal residual interval; it does not include parameter "
                "uncertainty or market-event risk"
            ),
        },
    )


__all__ = [
    "ARIMAResult",
    "adf_test",
    "difference",
    "fit_arima",
    "forecast_arima",
    "ljung_box",
]
