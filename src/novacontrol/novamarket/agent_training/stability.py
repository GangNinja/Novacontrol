"""PI-15 training-stability monitoring beyond NaN and exploding loss.

A non-finite or exploding loss is caught inline in the training worker and is
already fatal.  This module covers the slower failures that never raise on their
own:

* the reward **collapsing** against its own recent history,
* the policy collapsing onto a **single action**,
* a spike of actions the environment **refused** as invalid.

Each signal carries a declared severity, so the worker's response is a stated
policy rather than an ad-hoc branch:

* ``fail``  — the run stops, keeps a failure checkpoint and its artifact is
  rejected (an agent whose actions are refused is not producing a usable
  policy);
* ``pause`` — the run writes a checkpoint and parks in ``paused``, where an
  operator can inspect the evidence and resume it deliberately.

The assessment is a pure function over the metrics the worker already computes,
so identical inputs always produce the identical decision and the thresholds are
published in the PI-15 contract instead of being tuned per run.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Severities, most severe last.
SEVERITIES = ("pause", "fail")
FAIL = "fail"
PAUSE = "pause"


@dataclass(frozen=True, slots=True)
class StabilityLimits:
    """Every threshold is a stated decision, published in the contract."""

    #: The recent window must fall below this fraction of the previous window.
    reward_collapse_ratio: float = 0.5
    #: Episodes per comparison window.
    reward_collapse_window: int = 2
    #: Episodes required before a collapse can be claimed at all.
    reward_collapse_min_episodes: int = 4
    #: The policy must have emitted at least this many actions...
    action_repetition_min_actions: int = 50
    #: ...and one action must own at least this share of them.
    action_repetition_frequency: float = 0.98
    #: A refused-action spike needs at least this many actions...
    invalid_action_min_actions: int = 20
    #: ...and refused actions must reach this share.
    invalid_action_frequency: float = 0.20

    def to_dict(self) -> dict[str, Any]:
        return {
            "reward_collapse_ratio": self.reward_collapse_ratio,
            "reward_collapse_window": self.reward_collapse_window,
            "reward_collapse_min_episodes": self.reward_collapse_min_episodes,
            "action_repetition_min_actions": self.action_repetition_min_actions,
            "action_repetition_frequency": self.action_repetition_frequency,
            "invalid_action_min_actions": self.invalid_action_min_actions,
            "invalid_action_frequency": self.invalid_action_frequency,
        }


DEFAULT_STABILITY_LIMITS = StabilityLimits()


@dataclass(frozen=True, slots=True)
class StabilitySignal:
    """One detected instability and the evidence that produced it."""

    code: str
    severity: str
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rewards(metrics: Mapping[str, Any]) -> list[float]:
    history = metrics.get("reward_history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return []
    values: list[float] = []
    for item in history:
        number = _number(item)
        if number is not None:
            values.append(number)
    return values


def _action_totals(metrics: Mapping[str, Any]) -> tuple[int, dict[str, int]]:
    distribution = metrics.get("action_distribution")
    if not isinstance(distribution, Mapping):
        return 0, {}
    counts = {
        str(name): int(value)
        for name, value in distribution.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    }
    return sum(counts.values()), counts


def reward_collapse_signal(
    rewards: Sequence[float], limits: StabilityLimits
) -> StabilitySignal | None:
    """A collapse is the recent window falling below half the earlier window."""
    window = max(1, int(limits.reward_collapse_window))
    needed = max(2 * window, int(limits.reward_collapse_min_episodes))
    if len(rewards) < needed:
        return None
    recent = statistics.fmean(rewards[-window:])
    baseline = statistics.fmean(rewards[-2 * window : -window])
    if baseline <= 0.0:
        # A non-positive baseline has no meaningful ratio; the signal stays silent
        # rather than inventing one from a sign change.
        return None
    ratio = recent / baseline
    if ratio >= limits.reward_collapse_ratio:
        return None
    return StabilitySignal(
        code="reward_collapse",
        severity=PAUSE,
        reason=(
            f"training reward collapsed to {ratio:.3f} of its previous window "
            f"({recent:.6f} vs {baseline:.6f}); the run paused at a checkpoint"
        ),
        evidence={
            "recent_mean": round(recent, 10),
            "baseline_mean": round(baseline, 10),
            "ratio": round(ratio, 10),
            "threshold": limits.reward_collapse_ratio,
            "window": window,
            "episodes": len(rewards),
        },
    )


def action_repetition_signal(
    total_actions: int, counts: Mapping[str, int], limits: StabilityLimits
) -> StabilitySignal | None:
    if total_actions < limits.action_repetition_min_actions or not counts:
        return None
    action = max(counts, key=lambda name: counts[name])
    frequency = counts[action] / total_actions
    if frequency < limits.action_repetition_frequency:
        return None
    return StabilitySignal(
        code="action_repetition",
        severity=PAUSE,
        reason=(
            f"the policy repeated action {action!r} for {frequency:.3f} of "
            f"{total_actions} actions; the run paused at a checkpoint"
        ),
        evidence={
            "action": action,
            "frequency": round(frequency, 10),
            "threshold": limits.action_repetition_frequency,
            "total_actions": total_actions,
            "minimum_actions": limits.action_repetition_min_actions,
        },
    )


def invalid_action_signal(
    total_actions: int, invalid_actions: int, limits: StabilityLimits
) -> StabilitySignal | None:
    if total_actions < limits.invalid_action_min_actions or invalid_actions <= 0:
        return None
    frequency = invalid_actions / total_actions
    if frequency < limits.invalid_action_frequency:
        return None
    return StabilitySignal(
        code="invalid_action_spike",
        severity=FAIL,
        reason=(
            f"{invalid_actions} of {total_actions} actions ({frequency:.3f}) were "
            "refused by the environment; an agent emitting refused actions is not a "
            "usable policy, so the run failed and its artifact was rejected"
        ),
        evidence={
            "invalid_actions": invalid_actions,
            "total_actions": total_actions,
            "frequency": round(frequency, 10),
            "threshold": limits.invalid_action_frequency,
            "minimum_actions": limits.invalid_action_min_actions,
        },
    )


def assess_stability(
    metrics: Mapping[str, Any],
    limits: StabilityLimits = DEFAULT_STABILITY_LIMITS,
) -> tuple[StabilitySignal, ...]:
    """Every stability signal the metrics of one training run justify."""
    if not isinstance(metrics, Mapping):
        return ()
    signals: list[StabilitySignal] = []
    collapse = reward_collapse_signal(_rewards(metrics), limits)
    if collapse is not None:
        signals.append(collapse)
    total_actions, counts = _action_totals(metrics)
    repetition = action_repetition_signal(total_actions, counts, limits)
    if repetition is not None:
        signals.append(repetition)
    invalid = _number(metrics.get("invalid_actions"))
    spike = invalid_action_signal(
        total_actions, max(0, int(invalid or 0)), limits
    )
    if spike is not None:
        signals.append(spike)
    return tuple(signals)


def stability_action(signals: Sequence[StabilitySignal]) -> str:
    """``fail`` outranks ``pause``; no signal means ``ok``."""
    severities = {signal.severity for signal in signals}
    if FAIL in severities:
        return FAIL
    if PAUSE in severities:
        return PAUSE
    return "ok"


def stability_record(
    metrics: Mapping[str, Any],
    limits: StabilityLimits = DEFAULT_STABILITY_LIMITS,
    *,
    checked_at: str = "",
) -> dict[str, Any]:
    """The decision plus its evidence, ready to persist in the run metrics."""
    signals = assess_stability(metrics, limits)
    action = stability_action(signals)
    return {
        "action": action,
        "codes": [signal.code for signal in signals],
        "signals": [signal.to_dict() for signal in signals],
        "limits": limits.to_dict(),
        "policy": {
            "fail": "stop the run, keep a failure checkpoint, reject the artifact",
            "pause": "write a checkpoint and park in 'paused' until an operator resumes",
            "ok": "continue",
        },
        "checked_at": checked_at,
        "simulated": True,
        "advisory_only": True,
        "production_authority": False,
    }


__all__ = [
    "DEFAULT_STABILITY_LIMITS",
    "FAIL",
    "PAUSE",
    "SEVERITIES",
    "StabilityLimits",
    "StabilitySignal",
    "action_repetition_signal",
    "assess_stability",
    "invalid_action_signal",
    "reward_collapse_signal",
    "stability_action",
    "stability_record",
]
