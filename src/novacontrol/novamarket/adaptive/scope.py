"""Out-of-sample scoping for PI-16 reassessment evidence.

A reassessment window is only out-of-sample when it lies outside the data the
policy's weights were fitted on *and* outside the split that selected its
checkpoints.  PI-15 records both windows in ``dataset_provenance["splits"]``;
when a policy artifact does not record them the scope is UNVERIFIED and is
reported as exactly that instead of being assumed clean.

This module is pure: it classifies intervals and never evaluates, stores or
trains anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

#: The split the policy's weights were fitted on.
TRAINING_SPLIT = "train"

#: The split PI-15 used to choose the best checkpoint and hyperparameters.
SELECTION_SPLIT = "validation"

TRAINING_BASIS = "training"
SELECTION_BASIS = "selection"


def _parse(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _policy_windows(
    policy: Mapping[str, Any], split: str, basis: str
) -> list[dict[str, Any]]:
    provenance = policy.get("dataset_provenance")
    splits = provenance.get("splits") if isinstance(provenance, Mapping) else None
    if not isinstance(splits, Mapping):
        return []
    entry = splits.get(split)
    if not isinstance(entry, Mapping):
        return []
    start = _parse(entry.get("start"))
    end = _parse(entry.get("end"))
    if start is None or end is None or start >= end:
        return []
    return [
        {"split": split, "basis": basis, "start": start.isoformat(), "end": end.isoformat()}
    ]


def _overlap(
    start: datetime, end: datetime, row: Mapping[str, Any]
) -> dict[str, Any] | None:
    other_start = _parse(row["start"])
    other_end = _parse(row["end"])
    if other_start is None or other_end is None:
        return None
    overlap_start = max(start, other_start)
    overlap_end = min(end, other_end)
    if overlap_start >= overlap_end:
        return None
    window_seconds = (end - start).total_seconds()
    return {
        "split": row["split"],
        "basis": row["basis"],
        "start": row["start"],
        "end": row["end"],
        "overlap_start": overlap_start.isoformat(),
        "overlap_end": overlap_end.isoformat(),
        "overlap_seconds": round((overlap_end - overlap_start).total_seconds(), 6),
        "overlap_fraction": (
            round((overlap_end - overlap_start).total_seconds() / window_seconds, 6)
            if window_seconds
            else 0.0
        ),
    }


def reassessment_scope(
    policy: Mapping[str, Any], windows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Classify each window against the policy's own training/selection data."""
    training = _policy_windows(policy, TRAINING_SPLIT, TRAINING_BASIS)
    selection = _policy_windows(policy, SELECTION_SPLIT, SELECTION_BASIS)
    verified = bool(training)
    classified: list[dict[str, Any]] = []
    in_sample = False
    for index, window in enumerate(windows):
        start = _parse(window.get("start"))
        end = _parse(window.get("end"))
        if start is None or end is None or start >= end:
            classified.append(
                {
                    "start": str(window.get("start") or ""),
                    "end": str(window.get("end") or ""),
                    "out_of_sample": None,
                    "in_sample": False,
                    "overlaps": [],
                    "error": f"window {index} is not a valid interval",
                }
            )
            continue
        overlaps = [
            hit
            for hit in (
                *(_overlap(start, end, row) for row in training),
                *(_overlap(start, end, row) for row in selection),
            )
            if hit is not None
        ]
        if overlaps:
            in_sample = True
        classified.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "out_of_sample": (not overlaps) if verified else None,
                "in_sample": bool(overlaps),
                "overlaps": overlaps,
            }
        )
    if in_sample:
        out_of_sample: bool | None = False
    elif verified:
        out_of_sample = True
    else:
        out_of_sample = None
    return {
        "verified": verified,
        "training_windows": training,
        "selection_windows": selection,
        "windows": classified,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "note": _note(verified, in_sample),
    }


def _note(verified: bool, in_sample: bool) -> str:
    if in_sample:
        return (
            "at least one window overlaps the policy's training or checkpoint-"
            "selection data; such evidence is in-sample and never feeds suitability"
        )
    if not verified:
        return (
            "the policy artifact does not record its training window, so "
            "out-of-sample status could not be verified"
        )
    return "every window lies outside the policy's training and selection data"


def describe_overlaps(scope: Mapping[str, Any]) -> str:
    """A short human summary of the overlapping windows, for an error message."""
    parts: list[str] = []
    for window in scope.get("windows", ()):
        for hit in window.get("overlaps", ()):
            parts.append(
                f"{hit['split']} {hit['start']}..{hit['end']} overlaps the request "
                f"({hit['overlap_fraction']:.0%} of the window)"
            )
    return "; ".join(parts) or "no overlap detail was available"


__all__ = [
    "SELECTION_BASIS",
    "SELECTION_SPLIT",
    "TRAINING_BASIS",
    "TRAINING_SPLIT",
    "describe_overlaps",
    "reassessment_scope",
]
