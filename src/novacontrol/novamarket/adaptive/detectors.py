"""Deterministic, dependency-free detectors used by PI-16.

The selected methods are deliberately limited to what is defensible on small
NovaMarket windows without new numerical dependencies:

* PSI and Jensen-Shannon divergence for binned distribution movement;
* a two-sample Kolmogorov-Smirnov statistic with an asymptotic p-value;
* Page-Hinkley and two-sided CUSUM for sequential mean/error shifts;
* Fisher's z comparison and rolling error ratios for concept drift.

Every function returns its parameters and sample sizes so a caller can see why
a result was produced.  None of them infers future observations.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

_EPSILON = 1e-9


def clean_values(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires at least one value")
    position = (len(ordered) - 1) * min(1.0, max(0.0, probability))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def histogram(
    values: Sequence[float],
    *,
    bins: int = 10,
    lower: float | None = None,
    upper: float | None = None,
) -> list[float]:
    """Return non-negative bin counts over a shared, explicit range."""
    if not values:
        return []
    bins = max(2, min(int(bins), 50))
    low = min(values) if lower is None else float(lower)
    high = max(values) if upper is None else float(upper)
    if high <= low:
        return [float(len(values))] + [0.0] * (bins - 1)
    width = (high - low) / bins
    counts = [0.0] * bins
    for value in values:
        if value < low or value > high:
            continue
        index = min(bins - 1, max(0, int((value - low) / width)))
        counts[index] += 1.0
    return counts


def _proportions(counts: Sequence[float]) -> list[float]:
    total = sum(counts)
    if total <= 0:
        return []
    return [max(_EPSILON, count / total) for count in counts]


def population_stability_index(
    reference: Sequence[float], current: Sequence[float], *, bins: int = 10
) -> dict[str, Any]:
    """PSI over equal-width bins spanning both windows."""
    if len(reference) < 2 or len(current) < 2:
        return {
            "score": None,
            "bins": bins,
            "reference_samples": len(reference),
            "current_samples": len(current),
            "insufficient": True,
        }
    low = min(min(reference), min(current))
    high = max(max(reference), max(current))
    reference_counts = histogram(reference, bins=bins, lower=low, upper=high)
    current_counts = histogram(current, bins=bins, lower=low, upper=high)
    ref = _proportions(reference_counts)
    cur = _proportions(current_counts)
    if not ref or not cur:
        return {
            "score": None,
            "bins": bins,
            "reference_samples": len(reference),
            "current_samples": len(current),
            "insufficient": True,
        }
    score = sum((c - r) * math.log(c / r) for r, c in zip(ref, cur, strict=True))
    return {
        "score": max(0.0, score),
        "bins": bins,
        "reference_samples": len(reference),
        "current_samples": len(current),
        "insufficient": False,
        "degenerate": high <= low,
    }


def ks_2samp(reference: Sequence[float], current: Sequence[float]) -> dict[str, Any]:
    """Two-sample KS statistic with the standard asymptotic p-value."""
    if len(reference) < 2 or len(current) < 2:
        return {
            "statistic": None,
            "p_value": None,
            "reference_samples": len(reference),
            "current_samples": len(current),
            "insufficient": True,
        }
    a = sorted(reference)
    b = sorted(current)
    combined = sorted(set(a + b))
    i = j = 0
    maximum = 0.0
    for value in combined:
        while i < len(a) and a[i] <= value:
            i += 1
        while j < len(b) and b[j] <= value:
            j += 1
        maximum = max(maximum, abs(i / len(a) - j / len(b)))
    effective = math.sqrt((len(a) * len(b)) / (len(a) + len(b)))
    lam = (effective + 0.12 + 0.11 / effective) * maximum
    if lam <= 0:
        p_value = 1.0
    else:
        total = 0.0
        for k in range(1, 101):
            term = 2.0 * ((-1) ** (k - 1)) * math.exp(-2.0 * k * k * lam * lam)
            total += term
            if abs(term) < 1e-12:
                break
        p_value = min(1.0, max(0.0, total))
    return {
        "statistic": maximum,
        "p_value": p_value,
        "reference_samples": len(reference),
        "current_samples": len(current),
        "insufficient": False,
        "asymptotic": True,
    }


def jensen_shannon(
    reference: Sequence[float], current: Sequence[float], *, bins: int = 10
) -> dict[str, Any]:
    """Jensen-Shannon divergence in bits, using the shared histogram range."""
    if len(reference) < 2 or len(current) < 2:
        return {"score": None, "insufficient": True, "base": 2}
    low = min(min(reference), min(current))
    high = max(max(reference), max(current))
    ref = _proportions(histogram(reference, bins=bins, lower=low, upper=high))
    cur = _proportions(histogram(current, bins=bins, lower=low, upper=high))
    if not ref or not cur:
        return {"score": None, "insufficient": True, "base": 2}
    midpoint = [(a + b) / 2.0 for a, b in zip(ref, cur, strict=True)]

    def divergence(left: Sequence[float], right: Sequence[float]) -> float:
        return sum(p * math.log2(p / q) for p, q in zip(left, right, strict=True))

    score = 0.5 * divergence(ref, midpoint) + 0.5 * divergence(cur, midpoint)
    return {
        "score": max(0.0, min(1.0, score)),
        "insufficient": False,
        "base": 2,
        "bins": bins,
    }


def page_hinkley(
    values: Sequence[float], *, delta: float = 0.01, threshold: float = 6.0
) -> dict[str, Any]:
    """Page-Hinkley cumulative mean-shift detector (one-sided upward)."""
    if len(values) < 3:
        return {"detected": False, "score": 0.0, "index": None, "insufficient": True}
    average = sum(values) / len(values)
    cumulative = 0.0
    minimum = 0.0
    maximum_score = 0.0
    detected_index: int | None = None
    for index, value in enumerate(values):
        cumulative += value - average - float(delta)
        minimum = min(minimum, cumulative)
        score = cumulative - minimum
        maximum_score = max(maximum_score, score)
        if score > threshold and detected_index is None:
            detected_index = index
    return {
        "detected": detected_index is not None,
        "score": maximum_score,
        "index": detected_index,
        "threshold": threshold,
        "delta": delta,
        "insufficient": False,
    }


def cusum(
    reference: Sequence[float], values: Sequence[float], *, threshold: float = 5.0
) -> dict[str, Any]:
    """Two-sided standardized CUSUM against a reference mean and deviation."""
    center = mean(reference)
    spread = stdev(reference)
    if center is None or spread in (None, 0) or len(values) < 3:
        return {
            "detected": False,
            "score": 0.0,
            "index": None,
            "insufficient": True,
            "reason": "reference window has no usable variation",
        }
    positive = negative = 0.0
    maximum = 0.0
    detected_index: int | None = None
    for index, value in enumerate(values):
        standardized = (value - center) / spread
        positive = max(0.0, positive + standardized - 0.5)
        negative = min(0.0, negative + standardized + 0.5)
        maximum = max(maximum, positive, abs(negative))
        if max(positive, abs(negative)) > threshold and detected_index is None:
            detected_index = index
    return {
        "detected": detected_index is not None,
        "score": maximum,
        "index": detected_index,
        "threshold": threshold,
        "reference_mean": center,
        "reference_stdev": spread,
        "insufficient": False,
    }


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_ss = sum((a - left_mean) ** 2 for a in left)
    right_ss = sum((b - right_mean) ** 2 for b in right)
    if left_ss <= _EPSILON or right_ss <= _EPSILON:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def fisher_relationship_change(
    reference_x: Sequence[float],
    reference_y: Sequence[float],
    current_x: Sequence[float],
    current_y: Sequence[float],
) -> dict[str, Any]:
    """Compare two Pearson relationships using Fisher's z transform."""
    reference_r = pearson(reference_x, reference_y)
    current_r = pearson(current_x, current_y)
    if reference_r is None or current_r is None or len(reference_x) < 4 or len(current_x) < 4:
        return {
            "detected": False,
            "delta": None,
            "p_value": None,
            "insufficient": True,
            "reference_correlation": reference_r,
            "current_correlation": current_r,
        }
    clipped_ref = max(-0.999999, min(0.999999, reference_r))
    clipped_cur = max(-0.999999, min(0.999999, current_r))
    z_ref = math.atanh(clipped_ref)
    z_cur = math.atanh(clipped_cur)
    standard_error = math.sqrt(
        1.0 / max(1, len(reference_x) - 3) + 1.0 / max(1, len(current_x) - 3)
    )
    z_score = abs(z_ref - z_cur) / standard_error if standard_error else 0.0
    p_value = math.erfc(z_score / math.sqrt(2.0))
    return {
        "detected": None,
        "delta": abs(reference_r - current_r),
        "p_value": p_value,
        "z_score": z_score,
        "insufficient": False,
        "reference_correlation": reference_r,
        "current_correlation": current_r,
    }


def rolling_error_ratio(
    reference_errors: Sequence[float], current_errors: Sequence[float]
) -> dict[str, Any]:
    reference = [abs(value) for value in reference_errors]
    current = [abs(value) for value in current_errors]
    ref_mean = mean(reference)
    cur_mean = mean(current)
    if len(reference) < 2 or len(current) < 2 or ref_mean in (None, 0):
        return {
            "ratio": None,
            "reference_mae": ref_mean,
            "current_mae": cur_mean,
            "insufficient": True,
        }
    assert ref_mean is not None
    assert cur_mean is not None
    return {
        "ratio": cur_mean / ref_mean,
        "reference_mae": ref_mean,
        "current_mae": cur_mean,
        "change": (cur_mean - ref_mean) / ref_mean,
        "insufficient": False,
        "reference_samples": len(reference),
        "current_samples": len(current),
    }


def action_distribution_shift(
    reference: dict[str, float], current: dict[str, float]
) -> dict[str, Any]:
    """Total-variation distance between two action distributions."""
    if not reference or not current:
        return {"shift": None, "insufficient": True}
    keys = set(reference) | set(current)
    ref_total = sum(max(0.0, float(reference.get(key, 0.0))) for key in keys)
    cur_total = sum(max(0.0, float(current.get(key, 0.0))) for key in keys)
    if ref_total <= 0 or cur_total <= 0:
        return {"shift": None, "insufficient": True}
    distance = 0.5 * sum(
        abs(
            max(0.0, float(reference.get(key, 0.0))) / ref_total
            - max(0.0, float(current.get(key, 0.0))) / cur_total
        )
        for key in keys
    )
    return {"shift": distance, "insufficient": False}
