"""Feature selection diagnostics.

This module reports evidence; it never deletes a feature.  A correlation is a
reason to investigate redundancy, not a mandate, and a high-missingness or
unstable feature is retained with a recorded decision for a later model phase.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from novacontrol.novamarket.features.models import (
    SELECTION_VERSION,
    FeatureSet,
    FeatureStatus,
    SelectionDecision,
    SelectionIssue,
    SelectionReport,
)

_LEAKAGE_TOKENS = (
    "future",
    "forward_return",
    "target",
    "label",
    "outcome",
    "lookahead",
    "next_bar",
)


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rows(rows: Sequence[Mapping[str, Any]] | Sequence[FeatureSet]) -> tuple[list[dict[str, float | None]], tuple[str, ...]]:
    materialized: list[dict[str, float | None]] = []
    names: set[str] = set()
    for item in rows:
        if isinstance(item, FeatureSet):
            names.update(item.feature_names)
            materialized.append(
                {
                    name: (
                        float(item.values[name])
                        if name in item.values
                        and item.statuses.get(name)
                        not in (
                            FeatureStatus.MISSING,
                            FeatureStatus.UNAVAILABLE,
                            FeatureStatus.INVALID,
                        )
                        else None
                    )
                    for name in item.feature_names
                }
            )
        else:
            names.update(str(name) for name in item)
            materialized.append(
                {
                    str(name): _number(value)
                    for name, value in item.items()
                }
            )
    return materialized, tuple(sorted(names))


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(right) != len(left):
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right)
    )
    left_variance = sum((a - left_mean) ** 2 for a in left)
    right_variance = sum((b - right_mean) ** 2 for b in right)
    if left_variance <= 0 or right_variance <= 0:
        return None
    return numerator / math.sqrt(left_variance * right_variance)


def diagnose(
    rows: Sequence[Mapping[str, Any]] | Sequence[FeatureSet],
    *,
    feature_names: Sequence[str] | None = None,
    correlation_threshold: float = 0.95,
    missing_threshold: float = 0.5,
    stability_threshold: float = 1.5,
    leakage_safe: bool = True,
    report_id: str = "",
    generated_at: datetime | None = None,
) -> SelectionReport:
    """Report correlation, missingness, instability and suspected leakage."""
    if not rows:
        raise ValueError("feature selection needs at least one observation")
    materialized, discovered = _rows(rows)
    selected = tuple(feature_names) if feature_names is not None else discovered
    if not selected:
        raise ValueError("feature selection needs at least one feature")
    if not 0.0 <= correlation_threshold <= 1.0:
        raise ValueError("correlation_threshold must be within [0, 1]")
    if not 0.0 <= missing_threshold <= 1.0:
        raise ValueError("missing_threshold must be within [0, 1]")
    decisions: list[SelectionDecision] = []
    values_by_name: dict[str, list[float | None]] = {
        name: [row.get(name) for row in materialized] for name in selected
    }
    for name in selected:
        values = values_by_name[name]
        missing_count = sum(1 for value in values if value is None)
        missing_fraction = missing_count / len(values)
        if missing_fraction > missing_threshold:
            decisions.append(
                SelectionDecision(
                    feature=name,
                    issue=SelectionIssue.EXCESSIVE_MISSINGNESS,
                    detail=f"{missing_count}/{len(values)} observations are missing",
                )
            )
        present = [value for value in values if value is not None]
        if len(present) >= 2 and _variance(present) <= 1e-12:
            decisions.append(
                SelectionDecision(
                    feature=name,
                    issue=SelectionIssue.ZERO_VARIANCE,
                    detail="the training window contains a single repeated value",
                )
            )
        elif present and _variance(present) < 1e-8:
            decisions.append(
                SelectionDecision(
                    feature=name,
                    issue=SelectionIssue.LOW_VARIANCE,
                    detail="the training variance is too small for a stable model input",
                )
            )
        # Stability compares two time-ordered halves; it is not a test-set score.
        half = len(values) // 2
        left = [value for value in values[:half] if value is not None]
        right = [value for value in values[half:] if value is not None]
        if len(left) >= 3 and len(right) >= 3:
            pooled = math.sqrt(max((_variance(left) + _variance(right)) / 2.0, 0.0))
            if pooled > 0 and abs(_mean(left) - _mean(right)) / pooled > stability_threshold:
                decisions.append(
                    SelectionDecision(
                        feature=name,
                        issue=SelectionIssue.UNSTABLE,
                        detail="the mean shifts materially between the first and second half",
                    )
                )
        if not leakage_safe or any(token in name.lower() for token in _LEAKAGE_TOKENS):
            decisions.append(
                SelectionDecision(
                    feature=name,
                    issue=SelectionIssue.LEAKAGE,
                    detail="the name or lineage is not established as available at the feature timestamp",
                )
            )

    for left_index, left_name in enumerate(selected):
        left_values = values_by_name[left_name]
        for right_name in selected[left_index + 1 :]:
            right_values = values_by_name[right_name]
            pairs = [
                (left, right)
                for left, right in zip(left_values, right_values)
                if left is not None and right is not None
            ]
            if len(pairs) < 10:
                continue
            correlation = _correlation(
                [pair[0] for pair in pairs], [pair[1] for pair in pairs]
            )
            if correlation is not None and abs(correlation) >= correlation_threshold:
                decisions.append(
                    SelectionDecision(
                        feature=right_name,
                        issue=SelectionIssue.HIGHLY_CORRELATED,
                        detail=f"correlation {correlation:+.4f} with {left_name}",
                        related_to=left_name,
                    )
                )

    identifier = report_id or "sel_" + _digest(
        {
            "features": selected,
            "rows": len(materialized),
            "rows_digest": _digest(
                {
                    "rows": materialized,
                    "features": selected,
                }
            ),
            "correlation_threshold": correlation_threshold,
            "missing_threshold": missing_threshold,
            "leakage_safe": leakage_safe,
        }
    )[:24]
    return SelectionReport(
        report_id=identifier,
        version=SELECTION_VERSION,
        feature_names=selected,
        decisions=tuple(decisions),
        correlation_threshold=correlation_threshold,
        missing_threshold=missing_threshold,
        observations=len(materialized),
        leakage_safe=leakage_safe,
        generated_at=generated_at or datetime.now(UTC),
    )


__all__ = ["diagnose"]
