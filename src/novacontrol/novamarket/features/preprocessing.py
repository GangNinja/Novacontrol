"""Leakage-safe feature preprocessing.

The fit step accepts only training rows.  It never looks at a test row, and a
missing value is only imputed when the caller explicitly chooses a policy; the
result keeps an ``IMPUTED`` status and the original feature names so a model
cannot mistake a fill for an observation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from novacontrol.novamarket.features.models import (
    PREPROCESSING_VERSION,
    FeatureStatus,
    MissingPolicy,
    PreprocessingFit,
    PreprocessingMethod,
    TransformResult,
)


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None if value is None else (1.0 if value else 0.0)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("a quantile needs at least one value")
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _feature_names(
    rows: Sequence[Mapping[str, Any]], feature_names: Sequence[str] | None
) -> tuple[str, ...]:
    if feature_names is not None:
        selected = tuple(str(name) for name in feature_names)
        if not selected:
            raise ValueError("preprocessing needs at least one feature")
        return selected
    discovered = sorted({str(name) for row in rows for name in row})
    if not discovered:
        raise ValueError("preprocessing needs at least one feature")
    return tuple(discovered)


def fit_preprocessor(
    rows: Sequence[Mapping[str, Any]],
    *,
    feature_names: Sequence[str] | None = None,
    method: PreprocessingMethod | str = PreprocessingMethod.STANDARD,
    missing_policy: MissingPolicy | str = MissingPolicy.MEDIAN,
    outlier_z: float = 6.0,
    preprocessing_id: str = "",
    fitted_at: datetime | None = None,
) -> PreprocessingFit:
    """Fit parameters on the supplied TRAINING rows only."""
    if not rows:
        raise ValueError("preprocessing cannot be fitted without training rows")
    selected = _feature_names(rows, feature_names)
    method = PreprocessingMethod(method)
    missing_policy = MissingPolicy(missing_policy)
    if outlier_z <= 0:
        raise ValueError("outlier_z must be positive")
    columns: dict[str, list[float]] = {name: [] for name in selected}
    missing: dict[str, int] = {name: 0 for name in selected}
    for row in rows:
        for name in selected:
            value = _number(row.get(name))
            if value is None:
                missing[name] += 1
            else:
                columns[name].append(value)
    empty = [name for name, values in columns.items() if not values]
    if empty:
        raise ValueError(f"cannot fit a feature with no training values: {empty[0]!r}")

    parameters: dict[str, Any] = {
        "kind": str(method),
        "missing_policy": str(missing_policy),
        "outlier_z": float(outlier_z),
        "missing_counts": missing,
        "training_rows": len(rows),
        "mean": {
            name: sum(values) / len(values) for name, values in columns.items()
        },
        "median": {name: _median(values) for name, values in columns.items()},
    }
    if method is PreprocessingMethod.STANDARD:
        center: dict[str, float] = {}
        scale: dict[str, float] = {}
        for name, values in columns.items():
            mean = sum(values) / len(values)
            variance = (
                sum((value - mean) ** 2 for value in values) / (len(values) - 1)
                if len(values) > 1
                else 0.0
            )
            center[name] = mean
            scale[name] = variance**0.5 if variance > 0 else 1.0
        parameters["center"] = center
        parameters["scale"] = scale
    elif method is PreprocessingMethod.ROBUST:
        center = {name: _median(values) for name, values in columns.items()}
        scale = {}
        for name, values in columns.items():
            median = center[name]
            deviations = [abs(value - median) for value in values]
            mad = _median(deviations) * 1.4826
            if mad <= 0:
                iqr = _quantile(values, 0.75) - _quantile(values, 0.25)
                mad = iqr if iqr > 0 else 1.0
            scale[name] = mad
        parameters["center"] = center
        parameters["scale"] = scale
    elif method is PreprocessingMethod.MINMAX:
        minimum = {name: min(values) for name, values in columns.items()}
        maximum = {name: max(values) for name, values in columns.items()}
        parameters["minimum"] = minimum
        parameters["maximum"] = maximum
        parameters["scale"] = {
            name: (maximum[name] - minimum[name]) if maximum[name] > minimum[name] else 1.0
            for name in selected
        }
    else:  # L2 normalizes each row, no fitted column statistic is required.
        parameters["norm"] = "row_l2"

    training_digest = _digest(
        {
            "rows": [
                {name: _number(row.get(name)) for name in selected}
                for row in rows
            ],
            "feature_names": selected,
            "method": str(method),
            "missing_policy": str(missing_policy),
            "outlier_z": float(outlier_z),
        }
    )
    identifier = preprocessing_id or f"pp_{training_digest[:24]}"
    return PreprocessingFit(
        preprocessing_id=identifier,
        method=method,
        feature_names=selected,
        parameters=parameters,
        training_digest=training_digest,
        version=PREPROCESSING_VERSION,
        fitted_at=fitted_at or datetime.now(UTC),
    )


def transform(
    fit: PreprocessingFit,
    row: Mapping[str, Any],
    *,
    outlier_z: float | None = None,
) -> TransformResult:
    """Apply fitted training parameters to one row; no statistic is recomputed."""
    method = PreprocessingMethod(fit.method)
    parameters = dict(fit.parameters)
    policy = MissingPolicy(parameters.get("missing_policy", "median"))
    threshold = float(outlier_z if outlier_z is not None else parameters.get("outlier_z", 6.0))
    values: dict[str, float] = {}
    statuses: dict[str, FeatureStatus] = {}
    imputed: dict[str, float] = {}
    outliers: list[str] = []
    raw_values: dict[str, float | None] = {
        name: _number(row.get(name)) for name in fit.feature_names
    }
    if method is PreprocessingMethod.L2:
        norm = math.sqrt(sum(value * value for value in raw_values.values() if value is not None))
        if norm <= 0:
            norm = 1.0
    else:
        norm = 1.0

    for name in fit.feature_names:
        raw = raw_values[name]
        if raw is None:
            if policy is MissingPolicy.ERROR:
                raise ValueError(f"feature {name!r} is missing and the policy is ERROR")
            if policy is MissingPolicy.DROP:
                statuses[name] = FeatureStatus.MISSING
                continue
            if policy is MissingPolicy.ZERO:
                fill = 0.0
            elif policy is MissingPolicy.MEAN:
                fill = float(parameters.get("mean", {}).get(name, 0.0))
            elif policy is MissingPolicy.MEDIAN:
                fill = float(parameters.get("median", {}).get(name, 0.0))
            else:
                fill = float(parameters.get("center", {}).get(name, 0.0))
            transformed = _apply(name, fill, method, parameters, norm)
            values[name] = transformed
            imputed[name] = transformed
            statuses[name] = FeatureStatus.IMPUTED
            continue
        transformed = _apply(name, raw, method, parameters, norm)
        if method in (PreprocessingMethod.STANDARD, PreprocessingMethod.ROBUST):
            center = float(parameters.get("center", {}).get(name, 0.0))
            scale = float(parameters.get("scale", {}).get(name, 1.0)) or 1.0
            z_score = abs(raw - center) / scale
            if z_score > threshold:
                outliers.append(name)
        values[name] = transformed
        statuses[name] = FeatureStatus.ZERO if raw == 0 else FeatureStatus.VALUE

    completeness = (
        sum(1 for status in statuses.values() if status is not FeatureStatus.MISSING)
        / len(fit.feature_names)
        if fit.feature_names
        else 0.0
    )
    return TransformResult(
        values=values,
        statuses=statuses,
        imputed=imputed,
        outliers=tuple(outliers),
        completeness=completeness,
        preprocessing_id=fit.preprocessing_id,
    )


def _apply(
    name: str,
    value: float,
    method: PreprocessingMethod,
    parameters: Mapping[str, Any],
    norm: float,
) -> float:
    if method is PreprocessingMethod.STANDARD:
        center = float(parameters.get("center", {}).get(name, 0.0))
        scale = float(parameters.get("scale", {}).get(name, 1.0)) or 1.0
        return (value - center) / scale
    if method is PreprocessingMethod.ROBUST:
        center = float(parameters.get("center", {}).get(name, 0.0))
        scale = float(parameters.get("scale", {}).get(name, 1.0)) or 1.0
        return (value - center) / scale
    if method is PreprocessingMethod.MINMAX:
        minimum = float(parameters.get("minimum", {}).get(name, 0.0))
        scale = float(parameters.get("scale", {}).get(name, 1.0)) or 1.0
        return (value - minimum) / scale
    return value / norm


__all__ = ["fit_preprocessor", "transform"]
