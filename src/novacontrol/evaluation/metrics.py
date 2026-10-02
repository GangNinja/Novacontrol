"""Aggregate metrics: the figures a person asks for, computed from stored rows.

Everything here is derived from rows that were ALREADY written — trajectories,
evaluations and rewards. Nothing is sampled, nothing is instrumented, and no
running total is kept in memory that a restart would lose: the numbers are a
function of the store, so they agree with what an operator can read there.

Two of the figures deserve their definition written down, because a percentage
whose definition is a guess is worse than no percentage:

  * **fast-path percentage** — runs whose decision route reached a direct, local
    path (a tool, the machine's own readings, a subsystem handler, the vision
    pipeline) and needed no model.
  * **LLM escalation percentage** — runs whose route needed a language model or
    the plan/agent loop (``local_llm``, ``cloud``, ``planner``, ``agent``).

Both are counted PER RUN, not per distinct route: ten runs through ``local_llm``
beside one through ``direct_tool`` are an escalation rate of ten elevenths, not
the half that counting the two route NAMES would report.

A route outside both sets (a chat answer, a clarification question) counts as
neither, so the two percentages do not silently claim to sum to one hundred.
Latency percentiles are computed over MEASURED runs only; an unmeasured run is
absent from the sample rather than present as a zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import (
    AgentTrajectory,
    EvaluationDimension,
    OverallStatus,
    QualityVerdictValue,
)
from novacontrol.evaluation.quality import QualityVerdict

#: Routes that reach a local path with no model involved.
FAST_PATH_ROUTES = frozenset({"direct_tool", "system_tools", "local_capability", "vision"})

#: Routes that needed a language model or the plan/agent loop.
MODEL_ROUTES = frozenset({"local_llm", "cloud", "planner", "agent"})


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """A percentile by linear interpolation (the usual definition for p50/p95).

    Returns ``None`` for an empty sample rather than 0.0, because "no
    measurements" and "measured at zero" are different facts.
    """
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(fraction))) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _dimension_mean(
    evaluations: Sequence[EvaluationResult], dimension: EvaluationDimension
) -> float | None:
    scores = [
        score
        for score in (result.score(dimension) for result in evaluations)
        if score is not None
    ]
    return _mean(scores)


def _verdict_of(trajectory: AgentTrajectory) -> str:
    quality = trajectory.quality
    if isinstance(quality, Mapping):
        return str(quality.get("verdict", ""))
    return ""


class MetricsCalculator:
    """Computes the aggregate figures from stored rows. Pure: reads, never writes."""

    def __init__(self, *, ok_threshold: float = 0.75) -> None:
        self.ok_threshold = float(ok_threshold)

    def compute(
        self,
        trajectories: Sequence[AgentTrajectory] = (),
        evaluations: Sequence[EvaluationResult] = (),
        rewards: Sequence[Any] = (),
        verdicts: Sequence[QualityVerdict] = (),
    ) -> dict[str, Any]:
        """Every figure, computed once, over the rows the caller passes in."""
        total = len(trajectories)
        decided = [row for row in trajectories if row.success is not None]
        succeeded = [row for row in decided if row.success]
        failed = [row for row in decided if row.success is False]

        verifications = [item for row in trajectories for item in row.verification_results]
        passed_verifications = [item for item in verifications if item.passed]
        recoveries = [item for row in trajectories for item in row.recovery_events]
        recovered = [item for item in recoveries if item.succeeded]

        gated = [row for row in trajectories if row.tool_calls]
        intervened = [
            row
            for row in gated
            if any(call.status in {"denied", "refused"} for call in row.tool_calls)
        ]
        tool_calls = [call for row in trajectories for call in row.tool_calls]
        succeeded_calls = [call for call in tool_calls if call.succeeded]
        attempted_calls = [call for call in tool_calls if call.succeeded or call.failed]

        latencies = [
            float(row.latency_metrics.total_ms)
            for row in trajectories
            if row.latency_metrics.total_ms is not None
        ]
        ram_deltas = [
            float(row.resource_usage.ram_delta_bytes)
            for row in trajectories
            if row.resource_usage.ram_delta_bytes is not None
        ]

        route_names = [
            str(row.decision.get("route", "")).strip().lower()
            for row in trajectories
            if row.decision
        ]
        routed = [name for name in route_names if name]
        fast_path_runs = sum(1 for name in routed if name in FAST_PATH_ROUTES)
        model_route_runs = sum(1 for name in routed if name in MODEL_ROUTES)

        retried = [row for row in trajectories if row.retries > 0]

        reward_totals = [
            float(getattr(result, "total_reward", 0.0))
            for result in rewards
            if getattr(result, "total_reward", None) is not None
        ]

        quality_counts = {member.value: 0 for member in QualityVerdictValue}
        sources = (
            [verdict.verdict for verdict in verdicts]
            if verdicts
            else [_verdict_of(row) for row in trajectories]
        )
        for verdict in sources:
            if verdict in quality_counts:
                quality_counts[verdict] += 1

        planning_scored = [
            result
            for result in evaluations
            if result.score(EvaluationDimension.PLANNING) is not None
        ]
        planning_ok = [
            result
            for result in planning_scored
            if (result.score(EvaluationDimension.PLANNING) or 0.0) >= self.ok_threshold
        ]
        route_counts = {
            route: sum(1 for name in routed if name == route)
            for route in sorted(set(routed))
        }

        return {
            # -- counts ----------------------------------------------------------
            "trajectories": total,
            "completed": len(succeeded),
            "failed": len(failed),
            "undecided": total - len(decided),
            "evaluations": len(evaluations),
            "rewards": len(rewards),
            "tool_calls": len(tool_calls),
            "verifications": len(verifications),
            "recoveries": len(recoveries),
            # -- rates -----------------------------------------------------------
            "task_success_rate": _rate(len(succeeded), len(decided)),
            "failure_rate": _rate(len(failed), len(decided)),
            "verification_success_rate": _rate(
                len(passed_verifications), len(verifications)
            ),
            "tool_selection_accuracy": _dimension_mean(
                evaluations, EvaluationDimension.TOOL_SELECTION
            ),
            "tool_call_success_rate": _rate(len(succeeded_calls), len(attempted_calls)),
            "planning_success_rate": _rate(len(planning_ok), len(planning_scored)),
            "recovery_success_rate": _rate(len(recovered), len(recoveries)),
            "safety_intervention_rate": _rate(len(intervened), len(gated)),
            "retry_rate": _rate(len(retried), total),
            "average_reward": _mean(reward_totals),
            # -- latency ---------------------------------------------------------
            "average_latency_ms": _mean(latencies),
            "p50_latency_ms": percentile(latencies, 0.5),
            "p95_latency_ms": percentile(latencies, 0.95),
            "measured_latency_runs": len(latencies),
            # -- routing ---------------------------------------------------------
            "fast_path_percentage": _rate(fast_path_runs, len(routed)),
            "llm_escalation_percentage": _rate(model_route_runs, len(routed)),
            "routes": route_counts,
            # -- resources -------------------------------------------------------
            "average_ram_delta_bytes": _mean(ram_deltas),
            "measured_resource_runs": len(ram_deltas),
            # -- quality ---------------------------------------------------------
            "quality": {
                **quality_counts,
                "considered": sum(quality_counts.values()),
            },
            # -- dimension means -------------------------------------------------
            "dimension_scores": {
                member.value: _dimension_mean(evaluations, member)
                for member in EvaluationDimension
            },
            "overall_status": {
                status.value: sum(
                    1 for result in evaluations if result.overall_status == status.value
                )
                for status in OverallStatus
            },
        }


def quality_counts(verdicts: Iterable[QualityVerdict]) -> dict[str, int]:
    """How many rows fell into each quality verdict (for a status surface)."""
    counts = {member.value: 0 for member in QualityVerdictValue}
    for verdict in verdicts:
        if verdict.verdict in counts:
            counts[verdict.verdict] += 1
    return counts


__all__ = [
    "FAST_PATH_ROUTES",
    "MODEL_ROUTES",
    "MetricsCalculator",
    "percentile",
    "quality_counts",
]
