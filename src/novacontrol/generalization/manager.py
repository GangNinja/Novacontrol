"""The evaluation manager: the one place a generalized result is produced.

It owns five things, and each of them exists because a benchmark without it
lies by accident:

* **The task registry** — built-in tasks plus any an operator registers, with
  the split declared per task and never inferred from a filename.
* **The leakage check** — run before every evaluation, because a held-out task
  whose answer was already available turns a generalization number into a
  memorization number. It checks three ways (see :meth:`leakage_report`), and a
  run whose splits are dirty is REFUSED rather than annotated.
* **The runs** — a baseline and a candidate over the SAME tasks, with the frozen
  settings, the seed and the per-task results, persisted whole and listed
  trimmed.
* **The comparison** — per-task movement between two stored runs, where a
  regression is named and the baseline run is never modified.
* **The failure feed** — grouped failures and non-executing recommendations,
  which is how findings reach a person without a training run starting itself.

Nothing here trains anything. A stored run carries results, not weights, and
:meth:`export` produces a payload for review rather than a job for a scheduler.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from novacontrol.generalization.evaluation import EvaluationClock, evaluate_task
from novacontrol.generalization.failures import FAILURE_ADVICE, group_failures
from novacontrol.generalization.metrics import (
    ComparisonReport,
    compare_results,
    metrics_by_family,
)
from novacontrol.generalization.models import (
    RUN_SCHEMA_VERSION,
    EvaluationRun,
    EvaluationTask,
    FailureCategory,
    FailureRecord,
    ImprovementRecommendation,
    LeakageReport,
    ProviderPosture,
    RunStatus,
    Split,
    TaskKind,
    TaskResult,
    TaskStatus,
    validate_task,
)
from novacontrol.generalization.rules import MAX_DEPTH
from novacontrol.generalization.solvers import BASELINES, SOLVERS, Budget
from novacontrol.generalization.tasks import (
    BUILTIN_TASKS,
    CORPUS_VERSION,
    GRID_RULES_SUPPORTED,
)
from novacontrol.generalization.transforms import (
    OPERATOR_NAMES,
    operator_catalogue,
)

__all__ = [
    "FAMILIES",
    "HARD_MAX_CANDIDATES",
    "HARD_MAX_RESULTS_KEPT",
    "HARD_MAX_RUNS",
    "HARD_MAX_SECONDS",
    "HARD_MAX_TASKS_PER_RUN",
    "STORE_KEY",
    "FamilyDefinition",
    "GeneralizationManager",
    "GeneralizationSettingsView",
    "builtin_task_ids",
]

#: Persistence key. One key holds the runs, bounded by ``keep_runs``.
STORE_KEY = "generalization_runs"

#: Hard ceilings a configuration cannot exceed, whatever it asks for. The phase
#: targets a 16 GB machine, so an unbounded benchmark is a bug rather than a
#: setting.
HARD_MAX_TASKS_PER_RUN = 200
HARD_MAX_CANDIDATES = 4000
HARD_MAX_SECONDS = 300.0
HARD_MAX_RESULTS_KEPT = 400
HARD_MAX_RUNS = 50
HARD_MAX_TASKS_REGISTERED = 500


@dataclass(frozen=True, slots=True)
class FamilyDefinition:
    """One task family, as the report and the API describe it."""

    family_id: str
    kind: TaskKind
    description: str
    success_condition: str
    verifier: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id,
            "kind": self.kind.value,
            "description": self.description,
            "success_condition": self.success_condition,
            "verifier": self.verifier,
            "source": self.source,
        }


#: The six families. A single aggregate score is deliberately NOT one of them.
FAMILIES: tuple[FamilyDefinition, ...] = (
    FamilyDefinition(
        family_id="grid_transform",
        kind=TaskKind.GRID_TRANSFORM,
        description=(
            "ARC-style grid tasks: infer a transformation from two or three input/output "
            "examples and apply it to an unseen input."
        ),
        success_condition="the produced grid equals the expected grid exactly",
        verifier="exact_grid_match",
        source="built-in corpus, solved by the bounded operator library",
    ),
    FamilyDefinition(
        family_id="spatial_relation",
        kind=TaskKind.SPATIAL_RELATION,
        description=(
            "Geometry over a parsed scene: is one shape left of, above, touching, inside "
            "or larger than another?"
        ),
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        source="built-in corpus, solved by connected components and geometry",
    ),
    FamilyDefinition(
        family_id="state_transition",
        kind=TaskKind.STATE_TRANSITION,
        description=(
            "Predict the next structured state from worked transitions, inferring one rule "
            "per field."
        ),
        success_condition="the predicted state equals the expected state exactly",
        verifier="state_equals",
        source="built-in corpus, solved by per-field transition inference",
    ),
    FamilyDefinition(
        family_id="small_planning",
        kind=TaskKind.SMALL_PLANNING,
        description="Bounded grid planning: route to a goal, or prove no route exists.",
        success_condition="the proposed path reaches the goal without crossing a wall",
        verifier="plan_reaches_goal",
        source="built-in corpus, solved by bounded breadth-first search",
    ),
    FamilyDefinition(
        family_id="tool_selection",
        kind=TaskKind.TOOL_SELECTION,
        description="Choose a tool for a request, learned from labelled examples.",
        success_condition="the chosen tool is the expected one",
        verifier="tool_matches",
        source="built-in corpus, solved by example-weighted token overlap",
    ),
    FamilyDefinition(
        family_id="multi_step_reasoning",
        kind=TaskKind.MULTI_STEP_REASONING,
        description="Continue a sequence whose rule is never stated, only shown.",
        success_condition="the next term equals the expected term",
        verifier="chain_matches",
        source="built-in corpus, solved by inferring a constant step or ratio",
    ),
)


@dataclass(frozen=True, slots=True)
class GeneralizationSettingsView:
    """The manager's limits, resolved and clamped to hard bounds."""

    enabled: bool = True
    persist: bool = True
    default_family: str = ""
    default_solver: str = "infer"
    default_baseline: str = "identity"
    evaluate_held_out_only: bool = True
    max_tasks_per_run: int = 60
    max_candidates: int = 400
    max_depth: int = MAX_DEPTH
    max_seconds: float = 60.0
    max_results_kept: int = 200
    max_model_calls: int = 0
    require_clean_splits: bool = True
    keep_runs: int = 20
    max_registered_tasks: int = 100

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> GeneralizationSettingsView:
        raw = dict(data or {})
        defaults = cls()
        return cls(
            enabled=_bool(raw.get("enabled"), defaults.enabled),
            persist=_bool(raw.get("persist"), defaults.persist),
            default_family=str(raw.get("default_family") or defaults.default_family),
            default_solver=str(raw.get("default_solver") or defaults.default_solver),
            default_baseline=str(raw.get("default_baseline") or defaults.default_baseline),
            evaluate_held_out_only=_bool(
                raw.get("evaluate_held_out_only"), defaults.evaluate_held_out_only
            ),
            max_tasks_per_run=_count(
                raw.get("max_tasks_per_run"),
                defaults.max_tasks_per_run,
                HARD_MAX_TASKS_PER_RUN,
            ),
            max_candidates=_count(
                raw.get("max_candidates"), defaults.max_candidates, HARD_MAX_CANDIDATES
            ),
            max_depth=_count(raw.get("max_depth"), defaults.max_depth, MAX_DEPTH),
            max_seconds=_seconds(raw.get("max_seconds"), defaults.max_seconds, HARD_MAX_SECONDS),
            max_results_kept=_count(
                raw.get("max_results_kept"),
                defaults.max_results_kept,
                HARD_MAX_RESULTS_KEPT,
            ),
            max_model_calls=_count(raw.get("max_model_calls"), defaults.max_model_calls, 8),
            require_clean_splits=_bool(
                raw.get("require_clean_splits"), defaults.require_clean_splits
            ),
            keep_runs=_count(raw.get("keep_runs"), defaults.keep_runs, HARD_MAX_RUNS),
            max_registered_tasks=_count(
                raw.get("max_registered_tasks"),
                defaults.max_registered_tasks,
                HARD_MAX_TASKS_REGISTERED,
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "persist": self.persist,
            "default_family": self.default_family,
            "default_solver": self.default_solver,
            "default_baseline": self.default_baseline,
            "evaluate_held_out_only": self.evaluate_held_out_only,
            "max_tasks_per_run": self.max_tasks_per_run,
            "max_candidates": self.max_candidates,
            "max_depth": self.max_depth,
            "max_seconds": self.max_seconds,
            "max_results_kept": self.max_results_kept,
            "max_model_calls": self.max_model_calls,
            "require_clean_splits": self.require_clean_splits,
            "keep_runs": self.keep_runs,
            "max_registered_tasks": self.max_registered_tasks,
        }


class GeneralizationManager:
    """Runs evaluations, keeps the records, and refuses to overstate them."""

    def __init__(
        self,
        *,
        settings: GeneralizationSettingsView | Mapping[str, Any] | None = None,
        store: Any | None = None,
        observer: Callable[[str, Mapping[str, Any]], None] | None = None,
        provider: Any | None = None,
        provider_detail: str = "",
    ) -> None:
        self.settings = (
            settings
            if isinstance(settings, GeneralizationSettingsView)
            else GeneralizationSettingsView.from_mapping(settings)
        )
        self._store = store
        self._observer = observer
        self._provider = provider
        self._provider_detail = provider_detail
        self._tasks: dict[str, EvaluationTask] = {task.task_id: task for task in BUILTIN_TASKS}
        self._builtin_ids = frozenset(self._tasks)
        self._runs: dict[str, EvaluationRun] = {}
        self._order: list[str] = []
        self._restore_report: dict[str, Any] = {}
        self._counters: dict[str, int] = {
            "runs": 0,
            "tasks_evaluated": 0,
            "refusals": 0,
            "leakage_refusals": 0,
        }

    # -- the registry --------------------------------------------------------
    @property
    def builtin_task_count(self) -> int:
        return len(self._builtin_ids)

    def families(self) -> tuple[FamilyDefinition, ...]:
        return FAMILIES

    def tasks(
        self,
        *,
        family_id: str = "",
        split: Split | None = None,
        difficulty: Any | None = None,
        include_registered: bool = True,
    ) -> tuple[EvaluationTask, ...]:
        """The registered tasks matching a filter, in a deterministic order.

        Built-ins first (corpus order), then registered tasks in registration
        order, so two runs over the same filter evaluate the same list.
        """
        selected: list[EvaluationTask] = []
        for task in self._tasks.values():
            if not include_registered and task.task_id not in self._builtin_ids:
                continue
            if family_id and task.family_id != family_id:
                continue
            if split is not None and task.split is not split:
                continue
            if difficulty is not None and task.difficulty is not difficulty:
                continue
            selected.append(task)
        return tuple(selected)

    def registered_tasks(self) -> tuple[EvaluationTask, ...]:
        return tuple(
            task for task_id, task in self._tasks.items() if task_id not in self._builtin_ids
        )

    def task(self, task_id: str) -> EvaluationTask:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"no task is registered as {task_id!r}") from exc

    def register_task(self, payload: Mapping[str, Any]) -> EvaluationTask:
        """Register one task from a validated mapping.

        Refused, with the reason: an id already in use (a registered task may not
        replace a built-in one — the corpus is evidence), an unverifiable task,
        and a corpus larger than the configured ceiling.
        """
        if len(self.registered_tasks()) >= self.settings.max_registered_tasks:
            raise ValueError(
                f"this build registers at most {self.settings.max_registered_tasks} tasks; "
                "unregister or raise the ceiling deliberately"
            )
        task = validate_task(payload)  # raises with the specific reason
        if task.task_id in self._tasks:
            origin = "built-in" if task.task_id in self._builtin_ids else "registered"
            raise ValueError(
                f"task id {task.task_id!r} is already in use by a {origin} task; a corpus "
                "that can be overwritten cannot be cited as evidence"
            )
        self._tasks[task.task_id] = task
        self._announce(
            "generalization.task_registered",
            {
                "task_id": task.task_id,
                "family_id": task.family_id,
                "split": task.split.value,
                "kind": task.kind.value,
            },
        )
        return task

    def splits(self) -> dict[str, Any]:
        """How the corpus is split, with the leakage verdict attached."""
        counts = {split.value: 0 for split in Split}
        families: dict[str, int] = {}
        for task in self._tasks.values():
            counts[task.split.value] = counts.get(task.split.value, 0) + 1
            families[task.family_id] = families.get(task.family_id, 0) + 1
        return {
            "counts": counts,
            "families": dict(sorted(families.items())),
            "built_in": self.builtin_task_count,
            "registered": len(self.registered_tasks()),
            "corpus_version": CORPUS_VERSION,
            "leakage": self.leakage_report().to_dict(),
        }

    def leakage_report(self, *, tasks: Sequence[EvaluationTask] | None = None) -> LeakageReport:
        """Whether the held-out split is genuinely unseen — checked three ways.

        1. ``overlapping_ids``: the same task id, or the same question with the
           same answer, appears in both the fitting set and the held-out set.
        2. ``shared_answers``: a held-out task's (input, expected) pair already
           exists among the fitting tasks — its ANSWER was available, however
           different the task's name looks.
        3. ``used_answers``: an example of a held-out task is flagged ``seen``,
           which is the solver's own record that its answer was used while
           tuning. The phase names this case explicitly: an example is not
           unseen if its answer, or equivalent information, was used to tune.

        ``clean`` is ``None`` only when the check could not run (no held-out
        tasks at all), never when it found nothing.
        """
        corpus = tuple(tasks) if tasks is not None else tuple(self._tasks.values())
        fitting = [task for task in corpus if task.split is not Split.HELD_OUT]
        held_out = [task for task in corpus if task.split is Split.HELD_OUT]
        if not held_out:
            return LeakageReport(
                checked=True,
                clean=None,
                fitting_tasks=len(fitting),
                held_out_tasks=0,
                reason=(
                    "no held-out task is registered, so no generalization claim can be "
                    "made from this corpus"
                ),
            )
        used = sorted(
            f"{task.task_id}#{example.label or index}"
            for task in held_out
            for index, example in enumerate(task.examples)
            if example.seen
        )
        fitting_fingerprints = {
            _fingerprint(task.test_input, task.expected_output): task.task_id for task in fitting
        }
        overlaps: list[str] = []
        shared: list[str] = []
        for task in held_out:
            if task.task_id in {item.task_id for item in fitting}:
                overlaps.append(task.task_id)
            fingerprint = _fingerprint(task.test_input, task.expected_output)
            if fingerprint in fitting_fingerprints:
                shared.append(
                    f"{task.task_id} == {fitting_fingerprints[fingerprint]} "
                    "(same question, same answer)"
                )
        clean = not overlaps and not shared and not used
        parts: list[str] = []
        if overlaps:
            parts.append(f"{len(overlaps)} task id(s) appear in both splits")
        if shared:
            parts.append(f"{len(shared)} held-out answer(s) already appear in the fitting set")
        if used:
            parts.append(f"{len(used)} held-out example(s) were used while tuning")
        return LeakageReport(
            checked=True,
            clean=clean,
            fitting_tasks=len(fitting),
            held_out_tasks=len(held_out),
            overlapping_ids=tuple(overlaps),
            shared_answers=tuple(shared),
            used_answers=tuple(used),
            reason=(
                "the held-out split shares nothing with the fitting set"
                if clean
                else "; ".join(parts)
            ),
        )

    # -- running an evaluation ----------------------------------------------
    def evaluate(
        self,
        body: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> EvaluationRun:
        """Evaluate a baseline and a candidate over the SAME task set.

        The candidate runs first (its results are the ones being claimed), then
        the baseline over exactly the same tasks with the same budget — because
        comparing scores measured over different task sets is not a comparison.
        """
        payload = dict(body or {})
        if not self.settings.enabled:
            raise ValueError(
                "generalization evaluation is disabled by configuration, so nothing ran"
            )
        solver = str(payload.get("solver") or self.settings.default_solver)
        baseline = str(payload.get("baseline") or self.settings.default_baseline)
        if solver not in SOLVERS and solver not in BASELINES:
            raise ValueError(
                f"solver {solver!r} is not one this build has; it knows "
                f"{', '.join(SOLVERS)} and the baselines {', '.join(BASELINES)}"
            )
        if baseline and baseline not in BASELINES:
            raise ValueError(
                f"baseline {baseline!r} is not one this build has; it knows {', '.join(BASELINES)}"
            )
        family_id = str(payload.get("family") or self.settings.default_family)
        if family_id and family_id not in {item.family_id for item in FAMILIES}:
            raise ValueError(
                f"family {family_id!r} is not one this build has; it knows "
                + ", ".join(item.family_id for item in FAMILIES)
            )
        seed = int(payload.get("seed") or 0)
        split_filter: Split | None = None
        requested_split = str(payload.get("split") or "").strip()
        if requested_split and requested_split.lower() != "any":
            # "any" is spelled out rather than left implicit: a caller that
            # writes it is asking for no split filter at all, and silently
            # reading it as a filter name would refuse a request that said what
            # it wanted in plain words.
            try:
                split_filter = Split(requested_split)
            except ValueError as exc:
                raise ValueError(
                    f"split {requested_split!r} is not one this build has: "
                    f"{', '.join(item.value for item in Split)}"
                ) from exc
        elif not requested_split and self.settings.evaluate_held_out_only:
            split_filter = Split.HELD_OUT
        task_ids = payload.get("task_ids")
        if task_ids is not None:
            if not isinstance(task_ids, Sequence) or isinstance(task_ids, (str, bytes)):
                raise ValueError("task_ids must be a list of task ids")
            selected = []
            for item in task_ids:
                try:
                    selected.append(self.task(str(item)))
                except KeyError as exc:
                    raise ValueError(str(exc)) from exc
        else:
            selected = list(self.tasks(family_id=family_id, split=split_filter))
        if not selected:
            raise ValueError(
                "no task matched the request, so there is nothing to evaluate "
                f"(family={family_id or 'any'}, "
                f"split={split_filter.value if split_filter else 'any'})"
            )
        max_tasks = min(
            int(payload.get("max_tasks") or self.settings.max_tasks_per_run),
            self.settings.max_tasks_per_run,
            HARD_MAX_TASKS_PER_RUN,
        )
        budget = Budget(
            max_candidates=min(
                int(payload.get("max_candidates") or self.settings.max_candidates),
                self.settings.max_candidates,
                HARD_MAX_CANDIDATES,
            ),
            max_depth=min(
                int(payload.get("max_depth") or self.settings.max_depth),
                self.settings.max_depth,
                MAX_DEPTH,
            ),
            max_seconds=_seconds(
                payload.get("max_seconds"), self.settings.max_seconds, HARD_MAX_SECONDS
            ),
            max_model_calls=self.settings.max_model_calls,
        )
        truncated = len(selected) > max_tasks
        if truncated:
            # A truncated run says so: a task left out of a denominator is not a
            # failure, but silently dropping it would be a lie about the sample.
            selected = selected[:max_tasks]
        leakage = self.leakage_report(tasks=selected) if family_id else self.leakage_report()
        violations: list[str] = []
        if leakage.checked and leakage.clean is False:
            violations.append(leakage.reason)
            if self.settings.require_clean_splits:
                self._counters["leakage_refusals"] += 1
                # The refusal is ANNOUNCED before it is raised. Publishing this
                # only on the advisory path left the one caller who most needs to
                # know — the person whose measurement was just refused — with
                # nothing on the bus, so the event existed for the case it never
                # covered.
                self._announce(
                    "generalization.leakage_detected",
                    {
                        "run_id": "",
                        "refused": True,
                        "reason": leakage.reason[:200],
                        "overlapping_ids": list(leakage.overlapping_ids)[:5],
                        "shared_answers": list(leakage.shared_answers)[:5],
                        "used_answers": list(leakage.used_answers)[:5],
                    },
                )
                raise ValueError(
                    "the evaluation was refused because the split is not clean: "
                    f"{leakage.reason}. A number measured over leaked examples is not a "
                    "generalization measurement"
                )
        run = self._make_run(
            run_id=run_id,
            family_id=family_id,
            solver=solver,
            baseline=baseline,
            seed=seed,
            budget=budget,
            settings={
                "max_tasks": max_tasks,
                "max_candidates": budget.max_candidates,
                "max_depth": budget.max_depth,
                "max_seconds": budget.max_seconds,
                "max_model_calls": budget.max_model_calls,
                "max_results_kept": self.settings.max_results_kept,
                "truncated": truncated,
                "split": split_filter.value if split_filter else "any",
                "require_clean_splits": self.settings.require_clean_splits,
                # The setting that decided WHICH tasks were measured is part of
                # the frozen configuration: without it a run that evaluated 20 of
                # 39 tasks could not say why, and could not be reproduced.
                "evaluate_held_out_only": self.settings.evaluate_held_out_only,
                "corpus_version": CORPUS_VERSION,
            },
            violations=violations,
            leakage=leakage,
        )
        self._announce(
            "generalization.evaluation_started",
            {
                "run_id": run.run_id,
                "solver": solver,
                "tasks": len(selected),
                "family_id": family_id,
                "seed": seed,
            },
        )
        started = time.monotonic()
        candidate_results = self._evaluate_tasks(selected, solver=solver, budget=budget)
        baseline_results: tuple[TaskResult, ...] = ()
        if baseline:
            baseline_results = self._evaluate_tasks(selected, solver=baseline, budget=budget)
        run = self._finish_run(
            run, candidate_results=candidate_results, baseline_results=baseline_results
        )
        self._store_run(run)
        self._counters["runs"] += 1
        self._counters["tasks_evaluated"] += len(candidate_results)
        self._announce(
            "generalization.evaluation_completed",
            {
                "run_id": run.run_id,
                "status": run.status.value,
                "solved": run.solved,
                "held_out_solved": run.held_out_solved,
                "held_out_tasks": run.held_out_tasks,
                "duration_ms": run.duration_ms,
                "elapsed_ms": (time.monotonic() - started) * 1000.0,
            },
        )
        for failure in run.failures:
            self._announce(
                "generalization.failure_classified",
                {
                    "run_id": run.run_id,
                    "task_id": failure.task_id,
                    "category": failure.category.value,
                },
            )
        if violations:
            self._announce(
                "generalization.leakage_detected",
                {"run_id": run.run_id, "reason": violations[0][:200]},
            )
        return run

    def _evaluate_tasks(
        self, tasks: Sequence[EvaluationTask], *, solver: str, budget: Budget
    ) -> tuple[TaskResult, ...]:
        results: list[TaskResult] = []
        for task in tasks:
            clock = EvaluationClock.start()
            results.append(evaluate_task(task, solver=solver, budget=budget, clock=clock))
        return tuple(results)

    def _make_run(
        self,
        *,
        run_id: str | None,
        family_id: str,
        solver: str,
        baseline: str,
        seed: int,
        budget: Budget,
        settings: Mapping[str, Any],
        violations: Sequence[str],
        leakage: LeakageReport | None = None,
    ) -> EvaluationRun:
        return EvaluationRun(
            run_id=run_id or f"gen-{uuid.uuid4().hex[:12]}",
            status=RunStatus.RUNNING,
            family_id=family_id,
            solver=solver,
            baseline_id=baseline,
            candidate_id=solver,
            seed=seed,
            settings=dict(settings),
            started_at=_now(),
            violations=tuple(violations),
            # The verdict the run was DECIDED on travels with the run. Leaving
            # the model default here made every stored run report
            # `checked: false, clean: None` — a record that claims the check it
            # was gated on never happened.
            leakage=leakage if leakage is not None else LeakageReport(),
            provider=self._provider_posture(),
            provider_detail=self._provider_detail,
        )

    def _finish_run(
        self,
        run: EvaluationRun,
        *,
        candidate_results: Sequence[TaskResult],
        baseline_results: Sequence[TaskResult],
    ) -> EvaluationRun:
        failures = tuple(
            FailureRecord(
                task_id=result.task_id,
                family_id=result.family_id,
                category=result.failure,
                reason=result.failure_reason,
                expected=result.expected,
                predicted=result.predicted,
                candidate=result.candidate,
                evidence=result.evidence,
                reproduce={
                    "solver": result.solver,
                    "seed": run.seed,
                    "settings": dict(run.settings),
                    "task_id": result.task_id,
                },
            )
            for result in candidate_results
            if result.failure is not None
        )
        kept = tuple(candidate_results[: self.settings.max_results_kept])
        status = RunStatus.COMPLETED
        if any(result.status is TaskStatus.BUDGET_EXHAUSTED for result in candidate_results):
            status = RunStatus.BUDGET_EXHAUSTED
        if not candidate_results:
            status = RunStatus.FAILED
        return replace(
            run,
            status=status,
            results=kept,
            baseline_results=tuple(baseline_results[: self.settings.max_results_kept]),
            results_kept=len(kept),
            tasks_evaluated=len(candidate_results),
            task_ids=tuple(result.task_id for result in candidate_results),
            metrics=metrics_by_family(tuple(candidate_results)),
            failures=failures,
            finished_at=_now(),
            duration_ms=sum(result.duration_ms or 0.0 for result in candidate_results),
            resource_use={
                "candidates_examined": sum(
                    result.candidates_examined for result in candidate_results
                ),
                "results_kept": len(kept),
                "results_total": len(candidate_results),
                "baseline_results_kept": len(baseline_results),
                "model_calls": 0,
            },
        )

    # -- reading the records -------------------------------------------------
    def runs(self) -> tuple[EvaluationRun, ...]:
        """The stored runs, newest first — as full records, for an internal caller."""
        return tuple(self._runs[run_id] for run_id in reversed(self._order))

    def run_rows(self) -> list[dict[str, Any]]:
        """The TRIMMED rows: summaries only, never the per-task result bodies."""
        return [run.row() for run in self.runs()]

    def run(self, run_id: str) -> EvaluationRun:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise KeyError(f"no evaluation run is stored as {run_id!r}") from exc

    def view(self, run_id: str) -> dict[str, Any]:
        """One run, whole: settings, metrics per family, failures and results.

        The outcome FINGERPRINT travels with the run: the evaluate route and the
        API reference both promise it on this view, and it is derived from the
        results already in hand (never stored), so a caller checking whether two
        runs reproduced does not have to fetch the export to find out.
        """
        run = self.run(run_id)
        payload = {
            **run.to_dict(),
            "recommendations": [item.to_dict() for item in self.recommendations(run_id)],
            "failure_groups": self.failure_groups(run_id),
        }
        payload["fingerprint"] = self.fingerprint(payload)
        return payload

    def failures(self, run_id: str = "") -> tuple[FailureRecord, ...]:
        if run_id:
            return self.run(run_id).failures
        return tuple(failure for run in self.runs() for failure in run.failures)

    def failure_groups(self, run_id: str = "") -> dict[str, list[str]]:
        return group_failures(self.failures(run_id))

    def recommendations(self, run_id: str = "") -> tuple[ImprovementRecommendation, ...]:
        """Grouped failures turned into advice — never into an automatic job.

        One recommendation per category, naming the tasks and families that
        produced it, with the advice text that belongs to that category. Nothing
        is executed, no dataset is written and no model is touched: the phase
        forbids the benchmark from starting training as a side effect.
        """
        records = self.failures(run_id)
        grouped: dict[FailureCategory, list[FailureRecord]] = {}
        for record in records:
            grouped.setdefault(record.category, []).append(record)
        recommendations: list[ImprovementRecommendation] = []
        for category, items in sorted(grouped.items(), key=lambda pair: pair[0].value):
            tasks = tuple(sorted({item.task_id for item in items}))
            families = tuple(sorted({item.family_id for item in items}))
            recommendations.append(
                ImprovementRecommendation(
                    category=category,
                    occurrences=len(items),
                    tasks=tasks,
                    families=families,
                    recommendation=FAILURE_ADVICE.get(
                        category, "no advice is recorded for this category"
                    ),
                    evidence=tuple(f"{item.task_id}: {item.reason}" for item in items[:4]),
                )
            )
        return tuple(recommendations)

    def export(self, run_id: str) -> dict[str, Any]:
        """A report payload for review: the run, the metrics, the failures, the cost.

        It is data for a person to read. Nothing here schedules work, writes a
        dataset or promotes the candidate — the phase requires the decision to
        stay outside the benchmark.
        """
        view = self.view(run_id)
        return {
            "run": view,
            "exported_at": _now(),
            "corpus": {
                "version": CORPUS_VERSION,
                "built_in_tasks": self.builtin_task_count,
                "registered_tasks": len(self.registered_tasks()),
                "splits": self.splits(),
            },
            "operators": list(operator_catalogue()),
            "grid_rules_supported": list(GRID_RULES_SUPPORTED),
            "solvers": list(SOLVERS),
            "baselines": list(BASELINES),
            "reproduce": {
                "solver": view["solver"],
                "baseline": view["baseline_id"],
                "seed": view["seed"],
                "settings": view["settings"],
                "task_ids": view["task_ids"],
            },
            "fingerprint": view["fingerprint"],
        }

    def compare(self, baseline_run_id: str, candidate_run_id: str) -> ComparisonReport:
        """Compare two stored runs task by task.

        The stored records are read, never rewritten: ``baseline_preserved`` is
        reported as ``True`` because this method only reads. A regression here is
        a task that was solved in the baseline and is not solved in the
        candidate.
        """
        baseline = self.run(baseline_run_id)
        candidate = self.run(candidate_run_id)
        return compare_results(
            baseline.results,
            candidate.results,
            baseline_id=baseline.run_id,
            candidate_id=candidate.run_id,
            baseline_solver=baseline.solver,
            candidate_solver=candidate.solver,
            seed=candidate.seed,
            baseline_seed=baseline.seed,
        )

    def fingerprint(self, run_view: Mapping[str, Any]) -> str:
        """A hash of everything that decides a run's numbers — and nothing else.

        Durations, timestamps and result order are excluded on purpose: a
        reproducibility check compares OUTCOMES, and a wall-clock difference must
        not make two identical evaluations look different. Two runs with the same
        fingerprint got the same answers from the same inputs.
        """
        results = run_view.get("results") or []
        compact: list[dict[str, Any]] = []
        for result in sorted(results, key=lambda item: str(item.get("task_id"))):
            compact.append(
                {
                    "task_id": result.get("task_id"),
                    "status": result.get("status"),
                    "candidate": result.get("candidate"),
                    "source": result.get("source"),
                    "predicted": result.get("predicted"),
                    "exact_match": result.get("exact_match"),
                }
            )
        payload = {
            "solver": run_view.get("solver"),
            "baseline": run_view.get("baseline_id"),
            "seed": run_view.get("seed"),
            "settings": run_view.get("settings"),
            "tasks": run_view.get("task_ids"),
            "results": compact,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    # -- status and persistence ---------------------------------------------
    def status(self) -> dict[str, Any]:
        """What this build can evaluate here, and what it refuses to claim."""
        corpus = self.splits()
        solved = sum(run.solved for run in self.runs())
        return {
            "phase": "26",
            "enabled": self.settings.enabled,
            "corpus": corpus,
            "families": [item.to_dict() for item in FAMILIES],
            "solvers": list(SOLVERS),
            "baselines": list(BASELINES),
            "operators": list(OPERATOR_NAMES),
            "settings": self.settings.to_mapping(),
            "counters": dict(self._counters),
            "runs": len(self._runs),
            "solved_across_runs": solved,
            "provider": self._provider_posture(),
            "provider_detail": self._provider_detail,
            "restored": dict(self._restore_report),
            "storage": {
                "key": STORE_KEY,
                "persisted": bool(self._store is not None and self.settings.persist),
                "kept_runs": len(self._runs),
                "max_runs": self.settings.keep_runs,
            },
        }

    def summary(self) -> dict[str, Any]:
        return {
            "runs": len(self._runs),
            "tasks": len(self._tasks),
            "held_out_tasks": len(self.tasks(split=Split.HELD_OUT)),
            "families": len(FAMILIES),
        }

    def restore(self) -> dict[str, Any]:
        """Rebuild the stored runs. A run whose record is unreadable is skipped.

        Restoring reports what it did and never raises: a benchmark record that
        could not be read is reported as skipped, with the reason, which is the
        honest ending for a file this build cannot parse.
        """
        report: dict[str, Any] = {"restored": 0, "skipped": 0, "reason": ""}
        if self._store is None:
            report["reason"] = "no state store is wired, so nothing was restored"
            self._restore_report = report
            return report
        try:
            payload = self._store.read(STORE_KEY) or {}
        except Exception as exc:  # noqa: BLE001 - an unreadable store is reported, not fatal
            report["reason"] = f"the store could not be read: {exc}"
            self._restore_report = report
            return report
        rows = payload.get("runs") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            report["reason"] = "no stored runs were found"
            self._restore_report = report
            return report
        for record in rows:
            if not isinstance(record, Mapping):
                report["skipped"] += 1
                continue
            try:
                run = EvaluationRun.from_mapping(record)
            except Exception:  # noqa: BLE001 - an unreadable record is skipped, not fatal
                report["skipped"] += 1
                continue
            if not run.run_id:
                report["skipped"] += 1
                continue
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            report["restored"] += 1
        self._restore_report = report
        return report

    def close(self) -> dict[str, Any]:
        """Flush the runs to the store. Called on application shutdown."""
        if self._store is None or not self.settings.persist:
            return {"saved": 0, "reason": "nothing is persisted by configuration"}
        saved = self._save()
        return {"saved": saved, "reason": ""}

    def _store_run(self, run: EvaluationRun) -> None:
        self._runs[run.run_id] = run
        self._order.append(run.run_id)
        while len(self._order) > self.settings.keep_runs:
            dropped = self._order.pop(0)
            self._runs.pop(dropped, None)
        if self._store is not None and self.settings.persist:
            self._save()

    def _save(self) -> int:
        store = self._store
        if store is None:
            # Nothing to write to. ``close`` already reports this as a reason;
            # here it is simply nothing persisted rather than an error.
            return 0
        rows = [self._runs[run_id].to_dict() for run_id in self._order]
        try:
            store.write(STORE_KEY, {"runs": rows, "schema_version": RUN_SCHEMA_VERSION})
        except Exception:  # noqa: BLE001 - persistence never breaks an evaluation
            return 0
        return len(rows)

    def _provider_posture(self) -> str:
        if self._provider is None:
            return ProviderPosture.NOT_REQUIRED.value
        configured = bool(getattr(self._provider, "model_configured", False))
        return ProviderPosture.AVAILABLE.value if configured else ProviderPosture.UNAVAILABLE.value

    def _announce(self, kind: str, payload: Mapping[str, Any]) -> None:
        """Publish one event through the injected seam; never raise out of it."""
        if self._observer is None:
            return
        try:
            self._observer(kind, dict(payload))
        except Exception:  # noqa: BLE001 - an event that fails must not fail a run
            return


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fingerprint(test_input: Mapping[str, Any], expected: Any) -> str:
    """The identity of a (question, answer) pair, for the leakage check."""
    payload = json.dumps({"input": test_input, "expected": expected}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _count(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < 0:
        return 0
    return min(number, maximum)


def _seconds(value: Any, default: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(max(0.05, number), maximum)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def builtin_task_ids() -> tuple[str, ...]:
    """Every built-in task id, in corpus order — used by tests and the benchmark."""
    return tuple(task.task_id for task in BUILTIN_TASKS)
