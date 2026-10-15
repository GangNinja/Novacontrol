"""Metrics: what the numbers mean, what they do not, and how runs compare.

Four rules are enforced here rather than left to a report's prose:

* **A rate with no denominator is ``None``.** No held-out task evaluated is not
  "0% generalization", and an empty family is not "0% solved".
* **A family's metrics are always visible.** :func:`family_metrics` produces one
  :class:`FamilyMetrics` per family, and no aggregate is computed at all: this
  build has no comparable unit that would let a mean over grid exact-matches and
  planning outcomes mean anything. If a caller wants one, they must state the
  normalization and the weighting themselves (see :func:`weighted_aggregate`,
  which requires both as arguments).
* **Exact match and cell accuracy stay separate.** Cell accuracy is reported as a
  secondary number and is never converted into a solve.
* **A comparison states a delta, not a victory.** :func:`compare_results`
  reports per-task movement and refuses to compute a relative change when the
  baseline is zero, because "infinitely better" is not a measurement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.generalization.models import (
    Difficulty,
    FamilyMetrics,
    Split,
    TaskResult,
    TaskStatus,
)

__all__ = [
    "ComparisonReport",
    "TaskMovement",
    "compare_results",
    "family_metrics",
    "metrics_by_family",
    "weighted_aggregate",
]


def family_metrics(family_id: str, results: Sequence[TaskResult]) -> FamilyMetrics:
    """One family's measured numbers, denominators attached."""
    latencies: tuple[float, ...] = tuple(
        float(result.duration_ms) for result in results if result.duration_ms is not None
    )
    per_difficulty: dict[str, int] = {}
    failure_counts: dict[str, int] = {}
    cells_correct = 0
    cells_total = 0
    for result in results:
        per_difficulty[result.status.value] = per_difficulty.get(result.status.value, 0) + 1
        if result.failure is not None:
            failure_counts[result.failure.value] = failure_counts.get(result.failure.value, 0) + 1
        if result.cells_correct is not None and result.cells_total:
            cells_correct += result.cells_correct
            cells_total += result.cells_total
    held_out = [result for result in results if result.split is Split.HELD_OUT]
    return FamilyMetrics(
        family_id=family_id,
        tasks=len(results),
        solved=sum(1 for result in results if result.status is TaskStatus.SOLVED),
        failed=sum(1 for result in results if result.status is TaskStatus.FAILED),
        ambiguous=sum(1 for result in results if result.status is TaskStatus.AMBIGUOUS),
        unsupported=sum(1 for result in results if result.status is TaskStatus.UNSUPPORTED),
        invalid=sum(1 for result in results if result.status is TaskStatus.INVALID),
        budget_exhausted=sum(
            1 for result in results if result.status is TaskStatus.BUDGET_EXHAUSTED
        ),
        errors=sum(1 for result in results if result.status is TaskStatus.ERROR),
        held_out_tasks=len(held_out),
        held_out_solved=sum(1 for result in held_out if result.solved),
        cells_correct=cells_correct,
        cells_total=cells_total,
        candidates_examined=sum(result.candidates_examined for result in results),
        per_difficulty=per_difficulty,
        latencies_ms=latencies,
        failure_counts=failure_counts,
    )


def metrics_by_family(results: Sequence[TaskResult]) -> tuple[FamilyMetrics, ...]:
    """One :class:`FamilyMetrics` per family, in a deterministic family order."""
    families: list[str] = []
    for result in results:
        if result.family_id not in families:
            families.append(result.family_id)
    return tuple(
        family_metrics(family, [item for item in results if item.family_id == family])
        for family in sorted(families)
    )


def weighted_aggregate(
    metrics: Sequence[FamilyMetrics], *, weights: Mapping[str, float], normalised: bool
) -> dict[str, Any]:
    """An aggregate score, but only when the caller states its terms.

    ``normalised`` must be set deliberately and the weights must be supplied, so
    a combined number can never appear by accident. The returned payload carries
    the weights and the per-family inputs it used, because an aggregate whose
    inputs are not shown is a number nobody can check.
    """
    if not normalised:
        return {
            "score": None,
            "reason": (
                "no aggregate is computed: the families are measured in different units, "
                "and a combined score requires an explicit normalization and weighting"
            ),
            "weights": {},
            "inputs": [item.to_dict() for item in metrics],
        }
    total_weight = sum(weights.get(item.family_id, 1.0) for item in metrics) or 1.0
    score = 0.0
    counted = 0
    for item in metrics:
        rate = item.held_out_solve_rate if item.held_out_tasks else item.solve_rate
        if rate is None:
            continue
        score += rate * weights.get(item.family_id, 1.0)
        counted += 1
    if not counted:
        return {
            "score": None,
            "reason": "no family produced a measured rate, so there is nothing to combine",
            "weights": {str(k): float(v) for k, v in weights.items()},
            "inputs": [item.to_dict() for item in metrics],
        }
    return {
        "score": score / total_weight,
        "reason": (
            "a weighted mean of each family's held-out solve rate (its solve rate when no "
            "held-out task was evaluated), normalized by the total weight"
        ),
        "weights": {str(k): float(v) for k, v in weights.items()},
        "inputs": [item.to_dict() for item in metrics],
    }


@dataclass(frozen=True, slots=True)
class TaskMovement:
    """What happened to one task between two runs."""

    task_id: str
    family_id: str
    baseline_status: str
    candidate_status: str
    movement: str  # "improved" | "regressed" | "unchanged" | "appeared" | "disappeared"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "family_id": self.family_id,
            "baseline_status": self.baseline_status,
            "candidate_status": self.candidate_status,
            "movement": self.movement,
        }


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """A baseline and a candidate compared task by task, per family and overall.

    ``regressions`` are the tasks that were solved before and are not solved now:
    the list a candidate must not have. ``preserved`` records that the baseline
    run itself was not modified — a comparison that overwrote the baseline would
    make the next one meaningless.
    """

    baseline_id: str
    candidate_id: str
    baseline_solver: str = ""
    candidate_solver: str = ""
    seed: int = 0
    tasks_compared: int = 0
    improved: tuple[str, ...] = ()
    regressed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    appeared: tuple[str, ...] = ()
    disappeared: tuple[str, ...] = ()
    movements: tuple[TaskMovement, ...] = ()
    baseline_metrics: tuple[FamilyMetrics, ...] = ()
    candidate_metrics: tuple[FamilyMetrics, ...] = ()
    same_seed: bool = True
    same_task_set: bool = True
    baseline_preserved: bool = True
    notes: tuple[str, ...] = ()

    @property
    def improved_count(self) -> int:
        return len(self.improved)

    @property
    def regressed_count(self) -> int:
        return len(self.regressed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "candidate_id": self.candidate_id,
            "baseline_solver": self.baseline_solver,
            "candidate_solver": self.candidate_solver,
            "seed": self.seed,
            "tasks_compared": self.tasks_compared,
            "improved": list(self.improved),
            "regressed": list(self.regressed),
            "unchanged": list(self.unchanged),
            "appeared": list(self.appeared),
            "disappeared": list(self.disappeared),
            "regressed_count": self.regressed_count,
            "improved_count": self.improved_count,
            "movements": [item.to_dict() for item in self.movements],
            "baseline_metrics": [item.to_dict() for item in self.baseline_metrics],
            "candidate_metrics": [item.to_dict() for item in self.candidate_metrics],
            "same_seed": self.same_seed,
            "same_task_set": self.same_task_set,
            "baseline_preserved": self.baseline_preserved,
            "notes": list(self.notes),
        }


def compare_results(
    baseline: Iterable[TaskResult],
    candidate: Iterable[TaskResult],
    *,
    baseline_id: str,
    candidate_id: str,
    baseline_solver: str = "",
    candidate_solver: str = "",
    seed: int = 0,
    baseline_seed: int | None = None,
) -> ComparisonReport:
    """Compare two runs task by task. Never mutates either run's results.

    A task only counts as IMPROVED or REGRESSED when it appears in BOTH runs —
    a task that only one run evaluated is reported as appeared/disappeared, since
    an improvement over a task that was not evaluated is not an improvement.
    """
    baseline_by_id = {result.task_id: result for result in baseline}
    candidate_by_id = {result.task_id: result for result in candidate}
    shared = sorted(set(baseline_by_id) & set(candidate_by_id))
    movements: list[TaskMovement] = []
    improved: list[str] = []
    regressed: list[str] = []
    unchanged: list[str] = []
    for task_id in shared:
        before = baseline_by_id[task_id]
        after = candidate_by_id[task_id]
        if before.solved and not after.solved:
            movement = "regressed"
            regressed.append(task_id)
        elif after.solved and not before.solved:
            movement = "improved"
            improved.append(task_id)
        else:
            movement = "unchanged"
            unchanged.append(task_id)
        movements.append(
            TaskMovement(
                task_id=task_id,
                family_id=after.family_id,
                baseline_status=before.status.value,
                candidate_status=after.status.value,
                movement=movement,
            )
        )
    appeared = sorted(set(candidate_by_id) - set(baseline_by_id))
    disappeared = sorted(set(baseline_by_id) - set(candidate_by_id))
    notes: list[str] = []
    if appeared or disappeared:
        notes.append(
            "the two runs did not evaluate the same task set: "
            f"{len(appeared)} appeared, {len(disappeared)} disappeared; those tasks are "
            "reported separately rather than counted as movements"
        )
    if baseline_seed is not None and baseline_seed != seed:
        notes.append(
            f"the seeds differ (baseline {baseline_seed}, candidate {seed}), so a movement "
            "could come from the seed rather than from the change"
        )
    if regressed:
        notes.append(
            "regressions were detected: the candidate must not ship while a previously "
            "solved task is unsolved"
        )
    return ComparisonReport(
        baseline_id=baseline_id,
        candidate_id=candidate_id,
        baseline_solver=baseline_solver,
        candidate_solver=candidate_solver,
        seed=seed,
        tasks_compared=len(shared),
        improved=tuple(improved),
        regressed=tuple(regressed),
        unchanged=tuple(unchanged),
        appeared=tuple(appeared),
        disappeared=tuple(disappeared),
        movements=tuple(movements),
        baseline_metrics=metrics_by_family(list(baseline_by_id.values())),
        candidate_metrics=metrics_by_family(list(candidate_by_id.values())),
        same_seed=baseline_seed is None or baseline_seed == seed,
        same_task_set=not appeared and not disappeared,
        baseline_preserved=True,
        notes=tuple(notes),
    )


def difficulty_breakdown(results: Sequence[TaskResult]) -> dict[str, dict[str, int | float | None]]:
    """Solved/total per declared difficulty — where a weakness shows up first."""
    breakdown: dict[str, dict[str, int | float | None]] = {}
    for difficulty in Difficulty:
        selected = [result for result in results if result.split is Split.HELD_OUT] or list(results)
        chosen = [result for result in selected if _difficulty_of(result) == difficulty.value]
        if not chosen:
            continue
        solved = sum(1 for result in chosen if result.solved)
        breakdown[difficulty.value] = {
            "tasks": len(chosen),
            "solved": solved,
            "solve_rate": solved / len(chosen) if chosen else None,
        }
    return breakdown


def _difficulty_of(result: TaskResult) -> str:
    """The difficulty recorded on the task, carried through the result's tags."""
    for tag in result.evidence:
        if tag.startswith("difficulty="):
            return tag.split("=", 1)[1]
    return ""


@dataclass
class MetricTotals:
    """A small accumulator, used by the benchmark to total across families."""

    tasks: int = 0
    solved: int = 0
    failures: dict[str, int] = field(default_factory=dict)

    def add(self, results: Sequence[TaskResult]) -> None:
        for result in results:
            self.tasks += 1
            if result.solved:
                self.solved += 1
            if result.failure is not None:
                self.failures[result.failure.value] = self.failures.get(result.failure.value, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "solved": self.solved,
            "solve_rate": self.solved / self.tasks if self.tasks else None,
            "failures": dict(sorted(self.failures.items())),
        }
