"""Phase 26 — generalization, ARC-style reasoning and intelligence evaluation.

Two halves, both required.

The first half exercises the contracts directly: what a grid must look like to
be a grid, what an operator may be asked to do, what the search is allowed to
believe, where an answer may come from, and what the numbers mean. These tests
pin the RULES — a ragged grid is refused rather than padded, a rule that does
not reproduce every example is not a rule, a held-out answer is never consulted
by the search, a rate with no denominator is ``None`` rather than ``0.0``, and a
dirty split REFUSES the run instead of annotating it.

The second half drives the REAL application over HTTP (``create_app()`` against
an isolated data directory), which is the interface a user or a CLI reaches:
``POST /generalization/evaluate`` to run one, ``GET /generalization/runs/{id}``
to read it back, ``GET /generalization/compare`` to put it beside a baseline.
That is the only thing that proves the routes, the application methods, the
event seam, the capability registration and the persistence agree with each
other — an internal call could pass while the route 422s.

Every number asserted here was MEASURED by running the thing. Where a figure
could not be measured the assertion is that it is ``None``, which is the phase's
own rule (a figure nobody measured is not a zero).
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:  # pragma: no cover - import guard mirrors the rest of the suite
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
except Exception:  # pragma: no cover - httpx/fastapi missing
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    NovaControlApplication = None  # type: ignore[assignment]

from novacontrol.browser import NoopBrowserRunner
from novacontrol.core.events import EVENT_PAYLOAD_FIELDS, EventType
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.generalization import (
    BUILTIN_TASKS,
    GAME_EVAL_CONTRACTS,
    GRID_RULES_SUPPORTED,
    OPERATORS,
    STORE_KEY,
    Budget,
    CandidateRule,
    Difficulty,
    EvaluationTask,
    FailureCategory,
    GeneralizationManager,
    GeneralizationSettingsView,
    Grid,
    InvalidGrid,
    LeakageReport,
    Operator,
    RuleStep,
    Split,
    TaskKind,
    TaskStatus,
    capability_rows,
    compare_results,
    evaluate_embodied_sessions,
    evaluate_task,
    family_metrics,
    fit_rules,
    grid_equals,
    metrics_by_family,
    operator,
    overview,
    parse_grid,
    rule_from_mapping,
    rule_to_mapping,
    solve,
    summarise_embodied_session,
    task_by_id,
    tasks_payload,
    verify,
    weighted_aggregate,
)
from novacontrol.generalization.capabilities import CAPABILITY_TABLE

# --------------------------------------------------------------------------- #
# Small doubles
# --------------------------------------------------------------------------- #


class _MemoryStore:
    """A state store that remembers what it was written, and can be broken."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.writes: list[str] = []

    def read(self, key: str) -> Any:
        return self.data.get(key)

    def write(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.writes.append(key)


class _BrokenStore(_MemoryStore):
    """A store that raises on every read — an unreadable record, not a crash."""

    def read(self, key: str) -> Any:
        raise PermissionError("the store is not readable")

    def write(self, key: str, value: Any) -> None:
        raise PermissionError("the store is not writable")


def _pairs(*items: tuple[list[str], list[str]]) -> tuple[tuple[Grid, Grid], ...]:
    return tuple((parse_grid(left), parse_grid(right)) for left, right in items)


def _grid_task_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": "t_probe",
        "kind": TaskKind.GRID_TRANSFORM.value,
        "family_id": "grid_transform",
        "split": Split.TRAIN.value,
        "difficulty": Difficulty.EASY.value,
        "request": "Rotate each input a quarter turn clockwise.",
        "success_condition": "the produced grid equals the expected grid exactly",
        "verifier": "exact_grid_match",
        "examples": [
            {"inputs": {"grid": ["120", "003"]}, "expected": ["01", "02", "30"]},
            {"inputs": {"grid": ["1100", "0010"]}, "expected": ["01", "01", "10", "00"]},
        ],
        "test_input": {"grid": ["230", "010"]},
        "expected_output": ["02", "13", "00"],
        "rule_family": "rotation",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Grids: parsing, validation, and the analyses everything else is built on
# --------------------------------------------------------------------------- #


class GridParsingTests(unittest.TestCase):
    """A grid is a rectangle of palette colours, and nothing else."""

    def test_a_well_formed_grid_parses_from_rows_and_from_text(self) -> None:
        from_rows = parse_grid(["120", "003"])
        from_text = parse_grid("120\n003")
        self.assertEqual(from_rows.shape, (2, 3))
        self.assertEqual(from_rows.cells, from_text.cells)
        self.assertEqual(from_rows.colors(), (0, 1, 2, 3))

    def test_every_kind_of_unreadable_grid_is_refused_by_name(self) -> None:
        cases: dict[str, Any] = {
            "ragged": ["12", "1"],
            "empty": [],
            "empty row": [""],
            "out of palette": ["1a", "00"],
            "negative colour": ["-1", "00"],
            "boolean cell": [[True, False]],
            "not a sequence": 7,
        }
        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(InvalidGrid):
                parse_grid(value)

    def test_an_unreadable_grid_is_never_equal_to_another_unreadable_grid(self) -> None:
        self.assertFalse(grid_equals(["12", "1"], ["12", "1"]), "a ragged grid is not a match")
        self.assertFalse(
            grid_equals(["12", "1"], ["12", "3"]),
            "two unreadable payloads are not equal to each other",
        )
        self.assertFalse(grid_equals(["12"], ["13"]), "a readable grid never matches a wrong one")
        self.assertTrue(grid_equals(["12"], ["12"]), "and a correct one does match")

    def test_a_grid_beyond_the_size_bound_is_refused(self) -> None:
        too_wide = ["0" * 33]
        with self.assertRaises(InvalidGrid):
            parse_grid(too_wide)
        big = ["0" * 32 for _ in range(32)]
        self.assertEqual(parse_grid(big).size, 1024)


class GridAnalysisTests(unittest.TestCase):
    """Objects, background, symmetry — the structure the operators read."""

    def test_objects_are_the_connected_components_that_are_not_background(self) -> None:
        grid = parse_grid(["110", "000", "101"])
        objects = grid.objects()
        self.assertEqual(len(objects), 3, "three separate components of colour 1")
        self.assertEqual(objects[0].size, 2, "sorted by size, descending")
        self.assertEqual(grid.object_count(), 3)

    def test_the_background_is_read_from_the_corners_before_the_majority(self) -> None:
        # The corners are what a background looks like: it is the colour the
        # content sits ON, so it is the colour the border already agrees on.
        self.assertEqual(parse_grid(["00000", "01110", "01010", "01110", "00000"]).background(), 0)
        self.assertEqual(parse_grid(["22222", "21112", "21212", "21112", "22222"]).background(), 2)
        # When the corners DISAGREE, the majority decides, ties broken by the
        # smaller colour value — so the answer is the same on every run.
        self.assertEqual(parse_grid(["12", "34"]).background(), 1)
        self.assertEqual(parse_grid(["32", "14"]).background(), 1)

    def test_symmetries_are_reported_not_assumed(self) -> None:
        flags = parse_grid(["11", "11"]).symmetries()
        self.assertTrue(all(flags.values()), "a uniform grid has every symmetry")
        flags = parse_grid(["12", "34"]).symmetries()
        self.assertFalse(flags["horizontal"])
        self.assertFalse(flags["rotational"])

    def test_transformations_return_new_grids_and_never_mutate(self) -> None:
        source = parse_grid(["12", "34"])
        self.assertEqual(source.rotated(1).cells, parse_grid(["31", "42"]).cells)
        self.assertEqual(source.rotated(4).cells, source.cells)
        self.assertEqual(source.flipped(axis="horizontal").cells, parse_grid(["21", "43"]).cells)
        self.assertEqual(source.translated(1, 0).cells, parse_grid(["00", "12"]).cells)
        self.assertEqual(source.tiled(vertical=2, horizontal=1).height, 4)
        self.assertEqual(source.cells, parse_grid(["12", "34"]).cells, "the source is intact")

    def test_a_tiling_that_would_exceed_the_cell_bound_is_refused(self) -> None:
        grid = parse_grid(["1" * 32 for _ in range(32)])
        with self.assertRaises(InvalidGrid):
            grid.tiled(vertical=2, horizontal=2)


# --------------------------------------------------------------------------- #
# The operator library and the search that infers a rule from examples
# --------------------------------------------------------------------------- #


class OperatorLibraryTests(unittest.TestCase):
    """The vocabulary is enumerated, bounded, and contains no answers."""

    def test_the_library_is_small_enough_to_enumerate_and_read(self) -> None:
        self.assertLessEqual(len(OPERATORS), 30, "a library nobody can audit is a liability")
        names = [item.name for item in OPERATORS]
        self.assertEqual(len(names), len(set(names)), "operator names are unique")
        for expected in ("identity", "rotate", "flip", "translate", "recolor_map", "tile"):
            self.assertIn(expected, names)

    def test_every_operator_is_total_on_the_grids_the_corpus_uses(self) -> None:
        for task in BUILTIN_TASKS:
            if task.kind is not TaskKind.GRID_TRANSFORM:
                continue
            for example in task.examples:
                grid = parse_grid(example.inputs["grid"])
                for item in OPERATORS:
                    params = _first_proposal(item, grid)
                    if params is None:
                        continue
                    with self.subTest(operator=item.name, task=task.task_id):
                        produced = item.apply(grid, params)
                        self.assertIsInstance(produced, Grid)

    def test_an_unknown_operator_is_refused_by_name(self) -> None:
        with self.assertRaises(KeyError) as caught:
            operator("answer_the_benchmark")
        self.assertIn("not a transformation this build has", str(caught.exception))

    def test_the_library_carries_no_task_ids_and_no_answer_keys(self) -> None:
        """The adversarial check: no hardcoded answer can hide in the library.

        ``transforms.py``, ``rules.py`` and ``solvers.py`` are read as TEXT and
        must not mention a single corpus task id. An operator that keyed on a
        task id would be a lookup dressed as a rule, and this is the check that
        would notice.
        """
        root = Path(__file__).resolve().parents[1] / "src" / "novacontrol" / "generalization"
        ids = {task.task_id for task in BUILTIN_TASKS}
        for name in ("transforms.py", "rules.py", "solvers.py"):
            source = (root / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                for task_id in ids:
                    self.assertNotIn(task_id, source, f"{name!r} names the task {task_id!r}")


def _first_proposal(item: Operator, grid: Grid) -> tuple[Any, ...] | None:
    """The first parameter set this operator would propose for a single pair."""
    try:
        proposals = item.propose(((grid, grid),))
    except Exception:  # noqa: BLE001 - a proposal that raises proposes nothing
        return None
    return proposals[0] if proposals else None


class RuleInferenceTests(unittest.TestCase):
    """A rule is believed only after reproducing every example exactly."""

    def test_a_rule_that_explains_the_examples_is_found(self) -> None:
        outcome = fit_rules(_pairs((["12", "03"], ["31", "42"])))
        self.assertTrue(outcome.fitting, "a quarter turn is inside the library")
        self.assertFalse(outcome.ambiguous)
        self.assertEqual(
            outcome.fitting[0].apply(parse_grid(["12", "03"])).cells, parse_grid(["31", "42"]).cells
        )

    def test_a_rule_that_fits_one_pair_and_not_another_is_not_a_rule(self) -> None:
        # Both pairs are rotations, but of different amounts: no single rule
        # reproduces both, so nothing should be believed.
        pairs = _pairs((["12", "34"], ["31", "42"]), (["12", "34"], ["43", "21"]))
        outcome = fit_rules(pairs)
        single = [rule for rule in outcome.fitting if rule.depth == 1]
        self.assertEqual(single, [], "no depth-1 rule fits two different rotations")
        for rule in outcome.fitting:
            self.assertTrue(rule.matches(pairs))

    def test_a_composition_is_fitted_to_a_residual_not_to_a_cross_product(self) -> None:
        pairs = _pairs((["12", "03"], ["07", "98"]), (["13", "20"], ["87", "09"]))
        outcome = fit_rules(pairs)
        self.assertTrue(outcome.fitting, "rotate-then-recolour is two supported steps")
        self.assertTrue(any(rule.depth == 2 for rule in outcome.fitting))
        for rule in outcome.fitting:
            self.assertTrue(rule.matches(pairs))

    def test_the_search_respects_its_candidate_and_depth_ceilings(self) -> None:
        pairs = _pairs((["12", "34"], ["12", "34"]))
        capped = fit_rules(pairs, max_candidates=3)
        self.assertLessEqual(capped.examined, 3, "the candidate ceiling is hard")
        shallow = fit_rules(pairs, max_depth=1)
        self.assertFalse(
            any(rule.depth > 1 for rule in shallow.fitting),
            "a depth ceiling of one never composes",
        )

    def test_a_rule_survives_a_json_round_trip(self) -> None:
        rule = CandidateRule(steps=(RuleStep("rotate", (1,)), RuleStep("flip", ("horizontal",))))
        rebuilt = rule_from_mapping(json.loads(json.dumps(rule_to_mapping(rule))))
        self.assertEqual(rebuilt.rule_id, rule.rule_id)
        self.assertEqual(
            rebuilt.apply(parse_grid(["12", "34"])).cells,
            rule.apply(parse_grid(["12", "34"])).cells,
        )

    def test_a_stored_rule_naming_an_unknown_operator_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            rule_from_mapping({"steps": [{"operator": "answer_key", "params": []}]})

    def test_the_search_never_sees_the_held_out_answer(self) -> None:
        """Mutate the answer key and the prediction must not move.

        This is the load-bearing leak test: if a solver's output changed when
        the EXPECTED output changed, the key would be reaching the search.
        """
        for task in BUILTIN_TASKS:
            if task.kind is not TaskKind.GRID_TRANSFORM or task.split is not Split.HELD_OUT:
                continue
            with self.subTest(task=task.task_id):
                real = solve(task, solver="infer")
                perturbed = EvaluationTask(
                    task_id=task.task_id + "#perturbed",
                    kind=task.kind,
                    family_id=task.family_id,
                    split=task.split,
                    difficulty=task.difficulty,
                    request=task.request,
                    success_condition=task.success_condition,
                    examples=task.examples,
                    test_input=task.test_input,
                    # A key that answers something else entirely.
                    expected_output=[[7]],
                    verifier=task.verifier,
                    seed=task.seed,
                    rule_family=task.rule_family,
                )
                other = solve(perturbed, solver="infer")
                self.assertEqual(
                    real.predicted,
                    other.predicted,
                    "the answer key changed the solver's answer, so the key leaked",
                )


# --------------------------------------------------------------------------- #
# Verification and metrics: exact match is not cell accuracy
# --------------------------------------------------------------------------- #


class VerificationTests(unittest.TestCase):
    """The evaluator holds the key; the check is the family's own notion of done."""

    def test_an_exact_match_is_the_only_thing_that_solves_a_grid_task(self) -> None:
        task = task_by_id("gt_rotate90")
        right = verify(task, ["02", "13", "00"])
        wrong = verify(task, ["02", "13", "01"])
        self.assertIs(right.verified, True)
        self.assertIs(wrong.verified, False)
        self.assertIn("expected", right.evidence[0])

    def test_cell_accuracy_is_secondary_and_never_becomes_a_solve(self) -> None:
        task = task_by_id("gt_rotate90")
        result = evaluate_task(task, solver="infer")
        self.assertIsNotNone(result.cell_accuracy)
        if result.solved:
            self.assertEqual(result.cell_accuracy, 1.0, "a solved grid task got every cell")
        else:
            self.assertIsNot(result.status, TaskStatus.SOLVED)
        # A wrong answer still produces the secondary figure — it is a
        # measurement of HOW wrong, and it never upgrades the ending.
        wrong = evaluate_task(task, solver="identity")
        if not wrong.solved:
            self.assertIsNotNone(wrong.cell_accuracy)
            self.assertIsNot(wrong.status, TaskStatus.SOLVED)

    def test_a_plan_is_re_simulated_rather_than_trusted(self) -> None:
        task = task_by_id("pl_corridor")
        claims_a_path = {"path": [[0, 0], [1, 0], [2, 0], [3, 0], [3, 1], [3, 2], [3, 3]]}
        walkable = verify(task, claims_a_path)
        self.assertIs(
            walkable.verified, True, "the path is in bounds, avoids walls and ends on the goal"
        )
        through_a_wall = {"path": [[0, 0], [1, 1], [3, 3]]}
        self.assertIs(verify(task, through_a_wall).verified, False)

    def test_an_answer_that_is_not_the_right_shape_is_rejected_with_a_reason(self) -> None:
        task = task_by_id("gt_rotate90")
        record = verify(task, "not a grid at all")
        self.assertIs(record.verified, False)
        self.assertIn("not a grid", record.detail)


class MetricsTests(unittest.TestCase):
    """Rates carry their denominators, and an aggregate must be asked for."""

    def _results(self) -> Any:
        return [evaluate_task(task, solver="infer") for task in BUILTIN_TASKS[:6]]

    def test_a_family_with_no_tasks_reports_no_rate_rather_than_zero(self) -> None:
        metrics = family_metrics("empty", [])
        self.assertIsNone(metrics.solve_rate)
        self.assertIsNone(metrics.held_out_solve_rate)
        self.assertIsNone(metrics.cell_accuracy)
        self.assertEqual(metrics.tasks, 0)

    def test_cell_accuracy_and_solve_rate_are_reported_separately(self) -> None:
        metrics = metrics_by_family(self._results())
        for item in metrics:
            with self.subTest(family=item.family_id):
                self.assertIsNotNone(item.solve_rate)
                if item.cell_accuracy is not None:
                    self.assertGreaterEqual(item.cell_accuracy, 0.0)
                    self.assertLessEqual(item.cell_accuracy, 1.0)
                self.assertGreaterEqual(item.tasks, item.solved)
                self.assertLessEqual(item.solved, item.tasks)
                if item.tasks:
                    self.assertIsNotNone(item.solve_rate)
                    self.assertEqual(item.solve_rate, item.solved / item.tasks)

    def test_no_aggregate_score_exists_without_an_explicit_normalization(self) -> None:
        metrics = metrics_by_family(self._results())
        refused = weighted_aggregate(metrics, weights={}, normalised=False)
        self.assertIsNone(refused["score"])
        allowed = weighted_aggregate(
            metrics, weights={item.family_id: 1.0 for item in metrics}, normalised=True
        )
        if allowed["score"] is not None:
            self.assertTrue(allowed["reason"], "an aggregate must state its terms")
            self.assertTrue(allowed["inputs"], "and show the inputs it combined")

    def test_a_comparison_names_regressions_and_never_mutates_the_baseline(self) -> None:
        baseline = [evaluate_task(task, solver="infer") for task in BUILTIN_TASKS[:4]]
        # The "candidate" solves strictly less: everything that was solved is a
        # regression, and the movement list must say so per task.
        candidate = [evaluate_task(task, solver="no_answer") for task in BUILTIN_TASKS[:4]]
        report = compare_results(
            baseline,
            candidate,
            baseline_id="base",
            candidate_id="cand",
            seed=7,
            baseline_seed=7,
        )
        self.assertEqual(report.tasks_compared, 4)
        self.assertTrue(report.regressed, "a candidate that solves nothing regresses")
        self.assertTrue(report.baseline_preserved)
        self.assertEqual(report.baseline_metrics, metrics_by_family(baseline))

    def test_a_relative_change_over_a_zero_baseline_is_not_invented(self) -> None:
        zero = family_metrics("grid", [])
        self.assertIsNone(zero.solve_rate, "no tasks means no rate, not a zero")


# --------------------------------------------------------------------------- #
# The corpus: splits, leakage, and the honest endings
# --------------------------------------------------------------------------- #


class CorpusTests(unittest.TestCase):
    """The corpus is small, named, declared, and verifiable."""

    def test_every_task_states_its_success_condition_and_verifier(self) -> None:
        for task in BUILTIN_TASKS:
            with self.subTest(task=task.task_id):
                self.assertTrue(task.success_condition)
                self.assertTrue(task.verifier)

    def test_the_task_list_never_carries_the_answer_key(self) -> None:
        payload = tasks_payload()
        self.assertTrue(payload)
        for row in payload:
            self.assertNotIn("expected_output", row, "a list is not where a key belongs")

    def test_registering_a_task_is_validated_and_ids_cannot_be_replaced(self) -> None:
        manager = GeneralizationManager()
        with self.assertRaises(ValueError) as caught:
            manager.register_task({"task_id": ""})
        self.assertIn("task_id", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            manager.register_task(_grid_task_payload(success_condition=""))
        self.assertIn("success_condition", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            manager.register_task(_grid_task_payload(task_id="gt_rotate90"))
        self.assertIn(
            "built-in", str(caught.exception), "a corpus that can be overwritten is not evidence"
        )

    def test_an_unverifiable_task_is_refused_at_registration(self) -> None:
        manager = GeneralizationManager()
        with self.assertRaises(ValueError) as caught:
            manager.register_task(_grid_task_payload(verifier="looks_about_right"))
        self.assertIn("verifier", str(caught.exception))

    def test_the_held_out_split_of_the_built_in_corpus_is_clean(self) -> None:
        report = GeneralizationManager().leakage_report()
        self.assertIsInstance(report, LeakageReport)
        self.assertTrue(report.checked)
        self.assertTrue(report.clean, report.reason)
        self.assertGreater(report.held_out_tasks, 0)
        self.assertGreater(report.fitting_tasks, 0)


class LeakageTests(unittest.TestCase):
    """A dirty split refuses the run — it is not annotated onto a number."""

    def test_an_answer_already_in_the_fitting_set_makes_the_split_unclean(self) -> None:
        manager = GeneralizationManager()
        # A held-out task whose question AND answer are already a TRAIN task's.
        train = manager.tasks(split=Split.TRAIN)[0]
        manager.register_task(
            _grid_task_payload(
                task_id="leaky_twin",
                split=Split.HELD_OUT.value,
                test_input=dict(train.test_input),
                expected_output=train.expected_output,
            )
        )
        report = manager.leakage_report()
        self.assertFalse(report.clean)
        self.assertTrue(report.shared_answers, "a shared answer is the leak")
        with self.assertRaises(ValueError) as caught:
            manager.evaluate({"split": "held_out", "task_ids": ["leaky_twin"]})
        self.assertIn("not clean", str(caught.exception))

    def test_an_example_marked_as_already_used_is_not_unseen(self) -> None:
        manager = GeneralizationManager()
        manager.register_task(
            _grid_task_payload(
                task_id="used_while_tuning",
                split=Split.HELD_OUT.value,
                examples=[{**_grid_task_payload()["examples"][0], "seen": True}],
            )
        )
        report = manager.leakage_report()
        self.assertFalse(report.clean)
        self.assertTrue(
            any(item.startswith("used_while_tuning") for item in report.used_answers),
            "the phase names this case: an example is not unseen if its answer was used",
        )

    def test_a_corpus_with_no_held_out_task_cannot_claim_generalization(self) -> None:
        # The check runs over WHATEVER set it is given — which is how a run over
        # one family that happens to hold no held-out task is reported: it
        # cannot say "clean", because there was nothing unseen to measure.
        manager = GeneralizationManager()
        fitting = manager.tasks(split=Split.TRAIN)
        report = manager.leakage_report(tasks=fitting)
        self.assertTrue(report.checked)
        self.assertEqual(report.held_out_tasks, 0)
        self.assertIsNone(report.clean, "no held-out task is not a clean bill of health")
        self.assertIn("no generalization claim", report.reason)


class VerificationAuditTests(unittest.TestCase):
    """What the Phase 26 verification audit found, pinned so it cannot come back.

    Three defects were real and none of them was visible from a passing score:
    a refusal that announced nothing, a stored run that claimed the check it was
    gated on never happened, and a frozen configuration that could not say which
    tasks it had measured. Each one is a claim a reader makes from the record.
    """

    def _leaky(self, manager: GeneralizationManager) -> None:
        """Register a held-out twin of a fitting task — the shared-answer leak."""
        train = manager.tasks(split=Split.TRAIN)[0]
        manager.register_task(
            _grid_task_payload(
                task_id="audit_leaky_twin",
                split=Split.HELD_OUT.value,
                test_input=dict(train.test_input),
                expected_output=train.expected_output,
            )
        )

    def test_a_refused_run_announces_the_leakage_it_refused(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        manager = GeneralizationManager(
            observer=lambda kind, payload: events.append((kind, dict(payload)))
        )
        self._leaky(manager)
        with self.assertRaises(ValueError):
            manager.evaluate({"split": "held_out", "seed": 1})

        announced = [
            payload for kind, payload in events if kind == "generalization.leakage_detected"
        ]
        self.assertTrue(announced, "the refusal is the case this event exists for")
        self.assertTrue(announced[0]["refused"], "the announcement says it was a refusal")
        self.assertTrue(announced[0]["shared_answers"], "and names the shared answer")
        self.assertNotIn(
            "generalization.evaluation_started",
            [kind for kind, _ in events],
            "a refused evaluation never started, so it never announces a start",
        )
        self.assertEqual(manager.status()["counters"]["leakage_refusals"], 1)

    def test_a_stored_run_carries_the_leakage_verdict_it_was_gated_on(self) -> None:
        clean = GeneralizationManager()
        view = clean.view(clean.evaluate({"split": "held_out", "seed": 2}).run_id)
        self.assertTrue(view["leakage"]["checked"], "the check ran, and the run must say so")
        self.assertTrue(view["leakage"]["clean"])
        self.assertGreater(view["leakage"]["held_out_tasks"], 0)

        # With the guard switched off the run proceeds and must carry the verdict
        # AND the violation it was allowed to proceed with.
        advisory = GeneralizationManager(settings={"require_clean_splits": False})
        self._leaky(advisory)
        dirty = advisory.view(advisory.evaluate({"split": "held_out", "seed": 3}).run_id)
        self.assertFalse(dirty["leakage"]["clean"], "a number measured over a leak says so")
        self.assertTrue(dirty["violations"], "and the violation is recorded, not swallowed")

    def test_the_held_out_only_policy_is_frozen_with_the_run(self) -> None:
        manager = GeneralizationManager(settings={"evaluate_held_out_only": False})
        view = manager.view(manager.evaluate({"seed": 4}).run_id)
        self.assertFalse(view["settings"]["evaluate_held_out_only"])
        self.assertEqual(
            view["tasks_evaluated"],
            len(manager.tasks()),
            "with the policy off an unfiltered run measures the whole corpus",
        )

        default = GeneralizationManager()
        default_view = default.view(default.evaluate({"seed": 4}).run_id)
        self.assertTrue(default_view["settings"]["evaluate_held_out_only"])
        self.assertEqual(default_view["settings"]["split"], Split.HELD_OUT.value)
        self.assertLess(default_view["tasks_evaluated"], len(default.tasks()))
        self.assertEqual(
            default_view["tasks_evaluated"],
            len(default.tasks(split=Split.HELD_OUT)),
            "a run that measures the unseen examples says which policy chose them",
        )

    def test_an_explicit_any_split_still_evaluates_the_whole_corpus(self) -> None:
        manager = GeneralizationManager()
        view = manager.view(manager.evaluate({"split": "any", "seed": 5}).run_id)
        self.assertEqual(view["tasks_evaluated"], len(manager.tasks()))
        self.assertEqual(view["settings"]["split"], "any")


class HonestEndingsTests(unittest.TestCase):
    """Ambiguity and unsupport are answers, not failures."""

    def test_an_ambiguous_task_reports_ambiguity_with_the_competing_rules(self) -> None:
        task = task_by_id("ho_ambiguous_double_reading")
        result = evaluate_task(task, solver="infer")
        self.assertIsNot(result.status, TaskStatus.SOLVED)
        self.assertEqual(result.status, TaskStatus.AMBIGUOUS)
        self.assertEqual(result.failure, FailureCategory.MULTIPLE_PLAUSIBLE_RULES)
        self.assertIn("rules", result.failure_reason)
        self.assertIsNone(result.verification, "an ambiguous answer was never checked")

    def test_a_task_outside_the_library_is_reported_unsupported_not_answered(self) -> None:
        task = task_by_id("ho_unsupported_row_order")
        result = evaluate_task(task, solver="infer")
        self.assertEqual(result.status, TaskStatus.UNSUPPORTED)
        self.assertEqual(result.failure, FailureCategory.NO_CANDIDATE_RULE_FOUND)
        self.assertFalse(result.valid_solution)

    def test_an_unsupported_spatial_relation_is_named_rather_than_measured(self) -> None:
        task = task_by_id("ho_sp_unsupported_relation")
        result = evaluate_task(task, solver="infer")
        self.assertEqual(result.status, TaskStatus.UNSUPPORTED)
        self.assertIn("not one this build measures", result.failure_reason)

    def test_the_baseline_that_refuses_to_answer_is_reported_as_a_baseline(self) -> None:
        task = task_by_id("gt_rotate90")
        result = evaluate_task(task, solver="no_answer")
        self.assertIsNot(result.status, TaskStatus.SOLVED)
        self.assertIsNotNone(result.failure)


# --------------------------------------------------------------------------- #
# Budgets, seeds and reproducibility
# --------------------------------------------------------------------------- #


class BudgetTests(unittest.TestCase):
    """A spent budget is an ending, never a wrong answer."""

    def test_a_spent_time_budget_ends_the_solve_without_an_answer(self) -> None:
        import time as _time

        task = task_by_id("gt_rotate90")
        # Started five seconds ago with a 50 ms budget: the clock is spent
        # before the first operator proposes anything, and a spent budget is an
        # ending rather than a guess. (``max_seconds=0`` means "no time limit";
        # the manager clamps a configured value to at least 50 ms, so a caller
        # cannot disable the clock through the API either.)
        expired = Budget(max_seconds=0.05, started=_time.monotonic() - 5.0)
        result = evaluate_task(task, solver="infer", budget=expired)
        self.assertEqual(result.status, TaskStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.failure, FailureCategory.RESOURCE_BUDGET_EXHAUSTED)
        self.assertIsNone(result.verification)
        self.assertIsNone(result.exact_match, "nothing was compared, so nothing matched")

    def test_a_one_candidate_budget_still_reports_what_it_examined(self) -> None:
        task = task_by_id("gt_rotate90")
        result = evaluate_task(task, solver="infer", budget=Budget(max_candidates=1))
        self.assertLessEqual(result.candidates_examined, 1)

    def test_settings_clamp_to_hard_ceilings_rather_than_trusting_the_request(self) -> None:
        view = GeneralizationSettingsView.from_mapping(
            {"max_candidates": 10_000_000, "max_seconds": 1e9, "keep_runs": 10_000}
        )
        self.assertLessEqual(view.max_candidates, 4000)
        self.assertLessEqual(view.max_seconds, 300.0)
        self.assertLessEqual(view.keep_runs, 50)


class ReproducibilityTests(unittest.TestCase):
    """Two identical runs hash the same; a different solver does not."""

    def _manager(self, **settings: Any) -> tuple[GeneralizationManager, _MemoryStore]:
        store = _MemoryStore()
        return (
            GeneralizationManager(settings=settings or None, store=store),
            store,
        )

    def test_the_same_seed_and_settings_produce_the_same_fingerprint(self) -> None:
        first, _ = self._manager()
        second, _ = self._manager()
        body = {"split": "held_out", "seed": 4242, "max_candidates": 200}
        first.evaluate(body)
        second.evaluate(body)
        a = first.fingerprint(first.view(first.runs()[0].run_id))
        b = second.fingerprint(second.view(second.runs()[0].run_id))
        self.assertEqual(a, b, "identical inputs must produce an identical outcome fingerprint")

    def test_a_different_solver_produces_a_different_fingerprint(self) -> None:
        first, _ = self._manager()
        second, _ = self._manager()
        body = {"split": "held_out", "seed": 4242}
        first.evaluate(body)
        second.evaluate({**body, "solver": "identity"})
        a = first.fingerprint(first.view(first.runs()[0].run_id))
        b = second.fingerprint(second.view(second.runs()[0].run_id))
        self.assertNotEqual(a, b)

    def test_the_run_records_the_settings_and_seed_it_was_frozen_with(self) -> None:
        manager, _ = self._manager()
        run = manager.evaluate({"split": "held_out", "seed": 99, "max_candidates": 123})
        self.assertEqual(run.seed, 99)
        self.assertEqual(run.settings["max_candidates"], 123)
        self.assertEqual(run.settings["corpus_version"], manager.splits()["corpus_version"])


# --------------------------------------------------------------------------- #
# Persistence, provenance and the failure feed
# --------------------------------------------------------------------------- #


class PersistenceTests(unittest.TestCase):
    """Runs survive a restart; an unreadable record is reported, not fatal."""

    def test_runs_are_written_on_completion_and_restored_on_boot(self) -> None:
        store = _MemoryStore()
        first = GeneralizationManager(store=store)
        run = first.evaluate({"split": "held_out", "task_ids": ["gt_rotate90"]})
        self.assertIn(STORE_KEY, store.writes, "a finished run is written immediately")

        second = GeneralizationManager(store=store)
        report = second.restore()
        self.assertEqual(report["restored"], 1)
        restored = second.run(run.run_id)
        self.assertEqual(restored.solved, run.solved)
        self.assertEqual(
            [result.task_id for result in restored.results],
            [result.task_id for result in run.results],
        )

    def test_an_unreadable_store_is_reported_and_never_raises(self) -> None:
        manager = GeneralizationManager(store=_BrokenStore())
        report = manager.restore()
        self.assertEqual(report["restored"], 0)
        self.assertIn("could not be read", report["reason"])
        closed = manager.close()
        self.assertEqual(closed["saved"], 0, "a store that cannot be written is not fatal")

    def test_a_run_row_is_trimmed_while_the_run_itself_is_whole(self) -> None:
        manager = GeneralizationManager()
        run = manager.evaluate({"split": "held_out", "task_ids": ["gt_rotate90"]})
        row = manager.run_rows()[0]
        self.assertNotIn("results", row, "a list surface never embeds the result bodies")
        self.assertNotIn("failures", row)
        view = manager.view(run.run_id)
        self.assertIn("results", view)
        self.assertIn("failures", view)
        self.assertIn("recommendations", view)

    def test_the_kept_runs_are_bounded(self) -> None:
        manager = GeneralizationManager(settings={"keep_runs": 2})
        for _ in range(4):
            manager.evaluate({"split": "held_out", "task_ids": ["gt_rotate90"]})
        self.assertEqual(len(manager.runs()), 2, "the store is bounded by keep_runs")
        self.assertEqual(len(manager.run_rows()), 2)

    def test_the_export_carries_its_provenance_and_its_reproduce_block(self) -> None:
        manager = GeneralizationManager()
        run = manager.evaluate({"split": "held_out", "task_ids": ["gt_rotate90"], "seed": 5})
        export = manager.export(run.run_id)
        self.assertEqual(export["reproduce"]["seed"], 5)
        self.assertEqual(export["reproduce"]["solver"], "infer")
        self.assertIn("corpus", export)
        self.assertIn("operators", export)
        self.assertTrue(export["fingerprint"])


class FailureFeedTests(unittest.TestCase):
    """Findings reach a person; nothing is executed on their behalf."""

    def test_failures_are_grouped_by_category_with_advice_that_matches(self) -> None:
        manager = GeneralizationManager()
        run = manager.evaluate({"split": "held_out"})
        groups = manager.failure_groups(run.run_id)
        self.assertIn("multiple_plausible_rules", groups)
        self.assertIn("no_candidate_rule_found", groups)
        for record in run.failures:
            self.assertTrue(record.reason, "a failure without a reason is not actionable")
            self.assertTrue(record.evidence, "a failure without evidence cannot be reproduced")
            self.assertEqual(record.reproduce["solver"], "infer")

    def test_a_recommendation_advises_and_never_executes(self) -> None:
        manager = GeneralizationManager()
        run = manager.evaluate({"split": "held_out"})
        recommendations = manager.recommendations(run.run_id)
        self.assertTrue(recommendations)
        for item in recommendations:
            self.assertTrue(item.recommendation)
            self.assertGreater(item.occurrences, 0)
            self.assertTrue(item.tasks)

    def test_an_unknown_run_id_is_named_rather_than_silently_empty(self) -> None:
        manager = GeneralizationManager()
        with self.assertRaises(KeyError):
            manager.view("run-that-never-existed")


# --------------------------------------------------------------------------- #
# Capabilities, the overview, and the game-evaluation contracts
# --------------------------------------------------------------------------- #


class CapabilityTests(unittest.TestCase):
    """The table states the absences as loudly as the presences."""

    def test_the_capability_states_use_this_build_s_own_vocabulary(self) -> None:
        rows = capability_rows(GeneralizationManager())
        states = {row.state.value for row in rows}
        self.assertIn("implemented", states)
        self.assertIn("unavailable", states)
        self.assertIn("future", states)

    def test_no_capability_claims_a_single_intelligence_score_or_gameplay(self) -> None:
        rows = {row.name: row for row in capability_rows(GeneralizationManager())}
        score = rows["generalization.single_intelligence_score"]
        self.assertEqual(score.state.value, "unavailable")
        gameplay = rows["generalization.gameplaying"]
        self.assertEqual(gameplay.state.value, "future")
        solver = rows["generalization.arc_general_solver"]
        self.assertEqual(
            solver.state.value,
            "partially_implemented",
            "a named library is not a general ARC solver",
        )

    def test_the_overview_declares_everything_it_refuses_to_do(self) -> None:
        payload = overview()
        for flag in (
            "general_arc_solver",
            "hardcodes_benchmark_answers",
            "sees_held_out_answers_during_search",
            "leaks_held_out_answers_into_tuning",
            "single_intelligence_score",
            "fabricates_benchmark_scores",
            "labels_mock_results_as_real",
            "trains_anything",
            "downloads_models",
            "modifies_production_weights",
            "auto_starts_training",
            "claims_gameplay",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, payload)
                self.assertIs(payload[flag], False)
        self.assertIn("the dedicated post-Phase-26 game-playing evaluation", payload["deferred"])
        self.assertEqual(payload["grid_rules_supported"], list(GRID_RULES_SUPPORTED))


class GameEvalContractTests(unittest.TestCase):
    """Phase 26 §13: contracts over the Phase 25 session record, never a game."""

    def test_nine_contracts_are_defined_with_their_sources(self) -> None:
        self.assertEqual(len(GAME_EVAL_CONTRACTS), 9)
        ids = [item.contract_id for item in GAME_EVAL_CONTRACTS]
        self.assertEqual(len(ids), len(set(ids)))
        for item in GAME_EVAL_CONTRACTS:
            self.assertTrue(item.description)
            self.assertTrue(item.source)

    def test_a_figure_nobody_measured_is_none_rather_than_zero(self) -> None:
        row = summarise_embodied_session({"session_id": "s", "evaluation": {}})
        self.assertIsNone(row["seconds"], "no elapsed time was measured")
        self.assertIsNone(row["objective_completion_rate"])
        self.assertIsNone(row["observation_freshness_ratio"])
        self.assertIsNone(row["action_efficiency_inputs_per_completion"])

    def test_the_spread_is_reported_per_configuration_and_never_pooled(self) -> None:
        payload = evaluate_embodied_sessions(
            [
                {
                    "session_id": "a",
                    "environment_id": "gridworld",
                    "scenario": "clear",
                    "status": "completed",
                    "verdict": {"met": True},
                    "evaluation": {"actions_attempted": 4, "actions_completed": 2, "seconds": 1.5},
                    "trajectory": [1, 2, 3],
                },
                {
                    "session_id": "b",
                    "environment_id": "gridworld",
                    "scenario": "drifting",
                    "status": "budget_exhausted",
                    "verdict": {"met": False},
                    "evaluation": {"actions_attempted": 9, "actions_completed": 0},
                    "trajectory": [],
                },
            ]
        )
        self.assertEqual(len(payload["configurations"]), 1)
        bucket = payload["configurations"][0]
        self.assertEqual(bucket["sessions"], 2)
        self.assertEqual(bucket["scenarios"], ["clear", "drifting"], "configurations stay visible")
        self.assertEqual(bucket["completed"], 1)
        self.assertFalse(payload["note"].find("no real game") < 0)

    def test_an_empty_session_list_is_an_empty_report_not_a_perfect_score(self) -> None:
        payload = evaluate_embodied_sessions([])
        self.assertEqual(payload["sessions"], [])
        self.assertNotIn("score", payload)


# --------------------------------------------------------------------------- #
# The REAL application — one per test, over an isolated data directory
# --------------------------------------------------------------------------- #


class ApplicationWiringTests(unittest.TestCase):
    """The application owns the manager, and says so in its own status."""

    def setUp(self) -> None:
        if NovaControlApplication is None:  # pragma: no cover - import guard
            self.skipTest("novacontrol.application is unavailable")
        self._tmp = TemporaryDirectory()
        self._app = NovaControlApplication(data_dir=self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_status_surface_carries_the_generalization_section(self) -> None:
        status = self._app.status()
        self.assertIn("generalization", status)
        section = status["generalization"]
        self.assertEqual(section["phase"], "26")
        self.assertTrue(section["enabled"])
        self.assertGreater(section["corpus"]["counts"]["held_out"], 0)
        self.assertTrue(section["capabilities"], "the capability table is registered")

    def test_the_capability_registry_receives_the_generalization_rows(self) -> None:
        registry = self._app.intelligence.capabilities
        self.assertIsNotNone(registry.get("generalization.rule_induction"))
        self.assertIsNotNone(registry.get("generalization.split_control"))
        for definition in CAPABILITY_TABLE:
            with self.subTest(capability=definition.capability_id):
                declared = registry.get(definition.capability_id)
                self.assertIsNotNone(declared, "every row is on the ONE registry")
                self.assertEqual(declared.category, "generalization")  # type: ignore[union-attr]
                self.assertEqual(declared.executor, "generalization")  # type: ignore[union-attr]

    def test_an_evaluation_through_the_application_reaches_the_bus(self) -> None:
        async def scenario() -> list[str]:
            seen: list[str] = []

            async def handler(event: Any) -> None:
                seen.append(str(getattr(event, "type", "")))

            await self._app.event_bus.subscribe("*", handler)
            self._app.generalization_evaluate({"split": "held_out", "task_ids": ["gt_rotate90"]})
            await asyncio.sleep(0.05)
            await self._app.event_bus.unsubscribe("*", handler)
            return seen

        seen = asyncio.run(scenario())
        self.assertIn(EventType.GENERALIZATION_EVALUATION_STARTED.value, seen)
        self.assertIn(EventType.GENERALIZATION_EVALUATION_COMPLETED.value, seen)

    def test_the_event_payloads_declare_the_fields_they_send(self) -> None:
        for kind in (
            "generalization.task_registered",
            "generalization.evaluation_started",
            "generalization.evaluation_completed",
            "generalization.failure_classified",
            "generalization.leakage_detected",
        ):
            with self.subTest(kind=kind):
                self.assertIn(EventType(kind), EVENT_PAYLOAD_FIELDS)
                self.assertTrue(EVENT_PAYLOAD_FIELDS[EventType(kind)])

    def test_the_game_eval_reports_honestly_when_nothing_has_been_driven(self) -> None:
        payload = self._app.generalization_game_eval()
        self.assertEqual(payload["sessions_measured"], 0)
        self.assertFalse(payload["gameplay_claimed"])
        self.assertIn("empty report, not a zero score", payload["note"])

    def test_an_unknown_run_id_is_a_404_not_an_empty_record(self) -> None:
        with self.assertRaises(KeyError):
            self._app.generalization_view("nope")

    def test_a_run_survives_a_restart_of_the_application(self) -> None:
        run = self._app.generalization_evaluate({"split": "held_out", "task_ids": ["gt_rotate90"]})
        # ``close()`` is the flush ``stop()`` performs before the store goes away.
        self._app.generalization.close()
        rebooted = NovaControlApplication(data_dir=self._tmp.name)
        restored = rebooted.generalization_view(run["run_id"])
        self.assertEqual(restored["run_id"], run["run_id"])
        self.assertEqual(restored["solved"], run["solved"])


# --------------------------------------------------------------------------- #
# The REAL application over HTTP — the interface a user reaches
# --------------------------------------------------------------------------- #


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class GeneralizationHttpWorkflowTests(unittest.TestCase):
    """One real application per test, over a temp data dir, no device runners."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: Any = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> Any:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    # -- Workflow A: a simple grid transformation, end to end ----------------

    def test_workflow_a_a_simple_task_is_inferred_verified_and_read_back(self) -> None:
        listed = self._client.get("/generalization/tasks?split=held_out")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(listed.json()["answers_withheld"])

        response = self._client.post(
            "/generalization/evaluate", json={"task_ids": ["gt_rotate90"], "split": "any"}
        )
        self.assertEqual(response.status_code, 200)
        run = response.json()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["tasks_evaluated"], 1)
        result = run["results"][0]
        self.assertEqual(result["task_id"], "gt_rotate90")
        self.assertEqual(result["status"], "solved")
        self.assertIs(result["exact_match"], True)
        self.assertEqual(result["source"], "inferred")
        self.assertTrue(result["verification"]["verified"])

        fetched = self._client.get(f"/generalization/runs/{run['run_id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["solved"], 1)

    # -- Workflow B: an unseen variation is solved by the rule, not a lookup --

    def test_workflow_b_a_new_configuration_of_a_known_rule_is_solved(self) -> None:
        body = {
            "task_ids": ["gt_rotate90", "ho_rotate180_non_square", "ho_flip_vertical_colours"],
            "split": "any",
        }
        run = self._client.post("/generalization/evaluate", json=body).json()
        self.assertEqual(run["solved"], 3, "unseen colours and dimensions are solved by the rule")
        for result in run["results"]:
            self.assertEqual(result["source"], "inferred")

    # -- Workflow C: ambiguous examples report ambiguity ----------------------

    def test_workflow_c_ambiguity_is_reported_rather_than_resolved(self) -> None:
        run = self._client.post(
            "/generalization/evaluate", json={"task_ids": ["ho_ambiguous_double_reading"]}
        ).json()
        result = run["results"][0]
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["failure"], "multiple_plausible_rules")
        self.assertIsNone(result["exact_match"], "an ambiguous answer is never scored as a match")

    # -- Workflow D: an unsupported task reports the limitation ---------------

    def test_workflow_d_an_unsupported_task_is_reported_not_answered(self) -> None:
        run = self._client.post(
            "/generalization/evaluate", json={"task_ids": ["ho_unsupported_row_order"]}
        ).json()
        result = run["results"][0]
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["failure"], "no_candidate_rule_found")

    # -- Workflow E: baseline and candidate on the same held-out set ----------

    def test_workflow_e_the_baseline_and_the_candidate_are_compared(self) -> None:
        candidate = self._client.post(
            "/generalization/evaluate", json={"split": "held_out", "seed": 7}
        ).json()
        baseline = self._client.post(
            "/generalization/evaluate",
            json={"split": "held_out", "seed": 7, "solver": "identity", "baseline": "identity"},
        ).json()
        self.assertEqual(
            [item["task_id"] for item in candidate["results"]],
            [item["task_id"] for item in baseline["results"]],
            "a comparison over different task sets is not a comparison",
        )
        report = self._client.get(
            f"/generalization/compare?baseline={baseline['run_id']}&candidate={candidate['run_id']}"
        ).json()
        self.assertEqual(report["tasks_compared"], candidate["tasks_evaluated"])
        self.assertTrue(report["baseline_preserved"])
        self.assertGreater(len(report["improved"]), 0, "the inferred solver beats identity")
        self.assertTrue(
            report["regressed"] == [] or "regressions were detected" in " ".join(report["notes"])
        )
        self.assertEqual(
            {row["family_id"] for row in report["candidate_metrics"]},
            {row["family_id"] for row in candidate["metrics"]},
            "the comparison's metrics match the run's own metrics",
        )

    # -- Workflow F: the same configuration twice is reproducible -------------

    def test_workflow_f_two_identical_runs_hash_the_same(self) -> None:
        body = {"split": "held_out", "seed": 2026}
        first = self._client.post("/generalization/evaluate", json=body).json()
        second = self._client.post("/generalization/evaluate", json=body).json()
        first_export = self._client.get(f"/generalization/runs/{first['run_id']}/export").json()
        second_export = self._client.get(f"/generalization/runs/{second['run_id']}/export").json()
        self.assertEqual(first_export["fingerprint"], second_export["fingerprint"])
        self.assertEqual(first_export["reproduce"]["seed"], 2026)
        self.assertEqual(
            first_export["reproduce"]["settings"], second_export["reproduce"]["settings"]
        )
        self.assertNotEqual(first["run_id"], second["run_id"])

    # -- Workflow G: a small budget terminates and says so --------------------

    def test_workflow_g_a_tiny_budget_ends_honestly(self) -> None:
        response = self._client.post(
            "/generalization/evaluate",
            json={"task_ids": ["gt_rotate90"], "max_candidates": 1, "max_seconds": 0.05},
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["results"][0]
        self.assertIn(result["status"], {"solved", "budget_exhausted", "unsupported"})
        if result["status"] != "solved":
            self.assertIsNotNone(result["failure"])
            self.assertNotEqual(result["failure"], None)

    # -- Workflow H: a regression is detected and the baseline is preserved ---

    def test_workflow_h_a_worse_candidate_is_flagged_as_a_regression(self) -> None:
        good = self._client.post(
            "/generalization/evaluate", json={"task_ids": ["gt_rotate90"], "split": "any"}
        ).json()
        weak = self._client.post(
            "/generalization/evaluate",
            json={"task_ids": ["gt_rotate90"], "split": "any", "solver": "no_answer"},
        ).json()
        report = self._client.get(
            f"/generalization/compare?baseline={good['run_id']}&candidate={weak['run_id']}"
        ).json()
        self.assertIn("gt_rotate90", report["regressed"])
        self.assertTrue(any("regressions were detected" in note for note in report["notes"]))
        # The baseline record itself is untouched.
        reread = self._client.get(f"/generalization/runs/{good['run_id']}").json()
        self.assertEqual(reread["solved"], good["solved"])

    # -- Workflow I: the real entry point, traced end to end ------------------

    def test_workflow_i_the_http_entry_point_matches_the_evaluated_outputs(self) -> None:
        run = self._client.post("/generalization/evaluate", json={"split": "held_out"}).json()
        # The reported counts are the run's own results, recomputed here.
        statuses = [item["status"] for item in run["results"]]
        self.assertEqual(run["tasks_evaluated"], len(statuses))
        self.assertEqual(run["solved"], statuses.count("solved"))
        self.assertEqual(run["ambiguous"], statuses.count("ambiguous"))
        self.assertEqual(run["unsupported"], statuses.count("unsupported"))
        per_family = {row["family_id"]: row for row in run["metrics"]}
        counted: dict[str, int] = {}
        for item in run["results"]:
            counted[item["family_id"]] = counted.get(item["family_id"], 0) + 1
        self.assertEqual({key: value["tasks"] for key, value in per_family.items()}, counted)
        failures = self._client.get(f"/generalization/failures?run={run['run_id']}").json()
        self.assertEqual(failures["count"], len(run["failures"]))
        export = self._client.get(f"/generalization/runs/{run['run_id']}/export").json()
        self.assertEqual(export["run"]["solved"], run["solved"])

    # -- surface, refusals and authentication ---------------------------------

    def test_the_operating_surface_answers_and_the_refusals_name_the_rule(self) -> None:
        for path in (
            "/generalization/status",
            "/generalization/overview",
            "/generalization/families",
            "/generalization/operators",
            "/generalization/splits",
            "/generalization/runs",
            "/generalization/game-eval/contracts",
            "/generalization/game-eval",
        ):
            with self.subTest(path=path):
                self.assertEqual(self._client.get(path).status_code, 200)

        self.assertEqual(self._client.get("/generalization/runs/nope").status_code, 404)
        self.assertEqual(self._client.get("/generalization/tasks/nope").status_code, 404)
        self.assertEqual(self._client.get("/generalization/tasks?split=weird").status_code, 422)
        self.assertEqual(
            self._client.post("/generalization/evaluate", json={"solver": "magic"}).status_code,
            422,
        )
        self.assertEqual(
            self._client.post("/generalization/evaluate", json={"family": "chess"}).status_code,
            422,
        )
        families = self._client.get("/generalization/families").json()
        self.assertIsNone(families["aggregate_score"], "no blended intelligence score exists")

    def test_a_task_key_is_on_the_task_and_never_on_the_list(self) -> None:
        listed = self._client.get("/generalization/tasks").json()
        for row in listed["tasks"]:
            self.assertNotIn("expected_output", row)
        single = self._client.get("/generalization/tasks/ho_rotate180_non_square").json()
        self.assertIn("expected_output", single)
        self.assertIn("success_condition", single)

    def test_registering_a_task_is_validated_and_ids_cannot_be_replaced(self) -> None:
        source = self._client.get("/generalization/tasks/gt_rotate90").json()
        payload = {**source, "task_id": "operator_supplied_task"}
        created = self._client.post("/generalization/tasks", json=payload)
        self.assertEqual(created.status_code, 200)
        self.assertIn("expected_output", created.json())

        duplicate = self._client.post("/generalization/tasks", json=payload)
        self.assertEqual(duplicate.status_code, 422)
        self.assertIn("already in use", duplicate.json()["detail"])

        no_key = {**payload, "task_id": "operator_supplied_task_2", "expected_output": None}
        missing = self._client.post("/generalization/tasks", json=no_key)
        self.assertEqual(missing.status_code, 422)
        self.assertIn("expected_output", missing.json()["detail"])

        no_condition = {**payload, "task_id": "operator_supplied_task_3", "success_condition": ""}
        blank = self._client.post("/generalization/tasks", json=no_condition)
        self.assertEqual(blank.status_code, 422)
        self.assertIn("success_condition", blank.json()["detail"])

        # A registered task is listable and readable like any other, and it is
        # NOT part of the held-out split (it was registered as TRAIN).
        listed = self._client.get("/generalization/tasks?split=train").json()
        self.assertIn(
            "operator_supplied_task",
            [row["task_id"] for row in listed["tasks"]],
        )

    def test_every_generalization_route_requires_the_same_authentication(self) -> None:
        with mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": "s3cret-token"}):
            guarded = TestClient(create_app())
            with guarded:
                self.assertEqual(guarded.get("/generalization/status").status_code, 401)
                self.assertEqual(guarded.post("/generalization/evaluate", json={}).status_code, 401)
                allowed = guarded.get(
                    "/generalization/status", headers={"Authorization": "Bearer s3cret-token"}
                )
                self.assertEqual(allowed.status_code, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
