"""Evaluating one task: solve it, verify the answer, classify the miss.

This module is where the answer key lives. The solver never sees it — it is
passed to the verifier only AFTER the solver has produced its answer, and that
ordering is the whole point of the phase: a solver that could read the expected
output would make every number in the report a restatement of its own input.

Per verifier, the check is the family's own notion of success:

======================  ==========================================================
verifier                 what it checks
======================  ==========================================================
``exact_grid_match``     the produced grid equals the expected grid, cell for cell
``exact_value_match``    exact equality of a scalar answer
``state_equals``         key/value equality of two state mappings
``relation_holds``       the measured boolean relation equals the expected one
``plan_reaches_goal``    a claimed path is RE-SIMULATED (bounds, walls, goal); a
                         claim that no path exists is checked against the task's
                         hand-written answer key
``tool_matches``         the chosen tool id equals the expected id
``chain_matches``        the produced term equals the expected term
======================  ==========================================================

Cell accuracy is measured here as a SECONDARY number: it is the fraction of the
expected grid's cells the answer got right, and it never turns a wrong answer
into a solved task.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.generalization.failures import classify_failure
from novacontrol.generalization.grids import Grid, InvalidGrid, parse_grid
from novacontrol.generalization.models import (
    EvaluationTask,
    TaskKind,
    TaskResult,
    TaskStatus,
    VerificationRecord,
)
from novacontrol.generalization.solvers import Budget, SolveOutcome, solve

__all__ = [
    "EvaluationClock",
    "evaluate_task",
    "verify",
]


def verify(task: EvaluationTask, predicted: Any) -> VerificationRecord:
    """Check one answer against the task's expected output, by its own verifier."""
    verifier = task.verifier or "exact_value_match"
    expected = task.expected_output
    if verifier == "exact_grid_match":
        if predicted is None:
            return VerificationRecord(
                method=verifier,
                description="the produced grid must equal the expected grid exactly",
                verified=False,
                detail="no grid was produced",
            )
        try:
            produced = parse_grid(predicted)
        except InvalidGrid as exc:
            return VerificationRecord(
                method=verifier,
                description="the produced grid must equal the expected grid exactly",
                verified=False,
                detail=f"the answer is not a grid: {exc}",
            )
        expected_grid = parse_grid(expected)
        match = produced.cells == expected_grid.cells
        return VerificationRecord(
            method=verifier,
            description="the produced grid must equal the expected grid exactly",
            verified=match,
            detail=(
                "exact match"
                if match
                else f"expected {expected_grid.height}×{expected_grid.width}, "
                f"got {produced.height}×{produced.width}"
            ),
            evidence=(
                f"expected={expected_grid.render().replace(chr(10), '/')}",
                f"produced={produced.render().replace(chr(10), '/')}",
            ),
        )
    if verifier == "relation_holds":
        if not isinstance(predicted, bool):
            return VerificationRecord(
                method=verifier,
                description="the measured relation must equal the expected answer",
                verified=False,
                detail=f"the answer is {predicted!r}, not a measured yes/no",
            )
        match = bool(predicted) == bool(expected)
        return VerificationRecord(
            method=verifier,
            description="the measured relation must equal the expected answer",
            verified=match,
            detail=f"measured {predicted}, expected {expected}",
        )
    if verifier == "state_equals":
        if not isinstance(predicted, Mapping):
            return VerificationRecord(
                method=verifier,
                description="the predicted state must equal the expected state exactly",
                verified=False,
                detail=f"the answer is {predicted!r}, not a state mapping",
            )
        match = dict(predicted) == dict(expected)
        differing = sorted(
            key
            for key in set(dict(predicted)) | set(dict(expected))
            if dict(predicted).get(key) != dict(expected).get(key)
        )
        return VerificationRecord(
            method=verifier,
            description="the predicted state must equal the expected state exactly",
            verified=match,
            detail="exact state match" if match else f"fields differ: {differing}",
            evidence=(f"expected={dict(expected)}", f"produced={dict(predicted)}"),
        )
    if verifier == "plan_reaches_goal":
        return _verify_plan(task, predicted)
    if verifier == "tool_matches":
        match = isinstance(predicted, str) and predicted.strip() == str(expected).strip()
        return VerificationRecord(
            method=verifier,
            description="the chosen tool must be the expected tool",
            verified=match,
            detail=f"chose {predicted!r}, expected {expected!r}",
        )
    if verifier == "chain_matches":
        match = predicted == expected
        if not match and isinstance(predicted, (int, float)) and isinstance(expected, (int, float)):
            match = float(predicted) == float(expected)
        return VerificationRecord(
            method=verifier,
            description="the produced term must equal the expected term",
            verified=match,
            detail=f"produced {predicted!r}, expected {expected!r}",
        )
    return VerificationRecord(
        method=verifier,
        description="an unknown verifier cannot check anything",
        verified=None,
        detail=(
            f"verifier {verifier!r} is not implemented in this build, so the answer was "
            "NOT checked (which is not the same as a wrong answer)"
        ),
    )


def _verify_plan(task: EvaluationTask, predicted: Any) -> VerificationRecord:
    """Re-simulate a claimed path; check a no-path claim against the answer key."""
    expected = dict(task.expected_output or {})
    wants_reach = bool(expected.get("reaches_goal"))
    if not isinstance(predicted, Mapping):
        return VerificationRecord(
            method="plan_reaches_goal",
            description="a plan must reach the goal without crossing a wall",
            verified=False,
            detail=f"the answer is {predicted!r}, not a plan",
        )
    path = predicted.get("path")
    if path is None:
        match = not wants_reach
        return VerificationRecord(
            method="plan_reaches_goal",
            description=(
                "a claimed path is re-simulated; a claim that no path exists is checked "
                "against the task's hand-written answer key"
            ),
            verified=match,
            detail=(
                "no path claimed, and the task says none exists"
                if match
                else "no path claimed, but the task's answer key says one exists"
            ),
        )
    try:
        cells = [(int(item[0]), int(item[1])) for item in path]
    except (TypeError, ValueError, IndexError):
        return VerificationRecord(
            method="plan_reaches_goal",
            description="a plan must be a list of [row, column] cells",
            verified=False,
            detail=f"the path {path!r} is not a list of cells",
        )
    inputs = task.test_input
    width = int(inputs.get("width") or 0)
    height = int(inputs.get("height") or 0)
    walls = {tuple(int(value) for value in item) for item in inputs.get("walls") or ()}
    start = tuple(int(value) for value in inputs.get("start") or ())
    goal = tuple(int(value) for value in inputs.get("goal") or ())
    limits = dict(inputs.get("limits") or {})
    max_steps = int(limits.get("max_steps") or 40)
    problems: list[str] = []
    if not cells or cells[0] != start:
        problems.append(f"the path does not start at {start}")
    for index in range(1, len(cells)):
        previous, current = cells[index - 1], cells[index]
        if abs(previous[0] - current[0]) + abs(previous[1] - current[1]) != 1:
            problems.append(f"step {index} is not a single orthogonal move")
            break
    for index, cell in enumerate(cells):
        if not (0 <= cell[0] < height and 0 <= cell[1] < width):
            problems.append(f"step {index} leaves the {height}×{width} grid at {cell}")
            break
        if cell in walls:
            problems.append(f"step {index} walks through the wall at {cell}")
            break
    if len(cells) - 1 > max_steps:
        problems.append(f"the path is {len(cells) - 1} steps, over the {max_steps} allowed")
    if cells and cells[-1] != goal:
        problems.append(f"the path ends at {cells[-1]}, not at the goal {goal}")
    if problems:
        return VerificationRecord(
            method="plan_reaches_goal",
            description="the plan is re-simulated: bounds, walls, single moves, then the goal",
            verified=False,
            detail="; ".join(problems),
            evidence=(f"path={cells[:12]}",),
        )
    if not wants_reach:
        return VerificationRecord(
            method="plan_reaches_goal",
            description="the plan is re-simulated: bounds, walls, single moves, then the goal",
            verified=False,
            detail="a valid path was produced where the task's answer key says none exists",
            evidence=(f"path={cells[:12]}",),
        )
    return VerificationRecord(
        method="plan_reaches_goal",
        description="the plan is re-simulated: bounds, walls, single moves, then the goal",
        verified=True,
        detail=f"the path reaches the goal in {len(cells) - 1} steps",
        evidence=(f"path={cells[:12]}",),
    )


@dataclass(frozen=True, slots=True)
class EvaluationClock:
    """Wall-clock measurement for one evaluation, kept out of the scoring."""

    started: float

    @classmethod
    def start(cls) -> EvaluationClock:
        return cls(started=time.monotonic())

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000.0


def evaluate_task(
    task: EvaluationTask,
    *,
    solver: str = "infer",
    budget: Budget | None = None,
    clock: EvaluationClock | None = None,
) -> TaskResult:
    """Solve one task, verify the answer, and record the outcome.

    The order matters and is enforced here: ``solve`` runs first with the
    examples only, ``verify`` runs second with the answer key. The result carries
    the solver's own ending AND, when the answer was rejected, a classified
    failure — so "produced an answer" can never be read as "solved".
    """
    limits = budget or Budget()
    timer = clock or EvaluationClock.start()
    outcome: SolveOutcome = solve(task, solver=solver, budget=limits)
    verification: VerificationRecord | None = None
    status = outcome.status
    if status is TaskStatus.SOLVED:
        verification = verify(task, outcome.predicted)
        if verification.verified is not True:
            status = TaskStatus.FAILED
    cells_correct, cells_total = _cell_comparison(task, outcome.predicted)
    valid_solution = _valid_solution(task, outcome.predicted)
    category, reason = classify_failure(
        status=status,
        source=outcome.source,
        verified=None if verification is None else verification.verified,
        failure=outcome.failure,
        reason=outcome.failure_reason,
        task_kind=task.kind,
    )
    evidence = list(outcome.evidence)
    evidence.append(f"difficulty={task.difficulty.value}")
    evidence.append(f"split={task.split.value}")
    if verification is not None:
        evidence.extend(verification.evidence)
    return TaskResult(
        task_id=task.task_id,
        family_id=task.family_id,
        kind=task.kind,
        split=task.split,
        solver=solver,
        status=status,
        expected=task.expected_output,
        predicted=outcome.predicted,
        candidate=outcome.candidate,
        source=outcome.source,
        # Three-valued, like every other figure here: an answer that was
        # produced and then REJECTED is ``False``; an ending that produced no
        # answer at all (ambiguous, unsupported, budget spent) is ``None``,
        # because "we compared and it did not match" is a different claim from
        # "there was nothing to compare" — and a report that flattened the two
        # would count a refusal as a miss.
        exact_match=(
            True
            if status is TaskStatus.SOLVED
            else (False if outcome.predicted is not None else None)
        ),
        cells_correct=cells_correct,
        cells_total=cells_total,
        valid_solution=valid_solution,
        failure=category,
        failure_reason=reason,
        verification=verification,
        candidates_examined=outcome.candidates_examined,
        candidates_fitting=outcome.candidates_fitting,
        search_depth=outcome.search_depth,
        duration_ms=timer.elapsed_ms(),
        tokens=None,  # no model took part, so no token figure exists to report
        evidence=tuple(evidence),
        seed=task.seed,
        at=_now(),
    )


def _cell_comparison(task: EvaluationTask, predicted: Any) -> tuple[int | None, int | None]:
    """Cells right out of the expected grid's cells — a SECONDARY metric.

    A task with no grid (a tool choice, a plan, a state) has no cell figure at
    all, and reports ``None`` rather than a zero that would be averaged into
    something else.
    """
    if task.kind is not TaskKind.GRID_TRANSFORM:
        return None, None
    try:
        expected = parse_grid(task.expected_output)
    except InvalidGrid:
        return None, None
    total = expected.size
    if predicted is None:
        return 0, total
    try:
        produced = parse_grid(predicted)
    except InvalidGrid:
        return 0, total
    if produced.shape != expected.shape:
        return 0, total
    correct = sum(
        1
        for row in range(expected.height)
        for column in range(expected.width)
        if produced.at(row, column) == expected.at(row, column)
    )
    return correct, total


def _valid_solution(task: EvaluationTask, predicted: Any) -> bool | None:
    """Whether the answer is a well-formed answer for its family at all."""
    if predicted is None:
        return False
    if task.kind is TaskKind.GRID_TRANSFORM:
        try:
            parse_grid(predicted)
        except InvalidGrid:
            return False
        return True
    if task.kind is TaskKind.SMALL_PLANNING:
        return isinstance(predicted, Mapping)
    if task.kind is TaskKind.STATE_TRANSITION:
        return isinstance(predicted, Mapping)
    if task.kind is TaskKind.SPATIAL_RELATION:
        return isinstance(predicted, bool)
    return isinstance(predicted, (str, int, float))


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def grid_of(value: Sequence[Any] | Grid) -> Grid:
    """Parse a grid or raise — used by the corpus integrity checks."""
    return parse_grid(value)
