"""Phase 26: the contracts a generalization evaluation is written in.

Every record here exists so that ONE claim stays checkable: *this task was
solved by inference over examples, on an example the solver had not seen, and
here is the evidence*. The vocabulary is deliberately narrow:

* a :class:`EvaluationTask` states its own SUCCESS CONDITION and its split, and
  carries the examples a solver may look at plus the one it may not;
* a :class:`TaskResult` records what the solver actually produced, how it was
  verified, which failure category the miss belongs to (when it missed), and
  where the answer CAME FROM — a deterministic rule, a previously verified
  reusable rule, a freshly inferred hypothesis, or an optional model proposal;
* a :class:`LeakageReport` records whether the held-out split is genuinely
  held out, including the harder case the phase names: an example whose ANSWER
  (or an equivalent rule) was used while tuning is NOT unseen, however new its
  pixels are;
* an :class:`EvaluationRun` is the container, and it is persisted whole while
  its ROW form is trimmed — a run row is a summary, never the whole record.

Two rules are load-bearing and reappear throughout this package:

1. **A figure that was not measured is ``None``, never ``0.0``.** An accuracy
   nobody computed, a latency nobody timed and a resource nobody read are all
   ``None``; only a measured zero is a zero. (Phase 14's rule, kept.)
2. **Exact match is not cell accuracy.** Cell accuracy is a secondary metric of
   a WRONG answer; a task is solved only on an exact match (or on the family's
   own exact verifier), and the report never presents the first as the second.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from novacontrol.generalization.grids import Grid, InvalidGrid, parse_grid

__all__ = [
    "FAILURE_CATEGORIES",
    "PHASE",
    "RUN_SCHEMA_VERSION",
    "TASK_SCHEMA_VERSION",
    "Difficulty",
    "EvaluationRun",
    "EvaluationTask",
    "FamilyMetrics",
    "FailureCategory",
    "FailureRecord",
    "ImprovementRecommendation",
    "LeakageReport",
    "ProviderPosture",
    "RuleSource",
    "RunStatus",
    "Split",
    "TaskExample",
    "TaskKind",
    "TaskResult",
    "TaskStatus",
    "VerificationRecord",
    "validate_task",
]

PHASE = "26"

#: Bump when a persisted task record's shape changes.
TASK_SCHEMA_VERSION = "phase26.task.1"

#: Bump when a persisted run record's shape changes.
RUN_SCHEMA_VERSION = "phase26.run.1"


class Split(StrEnum):
    """Where an example sits relative to the solver's own tuning.

    ``TRAIN`` and ``DEV`` are the examples a solver may fit or tune on. A
    ``HELD_OUT`` example is the one that answers the only question this phase
    actually asks: does it work on something it has not seen?
    """

    TRAIN = "train"
    DEV = "dev"
    HELD_OUT = "held_out"


class Difficulty(StrEnum):
    """A declared difficulty, used for reporting and never for gating."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TaskKind(StrEnum):
    """What a task IS, which decides how it is solved and verified.

    The grid kind is the ARC-style one. The others are the families the phase
    asks for beyond grid puzzles: spatial relations over a parsed scene, a
    state transition on a structured state, a bounded planning problem, a tool
    choice, and a multi-step reasoning chain. Each has its own verifier — there
    is no generic "score" that could hide a family being broken.
    """

    GRID_TRANSFORM = "grid_transform"
    SPATIAL_RELATION = "spatial_relation"
    STATE_TRANSITION = "state_transition"
    SMALL_PLANNING = "small_planning"
    TOOL_SELECTION = "tool_selection"
    MULTI_STEP_REASONING = "multi_step_reasoning"


class TaskStatus(StrEnum):
    """How one task ended. Only ``SOLVED`` is a success.

    ``AMBIGUOUS`` is a first-class ending because the phase requires it: when
    two rules fit the examples, saying so is the honest answer and picking one
    silently is the defect. ``UNSUPPORTED`` is the other honest ending — the
    task names a transformation this build does not have.
    """

    SOLVED = "solved"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    ERROR = "error"


class FailureCategory(StrEnum):
    """Why a task failed, in categories a person can act on.

    The list is the phase's own, and it separates the causes that are easy to
    conflate: a solver that found no rule is not the same as one whose rule fit
    the examples and missed the test case, and neither is an infrastructure
    bug. ``DATA_LEAKAGE`` exists so a broken evaluation SETUP is reported as
    itself rather than counted as a model failure.
    """

    INPUT_PARSING_FAILURE = "input_parsing_failure"
    INVALID_TASK_DEFINITION = "invalid_task_definition"
    NO_CANDIDATE_RULE_FOUND = "no_candidate_rule_found"
    MULTIPLE_PLAUSIBLE_RULES = "multiple_plausible_rules"
    RULE_FITS_TRAIN_FAILS_TEST = "rule_fits_train_fails_test"
    TRANSFORMATION_DEFECT = "transformation_defect"
    VERIFICATION_FAILURE = "verification_failure"
    RESOURCE_BUDGET_EXHAUSTED = "resource_budget_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    DATA_LEAKAGE = "data_leakage"
    EVALUATION_INFRASTRUCTURE = "evaluation_infrastructure"


FAILURE_CATEGORIES: tuple[str, ...] = tuple(category.value for category in FailureCategory)


class RuleSource(StrEnum):
    """Where a solution CAME FROM — the phase's provenance requirement.

    ``REUSABLE_RULE`` is a rule this build had already verified on an earlier
    task; it is still a real generalization signal, but it is recorded
    separately from ``INFERRED`` because "it re-used what it learned" and "it
    worked it out from these examples" are different claims.
    """

    DETERMINISTIC = "deterministic"
    REUSABLE_RULE = "reusable_rule"
    INFERRED = "inferred"
    MODEL_PROPOSAL = "model_proposal"
    MOCK = "mock"
    NONE = "none"


class RunStatus(StrEnum):
    """The lifecycle of one evaluation run."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderPosture(StrEnum):
    """Whether a model could have taken part, and whether one did."""

    NOT_REQUIRED = "not_required"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """How one result was checked, in words a report can quote.

    ``verified`` is three-valued: ``None`` means the check could not be made
    (no expected output to compare against, or a family with no verifier) —
    which is NOT the same as a check that failed.
    """

    method: str
    description: str
    verified: bool | None = None
    detail: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "description": self.description,
            "verified": self.verified,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class TaskExample:
    """One input/answer pair a solver may look at — or must not.

    ``expected`` is ``None`` for an example whose answer is deliberately not
    supplied (the held-out input). ``seen`` records the solver's own history:
    an example whose answer was already used while tuning is marked seen even
    if its surface form is new, which is exactly the leakage the phase forbids
    us from calling unseen.
    """

    inputs: Mapping[str, Any]
    expected: Any = None
    label: str = ""
    seen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": _jsonable(self.inputs),
            "expected": _jsonable(self.expected),
            "label": self.label,
            "seen": self.seen,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TaskExample:
        return cls(
            inputs=dict(data.get("inputs") or {}),
            expected=data.get("expected"),
            label=str(data.get("label") or ""),
            seen=bool(data.get("seen", False)),
        )


@dataclass(frozen=True, slots=True)
class EvaluationTask:
    """One evaluation task: what it asks, what success is, and on what basis.

    ``examples`` are the pairs a solver may fit on. ``test_input`` is the one it
    is asked about, and ``expected_output`` is the answer — held by the
    EVALUATOR, not handed to the solver. A task whose success condition cannot
    be checked is refused at construction (see :func:`validate_task`), because a
    task nobody can verify turns into a low score with no explanation.
    """

    task_id: str
    kind: TaskKind
    family_id: str
    split: Split
    difficulty: Difficulty
    request: str
    success_condition: str
    examples: tuple[TaskExample, ...] = ()
    test_input: Mapping[str, Any] = field(default_factory=dict)
    expected_output: Any = None
    verifier: str = ""
    seed: int = 0
    tags: tuple[str, ...] = ()
    provenance: Mapping[str, str] = field(default_factory=dict)
    #: The transformation family this task exercises, when it declares one.
    #: Used for transfer reporting (the same rule on a new configuration) and
    #: never as an answer key: a solver is not told which family a task is in.
    rule_family: str = ""

    @property
    def train_examples(self) -> tuple[TaskExample, ...]:
        return tuple(example for example in self.examples if example.expected is not None)

    def to_dict(self, *, include_answer: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "family_id": self.family_id,
            "split": self.split.value,
            "difficulty": self.difficulty.value,
            "request": self.request,
            "success_condition": self.success_condition,
            "verifier": self.verifier,
            "seed": self.seed,
            "tags": list(self.tags),
            "provenance": {str(key): str(value) for key, value in dict(self.provenance).items()},
            "rule_family": self.rule_family,
            "examples": [example.to_dict() for example in self.examples],
            "test_input": _jsonable(self.test_input),
            "example_count": len(self.examples),
            "schema_version": TASK_SCHEMA_VERSION,
        }
        if include_answer:
            payload["expected_output"] = _jsonable(self.expected_output)
        return payload

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvaluationTask:
        raw_examples = data.get("examples") or ()
        examples = tuple(
            TaskExample.from_mapping(item) for item in raw_examples if isinstance(item, Mapping)
        )
        return cls(
            task_id=str(data.get("task_id") or ""),
            kind=TaskKind(str(data.get("kind") or TaskKind.GRID_TRANSFORM.value)),
            family_id=str(data.get("family_id") or ""),
            split=Split(str(data.get("split") or Split.TRAIN.value)),
            difficulty=Difficulty(str(data.get("difficulty") or Difficulty.EASY.value)),
            request=str(data.get("request") or ""),
            success_condition=str(data.get("success_condition") or ""),
            examples=examples,
            test_input=dict(data.get("test_input") or {}),
            expected_output=data.get("expected_output"),
            verifier=str(data.get("verifier") or ""),
            seed=int(data.get("seed") or 0),
            tags=tuple(str(tag) for tag in data.get("tags") or ()),
            provenance={
                str(key): str(value) for key, value in dict(data.get("provenance") or {}).items()
            },
            rule_family=str(data.get("rule_family") or ""),
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    """What one solver did on one task — the atom every metric counts.

    ``cells_correct`` and ``cells_total`` are recorded alongside the match so a
    cell-accuracy figure is a measurement rather than a re-derivation; when no
    comparison was possible both stay ``None``.
    """

    task_id: str
    family_id: str
    kind: TaskKind
    split: Split
    solver: str
    status: TaskStatus
    expected: Any = None
    predicted: Any = None
    candidate: str = ""
    source: RuleSource = RuleSource.NONE
    exact_match: bool | None = None
    cells_correct: int | None = None
    cells_total: int | None = None
    valid_solution: bool | None = None
    failure: FailureCategory | None = None
    failure_reason: str = ""
    verification: VerificationRecord | None = None
    candidates_examined: int = 0
    candidates_fitting: int = 0
    search_depth: int = 0
    duration_ms: float | None = None
    tokens: int | None = None
    evidence: tuple[str, ...] = ()
    seed: int = 0
    at: str = ""

    @property
    def cell_accuracy(self) -> float | None:
        """The secondary metric — ``None`` when no cell comparison was made.

        A task with no cell grid (a tool choice, a plan) has no cell accuracy
        at all, and reporting ``0.0`` for it would make an incomparable number
        look comparable.
        """
        if self.cells_correct is None or not self.cells_total:
            return None
        return self.cells_correct / self.cells_total

    @property
    def solved(self) -> bool:
        return self.status is TaskStatus.SOLVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "family_id": self.family_id,
            "kind": self.kind.value,
            "split": self.split.value,
            "solver": self.solver,
            "status": self.status.value,
            "expected": _jsonable(self.expected),
            "predicted": _jsonable(self.predicted),
            "candidate": self.candidate,
            "source": self.source.value,
            "exact_match": self.exact_match,
            "cell_accuracy": self.cell_accuracy,
            "cells_correct": self.cells_correct,
            "cells_total": self.cells_total,
            "valid_solution": self.valid_solution,
            "failure": self.failure.value if self.failure else None,
            "failure_reason": self.failure_reason,
            "verification": self.verification.to_dict() if self.verification else None,
            "candidates_examined": self.candidates_examined,
            "candidates_fitting": self.candidates_fitting,
            "search_depth": self.search_depth,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "evidence": list(self.evidence),
            "seed": self.seed,
            "at": self.at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TaskResult:
        failure = data.get("failure")
        return cls(
            task_id=str(data.get("task_id") or ""),
            family_id=str(data.get("family_id") or ""),
            kind=TaskKind(str(data.get("kind") or TaskKind.GRID_TRANSFORM.value)),
            split=Split(str(data.get("split") or Split.TRAIN.value)),
            solver=str(data.get("solver") or ""),
            status=TaskStatus(str(data.get("status") or TaskStatus.ERROR.value)),
            expected=data.get("expected"),
            predicted=data.get("predicted"),
            candidate=str(data.get("candidate") or ""),
            source=RuleSource(str(data.get("source") or RuleSource.NONE.value)),
            exact_match=data.get("exact_match"),
            cells_correct=data.get("cells_correct"),
            cells_total=data.get("cells_total"),
            valid_solution=data.get("valid_solution"),
            failure=FailureCategory(str(failure)) if failure else None,
            failure_reason=str(data.get("failure_reason") or ""),
            candidates_examined=int(data.get("candidates_examined") or 0),
            candidates_fitting=int(data.get("candidates_fitting") or 0),
            search_depth=int(data.get("search_depth") or 0),
            duration_ms=data.get("duration_ms"),
            tokens=data.get("tokens"),
            evidence=tuple(str(item) for item in data.get("evidence") or ()),
            seed=int(data.get("seed") or 0),
            at=str(data.get("at") or ""),
        )


@dataclass(frozen=True, slots=True)
class FamilyMetrics:
    """One family's measured numbers, with the denominators attached.

    Every rate is ``None`` when its denominator is zero: "no held-out task was
    evaluated" is not "0% generalization", and the report must be able to tell
    those apart.
    """

    family_id: str
    tasks: int = 0
    solved: int = 0
    failed: int = 0
    ambiguous: int = 0
    unsupported: int = 0
    invalid: int = 0
    budget_exhausted: int = 0
    errors: int = 0
    held_out_tasks: int = 0
    held_out_solved: int = 0
    cells_correct: int = 0
    cells_total: int = 0
    candidates_examined: int = 0
    per_difficulty: Mapping[str, int] = field(default_factory=dict)
    latencies_ms: tuple[float, ...] = ()
    failure_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def solve_rate(self) -> float | None:
        return self.solved / self.tasks if self.tasks else None

    @property
    def held_out_solve_rate(self) -> float | None:
        return self.held_out_solved / self.held_out_tasks if self.held_out_tasks else None

    @property
    def cell_accuracy(self) -> float | None:
        return self.cells_correct / self.cells_total if self.cells_total else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id,
            "tasks": self.tasks,
            "solved": self.solved,
            "failed": self.failed,
            "ambiguous": self.ambiguous,
            "unsupported": self.unsupported,
            "invalid": self.invalid,
            "budget_exhausted": self.budget_exhausted,
            "errors": self.errors,
            "solve_rate": self.solve_rate,
            "held_out_tasks": self.held_out_tasks,
            "held_out_solved": self.held_out_solved,
            "held_out_solve_rate": self.held_out_solve_rate,
            "cell_accuracy": self.cell_accuracy,
            "candidates_examined": self.candidates_examined,
            "per_difficulty": {str(k): int(v) for k, v in dict(self.per_difficulty).items()},
            "latency_ms": _distribution(self.latencies_ms),
            "failure_counts": {str(k): int(v) for k, v in dict(self.failure_counts).items()},
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> FamilyMetrics:
        return cls(
            family_id=str(data.get("family_id") or ""),
            tasks=int(data.get("tasks") or 0),
            solved=int(data.get("solved") or 0),
            failed=int(data.get("failed") or 0),
            ambiguous=int(data.get("ambiguous") or 0),
            unsupported=int(data.get("unsupported") or 0),
            invalid=int(data.get("invalid") or 0),
            budget_exhausted=int(data.get("budget_exhausted") or 0),
            errors=int(data.get("errors") or 0),
            held_out_tasks=int(data.get("held_out_tasks") or 0),
            held_out_solved=int(data.get("held_out_solved") or 0),
            cells_correct=int(data.get("cells_correct") or 0),
            cells_total=int(data.get("cells_total") or 0),
            candidates_examined=int(data.get("candidates_examined") or 0),
            per_difficulty={
                str(k): int(v) for k, v in dict(data.get("per_difficulty") or {}).items()
            },
            failure_counts={
                str(k): int(v) for k, v in dict(data.get("failure_counts") or {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """One classified failure, with the evidence needed to reproduce it."""

    task_id: str
    family_id: str
    category: FailureCategory
    reason: str
    expected: Any = None
    predicted: Any = None
    candidate: str = ""
    evidence: tuple[str, ...] = ()
    reproduce: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "family_id": self.family_id,
            "category": self.category.value,
            "reason": self.reason,
            "expected": _jsonable(self.expected),
            "predicted": _jsonable(self.predicted),
            "candidate": self.candidate,
            "evidence": list(self.evidence),
            "reproduce": _jsonable(self.reproduce),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> FailureRecord:
        return cls(
            task_id=str(data.get("task_id") or ""),
            family_id=str(data.get("family_id") or ""),
            category=FailureCategory(
                str(data.get("category") or FailureCategory.VERIFICATION_FAILURE.value)
            ),
            reason=str(data.get("reason") or ""),
            expected=data.get("expected"),
            predicted=data.get("predicted"),
            candidate=str(data.get("candidate") or ""),
            evidence=tuple(str(item) for item in data.get("evidence") or ()),
            reproduce=dict(data.get("reproduce") or {}),
        )


@dataclass(frozen=True, slots=True)
class ImprovementRecommendation:
    """A structured, non-executing recommendation derived from real failures.

    The phase forbids this from turning into training on its own: a
    recommendation names the recurring category, the tasks that produced it and
    the operator or family it implicates. Running a training phase from it is a
    different, explicitly authorized act.
    """

    category: FailureCategory
    occurrences: int
    tasks: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    recommendation: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "occurrences": self.occurrences,
            "tasks": list(self.tasks),
            "families": list(self.families),
            "recommendation": self.recommendation,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class LeakageReport:
    """Whether the held-out split is genuinely held out.

    Three checks, each recorded separately because they fail differently:
    ``overlapping_ids`` (a task in two splits), ``shared_answers`` (a held-out
    input whose answer — or an equivalent transformation — appears in the
    fitting examples) and ``used_answers`` (an example the solver's own
    tuning marked ``seen``). ``clean`` is ``None`` only when the check could
    not be run at all, never when it merely found nothing.
    """

    checked: bool = False
    clean: bool | None = None
    fitting_tasks: int = 0
    held_out_tasks: int = 0
    overlapping_ids: tuple[str, ...] = ()
    shared_answers: tuple[str, ...] = ()
    used_answers: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "clean": self.clean,
            "fitting_tasks": self.fitting_tasks,
            "held_out_tasks": self.held_out_tasks,
            "overlapping_ids": list(self.overlapping_ids),
            "shared_answers": list(self.shared_answers),
            "used_answers": list(self.used_answers),
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LeakageReport:
        return cls(
            checked=bool(data.get("checked", False)),
            clean=data.get("clean"),
            fitting_tasks=int(data.get("fitting_tasks") or 0),
            held_out_tasks=int(data.get("held_out_tasks") or 0),
            overlapping_ids=tuple(str(item) for item in data.get("overlapping_ids") or ()),
            shared_answers=tuple(str(item) for item in data.get("shared_answers") or ()),
            used_answers=tuple(str(item) for item in data.get("used_answers") or ()),
            reason=str(data.get("reason") or ""),
        )


#: The fields a run ROW carries. A run row is a summary on purpose: the results
#: and the per-task predictions live on the single-run view, exactly as the
#: embodied sessions list is trimmed to its lifecycle fields.
RUN_ROW_FIELDS: tuple[str, ...] = (
    "run_id",
    "status",
    "family_id",
    "solver",
    "baseline_id",
    "candidate_id",
    "seed",
    "tasks_evaluated",
    "solved",
    "held_out_tasks",
    "held_out_solved",
    "ambiguous",
    "unsupported",
    "failed",
    "duration_ms",
    "started_at",
    "finished_at",
    "provider",
)


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """One evaluation run: a baseline and a candidate on one held-out set.

    ``settings`` is the FROZEN configuration the run was made with, and it is
    what makes reproducibility a checkable claim rather than a hope: the same
    settings, the same tasks and the same seed must produce the same numbers.
    ``results`` is bounded (``max_results_kept``) — a run reports how many
    results it kept and never silently drops one from its denominators.
    """

    run_id: str
    status: RunStatus
    family_id: str = ""
    solver: str = ""
    baseline_id: str = ""
    candidate_id: str = ""
    seed: int = 0
    settings: Mapping[str, Any] = field(default_factory=dict)
    task_ids: tuple[str, ...] = ()
    #: The CANDIDATE's results. Every count and rate on the run is read from
    #: these, because the run's claim is about the candidate.
    results: tuple[TaskResult, ...] = ()
    #: The BASELINE's results over the same tasks, kept separately so a
    #: comparison can be made without the baseline leaking into the candidate's
    #: counts.
    baseline_results: tuple[TaskResult, ...] = ()
    results_kept: int = 0
    tasks_evaluated: int = 0
    metrics: tuple[FamilyMetrics, ...] = ()
    leakage: LeakageReport = field(default_factory=LeakageReport)
    failures: tuple[FailureRecord, ...] = ()
    provider: str = ProviderPosture.NOT_REQUIRED.value
    provider_detail: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float | None = None
    resource_use: Mapping[str, Any] = field(default_factory=dict)
    violations: tuple[str, ...] = ()
    reason: str = ""
    schema_version: str = RUN_SCHEMA_VERSION

    # -- reporting ---------------------------------------------------------
    @property
    def solved(self) -> int:
        return sum(1 for result in self.results if result.solved)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status is TaskStatus.FAILED)

    @property
    def ambiguous(self) -> int:
        return sum(1 for result in self.results if result.status is TaskStatus.AMBIGUOUS)

    @property
    def unsupported(self) -> int:
        return sum(1 for result in self.results if result.status is TaskStatus.UNSUPPORTED)

    @property
    def held_out_tasks(self) -> int:
        return sum(1 for result in self.results if result.split is Split.HELD_OUT)

    @property
    def held_out_solved(self) -> int:
        return sum(1 for result in self.results if result.split is Split.HELD_OUT and result.solved)

    def row(self) -> dict[str, Any]:
        """The TRIMMED row: lifecycle fields and counts, no result bodies."""
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "family_id": self.family_id,
            "solver": self.solver,
            "baseline_id": self.baseline_id,
            "candidate_id": self.candidate_id,
            "seed": self.seed,
            "tasks_evaluated": self.tasks_evaluated,
            "solved": self.solved,
            "held_out_tasks": self.held_out_tasks,
            "held_out_solved": self.held_out_solved,
            "ambiguous": self.ambiguous,
            "unsupported": self.unsupported,
            "failed": self.failed,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "provider": self.provider,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.row(),
            "reason": self.reason,
            "settings": _jsonable(self.settings),
            "task_ids": list(self.task_ids),
            "results_kept": self.results_kept,
            "metrics": [item.to_dict() for item in self.metrics],
            "leakage": self.leakage.to_dict(),
            "failures": [item.to_dict() for item in self.failures],
            "baseline_results": [result.to_dict() for result in self.baseline_results],
            "provider_detail": self.provider_detail,
            "resource_use": _jsonable(self.resource_use),
            "violations": list(self.violations),
            "results": [result.to_dict() for result in self.results],
            "schema_version": self.schema_version,
        }

    def with_(self, **changes: Any) -> EvaluationRun:
        return replace(self, **changes)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvaluationRun:
        raw_results = data.get("results") or ()
        raw_baseline = data.get("baseline_results") or ()
        raw_metrics = data.get("metrics") or ()
        raw_failures = data.get("failures") or ()
        return cls(
            run_id=str(data.get("run_id") or ""),
            status=RunStatus(str(data.get("status") or RunStatus.CREATED.value)),
            family_id=str(data.get("family_id") or ""),
            solver=str(data.get("solver") or ""),
            baseline_id=str(data.get("baseline_id") or ""),
            candidate_id=str(data.get("candidate_id") or ""),
            seed=int(data.get("seed") or 0),
            settings=dict(data.get("settings") or {}),
            task_ids=tuple(str(item) for item in data.get("task_ids") or ()),
            results=tuple(
                TaskResult.from_mapping(item) for item in raw_results if isinstance(item, Mapping)
            ),
            baseline_results=tuple(
                TaskResult.from_mapping(item) for item in raw_baseline if isinstance(item, Mapping)
            ),
            results_kept=int(data.get("results_kept") or 0),
            tasks_evaluated=int(data.get("tasks_evaluated") or 0),
            metrics=tuple(
                FamilyMetrics.from_mapping(item)
                for item in raw_metrics
                if isinstance(item, Mapping)
            ),
            leakage=LeakageReport.from_mapping(dict(data.get("leakage") or {})),
            failures=tuple(
                FailureRecord.from_mapping(item)
                for item in raw_failures
                if isinstance(item, Mapping)
            ),
            provider=str(data.get("provider") or ProviderPosture.NOT_REQUIRED.value),
            provider_detail=str(data.get("provider_detail") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            duration_ms=data.get("duration_ms"),
            resource_use=dict(data.get("resource_use") or {}),
            violations=tuple(str(item) for item in data.get("violations") or ()),
            reason=str(data.get("reason") or ""),
            schema_version=str(data.get("schema_version") or RUN_SCHEMA_VERSION),
        )


# --------------------------------------------------------------------------- #
# Validation — a task nobody can verify is refused, with the reason
# --------------------------------------------------------------------------- #

#: The verifiers this build actually implements. A task naming anything else is
#: refused at registration rather than scored as a miss later.
VERIFIERS: tuple[str, ...] = (
    "exact_grid_match",
    "exact_value_match",
    "state_equals",
    "relation_holds",
    "plan_reaches_goal",
    "tool_matches",
    "chain_matches",
)


def validate_task(payload: Mapping[str, Any]) -> EvaluationTask:
    """Build a task from a mapping, refusing anything unverifiable.

    Refusals are specific because the reason is the whole value of a refusal:
    no id, no request, no success condition, no held-out input, no expected
    output, an unknown verifier, an empty example set, an unreadable grid — each
    says which one it was. A task that arrives with an answer key for its
    held-out input but no `examples` would otherwise score as a perfect run.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("a task must be a mapping")
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("the task names no task_id")
    request = str(payload.get("request") or "").strip()
    if not request:
        raise ValueError(f"task {task_id!r} states no request, so nothing is being asked")
    condition = str(payload.get("success_condition") or "").strip()
    if not condition:
        raise ValueError(
            f"task {task_id!r} states no success_condition, so its result could never be "
            "verified; state what has to hold for the task to count as solved"
        )
    try:
        kind = TaskKind(str(payload.get("kind") or TaskKind.GRID_TRANSFORM.value))
    except ValueError as exc:
        raise ValueError(
            f"task {task_id!r} names kind {payload.get('kind')!r}, which this build does "
            f"not have ({exc})"
        ) from exc
    try:
        split = Split(str(payload.get("split") or Split.TRAIN.value))
    except ValueError as exc:
        raise ValueError(f"task {task_id!r} names an unknown split: {exc}") from exc
    try:
        difficulty = Difficulty(str(payload.get("difficulty") or Difficulty.EASY.value))
    except ValueError as exc:
        raise ValueError(f"task {task_id!r} names an unknown difficulty: {exc}") from exc
    verifier = str(payload.get("verifier") or _default_verifier(kind))
    if verifier not in VERIFIERS:
        raise ValueError(
            f"task {task_id!r} names verifier {verifier!r}; this build verifies "
            f"{', '.join(VERIFIERS)}"
        )
    raw_examples = payload.get("examples")
    if not isinstance(raw_examples, Sequence) or isinstance(raw_examples, (str, bytes)):
        raise ValueError(
            f"task {task_id!r} has no examples list; a task with nothing to infer from is a "
            "question, not an evaluation"
        )
    examples = tuple(
        TaskExample.from_mapping(item) for item in raw_examples if isinstance(item, Mapping)
    )
    if not examples:
        raise ValueError(f"task {task_id!r} has an empty examples list")
    test_input = payload.get("test_input")
    if not isinstance(test_input, Mapping) or not test_input:
        raise ValueError(f"task {task_id!r} names no test_input, so nothing is being asked")
    if "expected_output" not in payload:
        raise ValueError(
            f"task {task_id!r} carries no expected_output; a held-out task without an "
            "answer key cannot be scored, and guessing one would fake the whole run"
        )
    task = EvaluationTask(
        task_id=task_id,
        kind=kind,
        family_id=str(payload.get("family_id") or kind.value),
        split=split,
        difficulty=difficulty,
        request=request,
        success_condition=condition,
        examples=examples,
        test_input=dict(test_input),
        expected_output=payload.get("expected_output"),
        verifier=verifier,
        seed=int(payload.get("seed") or 0),
        tags=tuple(str(tag) for tag in payload.get("tags") or ()),
        provenance={
            str(key): str(value) for key, value in dict(payload.get("provenance") or {}).items()
        },
        rule_family=str(payload.get("rule_family") or ""),
    )
    _check_grid_payload(task)
    return task


def _default_verifier(kind: TaskKind) -> str:
    return {
        TaskKind.GRID_TRANSFORM: "exact_grid_match",
        TaskKind.SPATIAL_RELATION: "relation_holds",
        TaskKind.STATE_TRANSITION: "state_equals",
        TaskKind.SMALL_PLANNING: "plan_reaches_goal",
        TaskKind.TOOL_SELECTION: "tool_matches",
        TaskKind.MULTI_STEP_REASONING: "chain_matches",
    }[kind]


def _check_grid_payload(task: EvaluationTask) -> None:
    """Every grid a grid task mentions must actually parse.

    A non-rectangular or out-of-range grid is an INVALID task, not a solver
    failure, and it is caught here — at registration — so it can never enter a
    denominator as a miss.
    """
    if task.kind is not TaskKind.GRID_TRANSFORM:
        return
    for example in task.examples:
        if "grid" not in example.inputs:
            raise ValueError(
                f"task {task.task_id!r} is a grid task whose example carries no 'grid' input"
            )
        _grid_of(task, example.inputs)
    if "grid" not in task.test_input:
        raise ValueError(f"task {task.task_id!r} is a grid task whose test_input has no 'grid'")
    _grid_of(task, task.test_input)
    if task.expected_output is None:
        raise ValueError(
            f"task {task.task_id!r} is a grid task whose 'expected_output' is missing: "
            "a held-out task without an answer key cannot be scored"
        )
    try:
        parse_grid(task.expected_output)
    except InvalidGrid as exc:
        raise ValueError(f"task {task.task_id!r} has an unreadable expected_output: {exc}") from exc


def _grid_of(task: EvaluationTask, inputs: Mapping[str, Any]) -> Grid:
    try:
        return parse_grid(inputs.get("grid"))
    except InvalidGrid as exc:
        raise ValueError(f"task {task.task_id!r} has an unreadable grid: {exc}") from exc


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    """min/median/max/count over the values measured — all ``None`` when none were."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "min": None, "median": None, "max": None}
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": median,
        "max": ordered[-1],
    }


def _jsonable(value: Any) -> Any:
    """A JSON-safe copy — grids become row lists, tuples become lists."""
    if isinstance(value, Grid):
        return value.rows()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
